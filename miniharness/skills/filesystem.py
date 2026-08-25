"""本地文件系统 skill provider：从项目/自定义/用户/bundled 根发现 skill。

上游对照：packages/skill/skill-filesystem/src/index.ts（FileSystemSkillProvider +
discoverRoot / parseSkillFile / parseInvocationPolicy）。

契约（与上游一致）：
  * 六类根及其秩：project-dsh=100、project-agents=200、custom=300、
    user-dsh=400、user-agents=500、bundled=600；project 根需要 cwd 且向上
    找最近的 .git 目录作项目根
  * 发现规则：目录 bundle `<name>/SKILL.md` + 扁平 `<name>.md`；user-dsh
    根跳过 `.system`；条目按名字排序；坏条目（frontmatter 缺失/非法、
    name/description 缺失、名字非 kebab-case、invocation 非法）warn 跳过
    （fail-closed，单文件坏不影响其余）
  * parseInvocationPolicy：frontmatter 用 kebab-case 键 `disable-model-invocation`
    与 `user-invocable`；camelCase 旧键（modelInvocable/userInvocable/
    disableModelInvocation）出现即抛错引导；布尔接受 true/1/'1'/yes/on/…
    与 false/0/'0'/no/off/…，其它值抛 TypeError
  * get() 返回完整定义：invocation 重解析 + resourceBase
    {kind:'directory', path: 所在目录} + path
  * 缺失文件（ENOENT/ENOTDIR）静默返回空/None；其它 I/O 错误上抛
    （collect 端按 provider 失败处理，cacheable=False）

mini 简化（有意保留，须在文档标注）：
  * 同步 os 读取（上游 async ctx.fs / node:fs/promises）
  * frontmatter YAML：pyyaml 可选依赖（与 boot/composition 同款）；无 pyyaml
    时用内置极简 YAML 子集解析器（仅标量/引号/嵌套 mapping/块 scalar 列表；
    不支持的语法抛错 → 文件被忽略）
  * 无 skipSystem 之外的系统目录过滤启发（上游 nodeEntryKind 的 symlink
    跟随保留，目录条目归类到 'other' 时静默跳过）
  * 无 fs/observed 事件桥（mini 工具不产出 fs/observed 事件）
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml 为可选依赖
    yaml = None

from .registry import (
    BUNDLED_SKILL_RANK,
    is_skill_name,
)
from .watcher import SkillWatchManager

__all__ = [
    "FileSystemSkillProvider",
    "find_project_root",
    "parse_invocation_policy",
    "parse_skill_file",
]

logger = logging.getLogger("miniharness.skills")

PROJECT_DSH_RANK = 100
PROJECT_AGENTS_RANK = 200
CUSTOM_RANK = 300
USER_DSH_RANK = 400
USER_AGENTS_RANK = 500


def _resolve_dsh_home(value: str | None) -> Path:
    if value:
        return Path(value).resolve()
    env = os.environ.get("DSH_HOME")
    if env:
        return Path(env).resolve()
    return (Path.home() / ".dsh").resolve()


def find_project_root(cwd: str) -> Path:
    """从 cwd 向上找最近的 .git 目录；找不到返回 cwd 本身。"""
    current = Path(cwd).resolve()
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return Path(cwd).resolve()
        current = parent


# ---------- frontmatter YAML ----------

def _load_yaml_text(text: str) -> Any:
    if yaml is not None:
        return yaml.safe_load(text)
    return _parse_yaml_subset(text)


def _split_yaml_lines(text: str) -> list[tuple[int, str, str]]:
    """返回 [(indent, content, line_no)]，空行/纯注释行剔除，缩进以空格计。

    行内 `#` 注释（# 前有空白）在值解析阶段再处理，这里只保留原文。
    """
    result: list[tuple[int, str, str]] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ValueError(f"YAML 缩进不得使用 tab（行 {line_no}）")
        content = raw.rstrip()
        if not content.strip() or content.lstrip().startswith("#"):
            continue
        stripped = content.lstrip()
        indent = len(content) - len(stripped)
        result.append((indent, stripped, line_no))
    return result


def _parse_yaml_scalar(raw: str) -> Any:
    """解析一个标量值；裸字符串去行内注释。"""
    stripped = raw.strip()
    if stripped.startswith('"'):
        if len(stripped) < 2 or not stripped.endswith('"'):
            raise ValueError("未闭合的双引号字符串")
        return _unescape_quoted(stripped[1:-1])
    if stripped.startswith("'"):
        if len(stripped) < 2 or not stripped.endswith("'"):
            raise ValueError("未闭合的单引号字符串")
        return stripped[1:-1].replace("''", "'")
    if stripped in ("null", "Null", "NULL", "~"):
        return None
    if stripped in ("true", "True", "TRUE"):
        return True
    if stripped in ("false", "False", "FALSE"):
        return False
    if re.fullmatch(r"-?\d+", stripped):
        return int(stripped)
    if re.fullmatch(r"-?\d+\.\d+([eE][+-]?\d+)?", stripped):
        return float(stripped)
    if "#" in stripped:
        hash_index = stripped.index("#")
        if hash_index > 0 and stripped[hash_index - 1] in " \t":
            stripped = stripped[:hash_index].rstrip()
    return stripped


def _unescape_quoted(value: str) -> str:
    return (value.replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace("\\\\", "\\")
                .replace('\\"', '"'))


def _parse_yaml_subset(text: str) -> Any:
    """极简 YAML 子集解析器（仅 frontmatter 常见形态，fail-closed）。

    支持：嵌套 mapping（空格缩进）、`- ` 列表（扁平或作为映射值）、
    引号字符串、布尔/数字/null、块标量 `|` / `>`（literal / fold）。
    不支持的语法抛 ValueError——调用方把文件当作坏 frontmatter 忽略。
    """
    lines = _split_yaml_lines(text)
    root: dict[str, Any] = {}
    # 解析状态栈：(indent, container, parent_key_into_parent)
    stack: list[tuple[int, Any, str | None]] = [(-1, root, None)]
    index = 0
    block_buffer: list[str] = []
    block_mode: str | None = None  # '|' 或 '>'
    block_indent = 0

    def flush_block() -> None:
        nonlocal block_buffer, block_mode
        if block_mode is None:
            return
        parent = stack[-1][1]
        key = stack[-1][2]
        joined = "\n".join(block_buffer)
        if block_mode == ">":
            joined = re.sub(r"\n{2,}", "\n", joined)
            joined = joined.replace("\n", " ")
        if key is not None:
            parent[key] = joined
        else:
            parent.append(joined)
        block_buffer = []
        block_mode = None

    while index < len(lines):
        indent, stripped, line_no = lines[index]
        if block_mode is not None:
            if indent > block_indent:
                # 块标量内容行（教学简化：逐行去除公共前导空白）
                block_buffer.append(stripped)
                index += 1
                continue
            flush_block()
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        container = stack[-1][1]
        key = stack[-1][2]
        if stripped.startswith("- "):
            items = stripped[2:].strip()
            if isinstance(container, list):
                pass
            elif (isinstance(container, dict) and len(container) == 0
                    and len(stack) >= 2 and stack[-1][2] is not None):
                # 惰性类型：`key:` 后首个内容是列表 → 把空 dict 换成 list
                parent_container = stack[-2][1]
                new_list: list[Any] = []
                parent_container[stack[-1][2]] = new_list
                stack[-1] = (stack[-1][0], new_list, None)
                container = new_list
            else:
                raise ValueError(f"列表项出现在非列表上下文（行 {line_no}）")
            if items:
                container.append(_parse_yaml_scalar(items))
            else:
                child: list[Any] = []
                container.append(child)
                stack.append((indent, child, None))
                index += 1
                continue
            index += 1
            continue
        if ":" not in stripped:
            raise ValueError(f"YAML 行缺少 ':'（行 {line_no}）")
        pair_key, _, pair_value = stripped.partition(":")
        pair_key = pair_key.strip()
        if not pair_key:
            raise ValueError(f"空映射键（行 {line_no}）")
        if pair_key in ("true", "false", "null"):
            raise ValueError(f"非法映射键 {pair_key!r}（行 {line_no}）")
        raw_value = pair_value.strip()
        if raw_value in ("|", ">"):
            if not isinstance(container, dict):
                raise ValueError(f"块标量只能作映射值（行 {line_no}）")
            container[pair_key] = ""
            stack.append((indent, container, pair_key))
            block_mode = raw_value
            block_indent = indent
            index += 1
            continue
        parsed = _parse_yaml_scalar(raw_value) if raw_value else None
        if raw_value and raw_value.startswith("{"):
            raise ValueError(f"flow mapping 不受支持（行 {line_no}）")
        if isinstance(container, dict):
            if parsed is None and raw_value == "":
                child: dict[str, Any] = {}
                container[pair_key] = child
                stack.append((indent, child, pair_key))
            else:
                container[pair_key] = parsed
        elif key is not None:
            raise ValueError(f"映射值出现在嵌套列表后（行 {line_no}）")
        index += 1
    flush_block()
    return root


def parse_frontmatter(raw: str) -> dict | None:
    """解析 `---` 包裹的 frontmatter；无 frontmatter / 非对象返回 None。"""
    first_line_end = raw.find("\n")
    if first_line_end < 0:
        return None
    first_line = raw[:first_line_end].rstrip("\r")
    if first_line != "---":
        return None
    start = first_line_end + 1
    line_start = start
    while line_start <= len(raw):
        next_newline = raw.find("\n", line_start)
        line_end = len(raw) if next_newline < 0 else next_newline
        line = raw[line_start:line_end].rstrip("\r")
        if line == "---":
            body_start = len(raw) if next_newline < 0 else next_newline + 1
            yaml_text = raw[start:line_start]
            parsed = _load_yaml_text(yaml_text)
            if not isinstance(parsed, dict):
                return None
            return {"data": parsed, "body": raw[body_start:]}
        if next_newline < 0:
            return None
        line_start = next_newline + 1
    return None


# ---------- invocation 策略 ----------

def frontmatter_boolean(value: Any) -> bool:
    """frontmatter 布尔判定（对齐上游：true/1/'1'/yes/on/… vs 反义）。"""
    if isinstance(value, bool):
        return value
    if value == 1 or value == "1":
        return True
    if value == 0 or value == "0":
        return False
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in ("true", "yes", "on"):
            return True
        if lowered in ("false", "no", "off"):
            return False
    raise TypeError(f"frontmatter 字段必须为布尔值，got {value!r}")


def parse_invocation_policy(data: dict) -> dict:
    """从 frontmatter 解析 invocation 策略（camelCase 旧键出现即抛错）。"""
    for legacy, canonical in (
        ("disableModelInvocation", "disable-model-invocation"),
        ("modelInvocable", "disable-model-invocation"),
        ("userInvocable", "user-invocable"),
    ):
        if legacy in data:
            raise ValueError(
                f'frontmatter field "{legacy}" is unsupported; use "{canonical}"'
            )
    disable = data.get("disable-model-invocation")
    user = data.get("user-invocable")
    return {
        "modelInvocable": frontmatter_boolean(disable) is not True if disable is not None else True,
        "userInvocable": frontmatter_boolean(user) is not False if user is not None else True,
    }


# ---------- skill 文件解析 ----------

def parse_skill_file(raw: str) -> dict | None:
    """解析一个 skill 文件文本为 {name, description, whenToUse?, invocation,
    metadata?, content}；坏 frontmatter 返回 None（调用方 warn 跳过）。"""
    try:
        parsed = parse_frontmatter(raw)
    except Exception:
        return None
    if parsed is None:
        return None
    data = parsed["data"]
    name = data.get("name")
    description = data.get("description")
    if not isinstance(name, str) or name == "":
        return None
    if not isinstance(description, str) or description == "":
        return None
    if not is_skill_name(name):
        return None
    when_to_use = data.get("whenToUse")
    if when_to_use is not None and not isinstance(when_to_use, str):
        return None
    try:
        invocation = parse_invocation_policy(data)
    except Exception:
        return None
    metadata = data.get("metadata")
    if metadata is not None and (not isinstance(metadata, dict)):
        return None
    result: dict = {
        "name": name,
        "description": description,
        "invocation": invocation,
        "content": parsed["body"].strip(),
    }
    if when_to_use:
        result["whenToUse"] = when_to_use
    if metadata is not None:
        result["metadata"] = metadata
    return result


# ---------- 根与发现 ----------

class FileSystemSkillProvider:
    """同步文件系统 provider：项目/自定义/用户/bundled 根 → 候选。

    配置键（对齐上游 Config）：providerName / includeDefaultRoots / dshHome /
    agentsHome / customSkillDirs / bundledSkillDir / watch / watchUsePolling /
    watchStabilityThresholdMs / watchPollIntervalMs / watchMaxProjects /
    watchFollowSymlinks。

    watch 配置控制文件监听行为（对齐上游 SkillWatchManager）：
      watch=True（默认）时自动开启 watchdog 监听 skill 根目录；
      文件变更去抖后触发 invalidate_cache()。
    """

    def __init__(self, control: dict, config: dict | None = None):
        config = config or {}
        self.name = config.get("providerName") or "filesystem"
        self.include_default_roots = config.get("includeDefaultRoots", True)
        self.dsh_home = _resolve_dsh_home(config.get("dshHome"))
        agents_home = config.get("agentsHome") or os.environ.get("DSH_AGENTS_HOME")
        self.agents_home = Path(agents_home).resolve() if agents_home else (Path.home() / ".agents").resolve()
        custom = config.get("customSkillDirs") or []
        if not isinstance(custom, (list, tuple)):
            raise TypeError("customSkillDirs must be an array")
        self.custom_skill_dirs = [Path(item).resolve() for item in custom]
        bundled = config.get("bundledSkillDir")
        if bundled is None and self.include_default_roots:
            bundled = os.environ.get("DSH_BUNDLED_SKILL_DIR")
        self.bundled_skill_dir = Path(bundled).resolve() if bundled else None
        self._control = control
        self._watch_config = {k: v for k, v in config.items() if k.startswith("watch")}
        self._watcher: SkillWatchManager | None = None

    # ---------- 发现（provider 接口：list / get） ----------

    def list(self, options: dict | None = None) -> list[dict]:
        options = options or {}
        roots = self._roots(options.get("cwd"))
        # lazy-init watcher 并同步 root 列表
        self._ensure_watcher()
        if self._watcher is not None:
            self._watcher.update_roots(roots)
        candidates: list[dict] = []
        for root in roots:
            candidates.extend(self._discover_root(root))
        return candidates

    def get(self, candidate: dict, options: dict | None = None) -> dict | None:
        path = candidate.get("path")
        if not isinstance(path, str):
            return None
        raw = _read_text(path)
        if raw is None:
            return None
        parsed = parse_skill_file(raw)
        if parsed is None:
            return None
        directory = os.path.dirname(path)
        result: dict = {
            "name": parsed["name"],
            "description": parsed["description"],
            "invocation": parsed["invocation"],
            "source": candidate["source"],
            "provider": self.name,
            "resourceBase": {"kind": "directory", "path": directory},
            "path": path,
            "content": parsed["content"],
        }
        if "whenToUse" in parsed:
            result["whenToUse"] = parsed["whenToUse"]
        if "metadata" in parsed:
            result["metadata"] = parsed["metadata"]
        return result

    def invalidate(self) -> None:
        self._control["invalidate"]()

    def _ensure_watcher(self) -> None:
        """lazy-init watcher（首次 list() 时创建；watch=False 时不创建）。"""
        if self._watcher is not None:
            return
        if not self._watch_config.get("watch", True):
            return
        try:
            self._watcher = SkillWatchManager(
                invalidate_callback=self.invalidate,
                config=self._watch_config,
            )
        except Exception:
            logger.debug("skill watcher init failed, falling back to no-watch", exc_info=True)

    def dispose(self) -> None:
        """停止 watcher（对齐上游 SkillWatchManager.dispose）。"""
        if self._watcher is not None:
            self._watcher.dispose()
            self._watcher = None

    # ---------- 内部 ----------

    def _roots(self, cwd: str | None) -> list[dict]:
        roots: list[dict] = []
        if self.include_default_roots and cwd:
            project_root = find_project_root(cwd)
            roots.append({
                "path": str(Path(project_root) / ".dsh" / "skills"),
                "source": "project-dsh",
                "rank": PROJECT_DSH_RANK,
            })
            roots.append({
                "path": str(Path(project_root) / ".agents" / "skills"),
                "source": "project-agents",
                "rank": PROJECT_AGENTS_RANK,
            })
        roots.extend({
            "path": str(path),
            "source": "custom",
            "rank": CUSTOM_RANK,
        } for path in self.custom_skill_dirs)
        if self.include_default_roots:
            roots.append({
                "path": str(self.dsh_home / "skills"),
                "source": "user-dsh",
                "rank": USER_DSH_RANK,
                "skip_system": True,
            })
            roots.append({
                "path": str(self.agents_home / "skills"),
                "source": "user-agents",
                "rank": USER_AGENTS_RANK,
            })
        if self.bundled_skill_dir is not None:
            roots.append({
                "path": str(self.bundled_skill_dir),
                "source": "bundled",
                "rank": BUNDLED_SKILL_RANK,
            })
        return roots

    def _discover_root(self, root: dict) -> list[dict]:
        entries = self._list_root_entries(root)
        candidates: list[dict] = []
        for entry in sorted(entries, key=lambda e: e["name"]):
            if root.get("skip_system") and entry["name"] == ".system":
                continue
            if entry["type"] == "directory":
                locator_path = os.path.join(entry["path"], "SKILL.md")
                directory = entry["path"]
            elif entry["type"] == "file" and entry["name"].endswith(".md"):
                locator_path = entry["path"]
                directory = root["path"]
            else:
                continue
            raw = _read_text(locator_path)
            if raw is None:
                continue
            parsed = parse_skill_file(raw)
            if parsed is None:
                logger.warning("skill file %s ignored: invalid or missing YAML frontmatter", locator_path)
                continue
            candidate: dict = {
                "name": parsed["name"],
                "description": parsed["description"],
                "invocation": parsed["invocation"],
                "provider": self.name,
                "source": root["source"],
                "rank": root["rank"],
                "locator": {"path": locator_path, "directory": directory},
                "resourceBase": {"kind": "directory", "path": directory},
                "path": locator_path,
            }
            if "whenToUse" in parsed:
                candidate["whenToUse"] = parsed["whenToUse"]
            if "metadata" in parsed:
                candidate["metadata"] = parsed["metadata"]
            candidates.append(candidate)
        return candidates

    def _list_root_entries(self, root: dict) -> list[dict]:
        try:
            names = sorted(os.listdir(root["path"]))
        except FileNotFoundError:
            return []
        except NotADirectoryError:
            return []
        except PermissionError:
            return []
        entries: list[dict] = []
        for name in names:
            full = os.path.join(root["path"], name)
            entries.append({"name": name, "type": _entry_kind(full), "path": full})
        return entries


def _entry_kind(path: str) -> str:
    """目录/文件/其它（symlink 跟随目标；跟随失败按其它处理）。"""
    try:
        if os.path.isdir(path):
            return "directory"
        if os.path.isfile(path):
            return "file"
    except OSError:
        return "other"
    return "other"


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        return None
    except IsADirectoryError:
        return None
    except PermissionError:
        return None
