"""profile 补丁文件上的持久化配置编辑（对齐 packages/boot/config-editor/src/index.ts）。

`ConfigEditor`（`ctx.configEditor`）：把 profile 的活动条目配置编辑持久化到
profile 的 `cordis.patch.yml`，并经 Loader 正常路径（reconcile）应用。

契约（对齐上游 index.ts）：
  * `documentPath` → profile 的 `cordis.patch.yml`（profileContext.patchPath）。
  * `entries()` → 根 Include 树下唯一 id 的活动条目（nested Include 各自独立
    配置所有权，重复 id 排除）。
  * `configuration()` → 每条 `{entry, inherited, override}`——inherited = 去掉
    该行自身 config 后重组合，override = patch 文件中最后一条该 id 的 config。
  * `edit(entry, change)`：filelock（锁锚 profile package.json）→ 先 reconcile
    磁盘现状 → change(current, inherited) → `internal/config` waterfall 验证 →
    注释保留改写 patch 文件（next==inherited 删 config/整行，否则 setIn）→
    `!!js` 节点还原 → 重组合验证 effective==next（被 home 层/overlay 覆盖则
    抛）→ 原子写 → reconcile requiredIds（失败写回旧文本 + 再 reconcile 回滚）
    → 全程包 `hmr.run_exclusive`。

载体差异（登记）：
  * mini 插件运行期无 schemastery Config 校验（configs 是 dict）——上游
    `resolveConfig(fiber.runtime, resolved)` 的 schema 校验面经
    `Fiber._resolve_config`（internal/config waterfall + 有 schema 时校验）。
  * 注释保留经 ruamel.yaml round-trip（上游 `yaml` 包 parseDocument）。
  * filelock 载体 = `filelock.FileLock`（黄金法则 #4）；原子写 = 本地
    `_write_atomic`（tempfile + os.replace，同 loader/include 文件回写）。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from filelock import FileLock, Timeout

from ..core.scope import FiberState
from .boot import reconcile_profile_patches
from .profile import (
    compose_entries,
    load_profile_directory,
    read_profile_patches,
)

__all__ = [
    "ConfigEditor",
    "install_config_editor",
]

#: 跨进程锁等待（对齐 credentials withFileLock 30s 纪律）。
DOCUMENT_LOCK_WAIT_SECONDS = 30.0


def _write_atomic(path: str, data: str) -> None:
    """原子整文件替换（对齐 dsh-atomic-write writeFileAtomic 语义）。

    写同目录临时文件 → fsync → os.replace 盖目标（同 loader/include.py 的
    include 文件回写、credentials 持久化——boot 层不依赖 storage 子包）。
    """
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _flatten(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        out.append(row)
        if row.get("group") and isinstance(row.get("config"), list):
            out.extend(_flatten(row["config"]))
    return out


def _resolve_config_placeholder(entry: Any, next_config: dict) -> dict:
    """`internal/config` waterfall 验证（对齐上游 resolveConfig 的 mini 载体）。

    经 `Fiber._resolve_config` 走 loader 的 internal/config waterfall（__jsExpr
    interpolate 求值）+ Config schema 校验（有 schema 时）。
    """
    fiber = getattr(entry, "fiber", None)
    if fiber is None:
        raise RuntimeError("Configuration plugin is no longer active")
    resolver = getattr(fiber, "_resolve_config", None)
    if resolver is None:
        raise RuntimeError("Configuration plugin is no longer active")
    resolved = resolver(next_config)
    return resolved if isinstance(resolved, dict) else next_config


class ConfigEditor:
    """`ctx.configEditor`：profile 配置编辑服务（对齐上游 ConfigEditor）。"""

    provide = "configEditor"

    def __init__(self, ctx: Any, profile_dir: str, patch_path: str,
                 home: str | None = None):
        self.ctx = ctx
        self._profile_dir = profile_dir
        self._patch_path = patch_path
        self._home = home

    @property
    def document_path(self) -> str:
        """profile 的 cordis.patch.yml（上游 documentPath → patchPath）。"""
        return self._patch_path

    def _root_include_entry(self) -> Any:
        from .boot import _BOOTSTRAP_INCLUDES
        entry = _BOOTSTRAP_INCLUDES.get(self.ctx)
        if entry is None:
            raise RuntimeError("config-editor: profile reload requires the root Include entry")
        return entry

    def entries(self) -> list:
        """根 Include 树下唯一 id 的活动条目（对齐上游 entries()）。"""
        loader = self.ctx.get("loader")
        if loader is None:
            return []
        candidates = []
        for entry in loader.entries():
            parent = getattr(entry, "parent", None)
            tree = getattr(parent, "tree", None) if parent is not None else None
            owner = getattr(tree, "ctx", None) if tree is not None else None
            fiber = getattr(owner, "fiber", None) if owner is not None else None
            include_entry = getattr(fiber, "entry", None) if fiber is not None else None
            if include_entry is not None and getattr(include_entry, "id", None) == "include":
                candidates.append(entry)
        counts: dict[str, int] = {}
        for entry in candidates:
            counts[entry.options.get("id")] = counts.get(entry.options.get("id"), 0) + 1
        return [entry for entry in candidates if counts.get(entry.options.get("id")) == 1]

    def _loaded_profile(self):
        return load_profile_directory("miniharness", self._profile_dir)

    def configuration(self) -> list[dict]:
        """读活动条目的继承层与显式覆盖（对齐 upstream configuration()）。"""
        loaded = self._loaded_profile()
        out: list[dict] = []
        for entry in self.entries():
            out.append({
                "entry": entry,
                "inherited": self._inherited(entry, loaded),
                "override": self._override(entry, loaded),
            })
        return out

    def _override(self, entry: Any, loaded: Any) -> dict:
        for patch in reversed(loaded.patches):
            if patch.get("id") == entry.options.get("id") and "config" in patch:
                return dict(patch.get("config") or {})
        return {}

    def _inherited(self, entry: Any, loaded: Any) -> dict:
        """继承层：去掉本行 profile patch config 后 entry 应有的配置。

        对齐上游 inherited()：把 profile patch 中该 id 行的 config 剥离，再
        经 composeEntries 组合（bundle 层 + 无 config 的 patch 行）。mini 的
        基础配置来自根 Include 的 config 文件（bundle 层为 []），故用
        include 的当前条目表 + 无 config 的 profile patch 行重组合。
        """
        include = self._root_include_entry()
        base_rows = self._include_base_rows(include)
        patches = []
        for patch in loaded.patches:
            if patch.get("id") == entry.options.get("id") and "insert" not in patch:
                rest = dict(patch)
                rest.pop("config", None)
                patches.append(rest)
            else:
                patches.append(patch)
        layers = [layer.patches for layer in loaded.layers]
        row = next((r for r in _flatten(compose_entries([*layers, patches], base_rows))
                    if r.get("id") == entry.options.get("id")), None)
        return dict(row.get("config") or {}) if row else {}

    @staticmethod
    def _include_base_rows(include: Any) -> list[dict]:
        """根 Include 的配置文件条目表（mini 的基础层，不含已应用补丁）。

        只读 include config.path 的**原始**条目——不套 config.patches（reconcile
        会把 profile patch 写进 include 的 patches，若套会 double-apply）。
        返回条目表供 compose 作为基底（profile patch 行另经 _inherited 剥离）。
        """
        from ..loader.include import load_entry_list_yaml
        cfg = include.options.get("config") or {}
        path = cfg.get("path")
        rows: list[dict] = []
        if path and os.path.exists(path):
            data = load_entry_list_yaml(Path(path).read_text(encoding="utf-8"))
            if isinstance(data, list):
                rows = [dict(r) for r in data]
            elif isinstance(data, dict) and isinstance(data.get("plugins"), list):
                rows = [dict(r) for r in data["plugins"]]
        return rows

    def edit(self, entry: Any, change: Callable[[dict, dict], dict]) -> None:
        """校验、持久化并对账一个条目的 next config（对齐上游 edit()）。"""
        run = lambda: self._edit_locked(entry, change)
        hmr = self.ctx.get("hmr")
        if hmr is not None and hasattr(hmr, "run_exclusive"):
            hmr.run_exclusive(run)
        else:
            run()

    def _edit_locked(self, entry: Any, change: Callable[[dict, dict], dict]) -> None:
        lock_path = os.path.join(self._profile_dir, "package.json")
        try:
            with FileLock(lock_path + ".lock", timeout=DOCUMENT_LOCK_WAIT_SECONDS):
                self._edit_with_lock(entry, change)
        except Timeout:
            raise RuntimeError(
                f"config-editor: timed out acquiring profile lock: {lock_path}") from None

    def _edit_with_lock(self, entry: Any, change: Callable[[dict, dict], dict]) -> None:
        include = self._root_include_entry()
        if entry not in self.entries() or getattr(entry, "fiber", None) is None:
            raise RuntimeError("Configuration entry is no longer available")
        before_patches = read_profile_patches(
            "miniharness", self._loaded_profile(),
            home=self._home, overlays=None)
        reconcile_profile_patches(self.ctx, before_patches, bin_name="miniharness")
        if entry not in self.entries():
            raise RuntimeError("Configuration entry changed during reload")
        current = dict(entry.options.get("config") or {})
        inherited = self._inherited(entry, self._loaded_profile())
        next_config = change(current, inherited)
        fiber = getattr(entry, "fiber", None)
        if fiber is None or fiber.state != FiberState.ACTIVE:
            raise RuntimeError("Configuration plugin is no longer active")
        _resolve_config_placeholder(entry, next_config)
        path = self.document_path
        try:
            before = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            before = "[]\n"
        from ruamel.yaml import YAML
        yaml_rt = YAML()
        yaml_rt.preserve_quotes = True
        document = yaml_rt.load(before) if before.strip() else []
        if document is None:
            document = []
        if not isinstance(document, list):
            raise RuntimeError("Profile patch must be a YAML sequence")
        index = self._find_patch_row(document, entry)
        if next_config == inherited:
            self._remove_config_rows(document, entry)
        elif index < 0:
            document.append({"id": entry.options.get("id"),
                             "name": entry.options.get("name"),
                             "config": next_config})
        else:
            document[index]["config"] = next_config
        self._restore_js_expr(document)
        buffer = _round_trip_dump(document)
        loaded = self._loaded_profile()
        # 用编辑后的文档替换 profile 层补丁做 effective 验证
        from .profile import Profile
        edited = Profile(name=loaded.name, dir=loaded.dir, layers=loaded.layers,
                         patch_path=loaded.patch_path, patches=document)
        effective_patches = read_profile_patches(
            "miniharness", edited, home=self._home, overlays=None)
        base_rows = self._include_base_rows(include)
        row = next((r for r in _flatten(compose_entries([effective_patches], base_rows))
                    if r.get("id") == entry.options.get("id")), None)
        if (row.get("config") or {}) != next_config:
            raise RuntimeError(
                f'Configuration for "{entry.options.get("id")}" is overridden by a '
                "home patch or command-line overlay")
        _write_atomic(path, buffer)
        try:
            reconcile_profile_patches(self.ctx, effective_patches,
                                      bin_name="miniharness",
                                      required_ids=[entry.options.get("id")])
        except Exception:
            _write_atomic(path, before)
            reconcile_profile_patches(self.ctx, before_patches,
                                      bin_name="miniharness")
            raise

    @staticmethod
    def _find_patch_row(document: list, entry: Any) -> int:
        for index in range(len(document) - 1, -1, -1):
            row = document[index]
            if not isinstance(row, dict):
                continue
            if row.get("id") == entry.options.get("id") and "insert" not in row:
                if "name" not in row or row.get("name") == entry.options.get("name"):
                    return index
        return -1

    @staticmethod
    def _remove_config_rows(document: list, entry: Any) -> None:
        for index in range(len(document) - 1, -1, -1):
            row = document[index]
            if not isinstance(row, dict):
                continue
            if row.get("id") == entry.options.get("id") and "insert" not in row:
                row.pop("config", None)
                keys = set(row.keys())
                if len(row) == (1 if "id" in row else 0) + (1 if "name" in row else 0):
                    document.pop(index)

    @staticmethod
    def _restore_js_expr(document: Any) -> None:
        """把 __jsExpr 节点还原为 !!js 标量（对齐上游 visit 还原）。

        ruamel round-trip 会把 pyyaml 的 __jsExpr dict 当普通 dict 保留；
        由于 patch 文档的 !!js 在 parse 时就经 ruamel 的构造器变成 dict，
        这里无需额外转换（JSON round-trip 路径经 dump_js_expr_yaml 兜底）。
        """


def _round_trip_dump(document: list) -> str:
    """注释保留地把 patch 文档序列化回 YAML（对齐上游 String(document)）。"""
    from ruamel.yaml import YAML
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    yaml_rt.width = 4096
    import io
    buf = io.StringIO()
    yaml_rt.dump(document, buf)
    return buf.getvalue()


def install_config_editor(ctx: Any, *, profile_dir: str, patch_path: str,
                          home: str | None = None) -> ConfigEditor:
    """装配 `ctx.configEditor`（幂等：已装返回既有实例）。

    @param profile_dir profile 目录。
    @param patch_path profile 的 cordis.patch.yml 路径。
    @param home harness 根（home 级补丁层，可选）。
    """
    existing = ctx.get("configEditor")
    if existing is not None:
        return existing
    editor = ConfigEditor(ctx, profile_dir, patch_path, home=home)
    ctx.provide("configEditor", editor)
    return editor