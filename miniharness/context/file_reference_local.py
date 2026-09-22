"""file-reference-local：`ctx.fileReferences` 的本地文件系统实现（对齐 packages/context/file-reference-local）。

`WorkspaceFileSearch` 为一个 agent 工作目录维护可复用、可失效的路径索引：目录作用域查询列实时
状态，裸模糊查询共享一次有界遍历；排序确定（精确/前缀/包含/子序列 + 目录加成）。`@file` 只产
路径候选，文件内容留在 `read` 工具之后。

载体差异（登记）：
  * 上游索引遍历为异步且失效后**后台**重建（旧索引继续应答）；mini 同步模型下失效后下次查询
    同步重建（无后台刷新窗口），语义等价、无并发竞态。
  * 上游 `list(agent, query, signal)` 为 Promise + AbortSignal；mini 为同步调用（无 signal）。
  * 提示词节 order 用字面量 900（上游 `getSectionOrder('FILE_REFERENCE')`）。
"""
from __future__ import annotations

import os
import stat
from typing import Any

from ..core.scope import Context
from .file_reference import FILE_REFERENCE_PROMPT, FileReferenceService

__all__ = [
    "DEFAULT_FILE_SEARCH_EXCLUDED_DIRECTORIES",
    "DEFAULT_FILE_SEARCH_MAX_ENTRIES",
    "DEFAULT_FILE_SEARCH_MAX_RESULTS",
    "FILE_REFERENCE_SECTION",
    "FILE_REFERENCE_SECTION_ORDER",
    "LocalFileReferenceService",
    "WorkspaceFileSearch",
    "install_file_reference_local",
    "resolve_search_config",
]

DEFAULT_FILE_SEARCH_MAX_RESULTS = 20
DEFAULT_FILE_SEARCH_MAX_ENTRIES = 50_000

#: 遍历/候选默认排除的目录 basename（search.ts:31-47；`lib` 故意不排除）。
DEFAULT_FILE_SEARCH_EXCLUDED_DIRECTORIES = (
    ".git", "node_modules", "dist", "build", "out", "coverage", "target",
    ".next", ".nuxt", ".turbo", ".venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".gradle",
)

FILE_REFERENCE_SECTION = "context:file-reference"
FILE_REFERENCE_SECTION_ORDER = 900


def resolve_search_config(config: dict | None = None) -> dict:
    config = dict(config or {})
    resolved = {
        "maxResults": config.get("maxResults", DEFAULT_FILE_SEARCH_MAX_RESULTS),
        "maxEntries": config.get("maxEntries", DEFAULT_FILE_SEARCH_MAX_ENTRIES),
        "excludedDirectories": list(
            config.get("excludedDirectories", DEFAULT_FILE_SEARCH_EXCLUDED_DIRECTORIES)),
    }
    for key in ("maxResults", "maxEntries"):
        value = resolved[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"file-reference-local: {key} must be a positive safe integer")
    for name in resolved["excludedDirectories"]:
        if not isinstance(name, str) or name == "" or "/" in name or "\\" in name:
            raise ValueError(
                "file-reference-local: excludedDirectories entries must be non-empty "
                "directory basenames")
    return resolved


def _read_directory(absolute: str) -> list:
    try:
        with os.scandir(absolute) as iterator:
            entries = [(entry.name, entry.is_dir(follow_symlinks=False),
                        entry.is_file(follow_symlinks=False)) for entry in iterator]
    except OSError:
        return []
    entries.sort(key=lambda item: item[0])
    return entries


def _read_workspace_root(absolute: str) -> list:
    with os.scandir(absolute) as iterator:
        entries = [(entry.name, entry.is_dir(follow_symlinks=False),
                    entry.is_file(follow_symlinks=False)) for entry in iterator]
    entries.sort(key=lambda item: item[0])
    return entries


def _resolve_display_directory(root: str, display: str) -> str | None:
    resolved_root = os.path.abspath(root)
    absolute = os.path.abspath(os.path.join(resolved_root, display or "."))
    from_root = os.path.relpath(absolute, resolved_root)
    if from_root == ".." or from_root.startswith(".." + os.sep):
        return None
    if os.path.isabs(from_root):
        return None
    current = resolved_root
    for segment in [part for part in from_root.split(os.sep) if part and part != "."]:
        current = os.path.join(current, segment)
        try:
            status = os.lstat(current)
        except OSError:
            return None
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            return None
    return absolute


def _visible_for_global_query(path: str, query: str) -> bool:
    if query.startswith(".") or "/." in query:
        return True
    return not any(segment.startswith(".") for segment in path.split("/"))


def _subsequence_score(target: str, query: str) -> int | None:
    target_index = 0
    gap = 0
    for character in query:
        found = target.find(character, target_index)
        if found < 0:
            return None
        gap += found - target_index
        target_index = found + 1
    return max(0, 100 - gap)


def _score_candidate(candidate: dict, query: str) -> int | None:
    if query == "":
        return 0
    path = candidate["path"].lower()
    name = path[path.rfind("/") + 1:]
    needle = query.lower()
    directory_bonus = 25 if candidate["kind"] == "directory" else 0
    if name == needle:
        return 1_000 + directory_bonus
    if name.startswith(needle):
        return 900 + directory_bonus
    if needle in name:
        return 700 + directory_bonus
    if needle in path:
        return 500 + directory_bonus
    subsequence = _subsequence_score(path, needle)
    return None if subsequence is None else 300 + subsequence + directory_bonus


def rank_candidates(candidates: list, query: str, limit: int) -> list:
    ranked = []
    for candidate in candidates:
        score = _score_candidate(candidate, query)
        if score is not None:
            ranked.append((score, candidate))
    ranked.sort(key=lambda item: (
        -item[0],
        0 if item[1]["kind"] == "directory" else 1,
        (len(item[1]["path"]) if query != "" else 0),
        item[1]["path"],
    ))
    return [candidate for _score, candidate in ranked[:limit]]


class WorkspaceFileSearch:
    """一个 agent 工作目录的路径索引与模糊补全（search.ts:84-255）。"""

    def __init__(self, root: str, config: dict):
        self.config = resolve_search_config(config)
        self.root = os.path.abspath(root)
        self._excluded = set(self.config["excludedDirectories"])
        self._settled: list | None = None
        self._invalidations = 0
        self._disposed = False

    def list(self, raw_query: str) -> list:
        if self._disposed:
            return []
        query = raw_query.replace("\\", "/")
        slash = query.rfind("/")
        if query == "" or slash >= 0:
            directory = "" if slash < 0 else query[:slash + 1]
            fragment = "" if slash < 0 else query[slash + 1:]
            return self._list_directory(directory, fragment)
        indexed = self._index_for()
        return rank_candidates(
            [candidate for candidate in indexed
             if _visible_for_global_query(candidate["path"], query)],
            query, self.config["maxResults"])

    def invalidate(self) -> None:
        self._invalidations += 1

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._settled = None

    def _index_for(self) -> list:
        if self._settled is None or self._settled[1] < self._invalidations:
            entries = self._scan_workspace()
            self._settled = [entries, self._invalidations]
        return self._settled[0]

    def _scan_workspace(self) -> list:
        indexed: list = []
        directories = [(self.root, "")]
        cursor = 0
        while cursor < len(directories) and len(indexed) < self.config["maxEntries"]:
            absolute, relative = directories[cursor]
            cursor += 1
            entries = (_read_workspace_root(absolute) if relative == ""
                       else _read_directory(absolute))
            for name, is_directory, is_file in entries:
                path = name if relative == "" else f"{relative}/{name}"
                if is_directory:
                    if name in self._excluded:
                        continue
                    indexed.append({"path": path, "kind": "directory"})
                    directories.append((os.path.join(absolute, name), path))
                elif is_file:
                    indexed.append({"path": path, "kind": "file"})
                if len(indexed) >= self.config["maxEntries"]:
                    break
        return indexed

    def _list_directory(self, display_directory: str, fragment: str) -> list:
        if any(segment in self._excluded for segment in display_directory.split("/")):
            return []
        absolute = _resolve_display_directory(self.root, display_directory)
        if absolute is None:
            return []
        candidates: list = []
        for name, is_directory, is_file in _read_directory(absolute):
            if name.startswith(".") and not fragment.startswith("."):
                continue
            if is_directory:
                if name in self._excluded:
                    continue
                candidates.append({"path": f"{display_directory}{name}", "kind": "directory"})
            elif is_file:
                candidates.append({"path": f"{display_directory}{name}", "kind": "file"})
        return rank_candidates(candidates, fragment, self.config["maxResults"])


class LocalFileReferenceService(FileReferenceService):
    """`ctx.fileReferences` 的本地实现（index.ts:44-127）。"""

    def __init__(self, ctx: Context, config: dict | None = None):
        super().__init__(ctx)
        self.config = resolve_search_config(config)
        self._searches: dict = {}
        self._prompt_fibers: dict = {}
        ctx.on("agent/created", self._on_agent_created)
        ctx.on("agent/disposed", self._on_agent_disposed)
        ctx.on("session/event", self._on_session_event, global_=True)
        ctx.effect(lambda: lambda: self._dispose_all(), "file-reference-local: search cache")
        agents = ctx.get("agents")
        if agents is not None:
            for agent in agents.list():
                self._install_prompt(agent)

    def list(self, agent, query: str, signal=None) -> list:
        search = self._searches.get(agent)
        if search is None:
            root = (getattr(agent.session, "meta", {}) or {}).get("cwd") or os.getcwd()
            search = WorkspaceFileSearch(root, self.config)
            self._searches[agent] = search
        return search.list(query)

    # ---------- 事件 ----------

    def _on_agent_created(self, payload: dict) -> None:
        agent = payload.get("agent")
        if agent is not None:
            self._install_prompt(agent)

    def _on_agent_disposed(self, payload: dict) -> None:
        agent = payload.get("agent")
        if agent is None:
            return
        search = self._searches.pop(agent, None)
        if search is not None:
            search.dispose()
        fiber = self._prompt_fibers.pop(agent, None)
        if fiber is not None:
            try:
                fiber.dispose()
            except Exception:  # noqa: BLE001 - 拆解失败仅告警
                logger = getattr(self.ctx, "logger", None)
                if logger is not None and hasattr(logger, "warn"):
                    logger.warn("file-reference-local: prompt cleanup failed")

    def _on_session_event(self, payload: dict) -> None:
        event = payload.get("event") or {}
        if event.get("type") != "tool/result":
            return
        session = payload.get("session")
        agents = self.ctx.get("agents")
        if session is None or agents is None:
            return
        agent = agents.get(session.session_id)
        if agent is not None and agent in self._searches:
            self._searches[agent].invalidate()

    # ---------- 提示词节 ----------

    def _install_prompt(self, agent) -> None:
        if agent in self._prompt_fibers:
            return
        agent_ctx = getattr(agent, "ctx", None)
        if agent_ctx is None:
            return

        def setup(scope):
            prompt = scope.get("systemPrompt")
            if prompt is None:
                return
            prompt.section(FILE_REFERENCE_SECTION, FILE_REFERENCE_SECTION_ORDER,
                           lambda context: self._prompt_text(context))

        try:
            fiber = agent_ctx.inject(["systemPrompt", "tools"], setup)
        except Exception:  # noqa: BLE001 - 依赖不满足时不注册节
            return
        self._prompt_fibers[agent] = fiber

    def _prompt_text(self, context: dict) -> str:
        agent = (context or {}).get("agent")
        if agent is None:
            return ""
        tools = agent.ctx.get("tools") if getattr(agent, "ctx", None) is not None else None
        if tools is None:
            return ""
        try:
            return FILE_REFERENCE_PROMPT if tools.resolve("read", agent.ctx) is not None else ""
        except Exception:  # noqa: BLE001 - 解析失败视为无 read 工具
            return ""

    def _dispose_all(self) -> None:
        for search in list(self._searches.values()):
            search.dispose()
        self._searches.clear()
        fibers = list(self._prompt_fibers.values())
        self._prompt_fibers.clear()
        for fiber in fibers:
            try:
                fiber.dispose()
            except Exception:  # noqa: BLE001 - 拆解失败仅告警
                pass


def install_file_reference_local(ctx: Context, config: dict | None = None) -> LocalFileReferenceService:
    """装配 `ctx.fileReferences` 的本地实现（幂等）。"""
    existing = ctx.get("fileReferences")
    if existing is not None:
        return existing
    return LocalFileReferenceService(ctx, config)
