"""watchdog-based skill file watcher（对齐上游 SkillWatchManager）。

上游对照：packages/skill/skill-filesystem/src/index.ts SkillWatchManager。

契约（与上游语义一致）：
  * per-root watcher 监听 add/addDir/change/unlink/unlinkDir 五类事件
  * 事件过滤（isRelevantWatchEvent）：
    - 根目录本身：仅 addDir/unlinkDir 相关
    - 根下一级：.md 文件 + 目录（含 .system 跳过逻辑）
    - 根下二级：仅 SKILL.md（排除 addDir/unlinkDir）
  * 去抖 invalidation：queue_microtask 级别去抖（对齐 queueMicrotask）
  * 根不存在时监听父目录（对齐 upstream watchFile probe）
  * LRU 上限（watchMaxProjects，默认 128）

mini 简化（有意保留，须在文档标注）：
  * 无 awaitWriteFinish 稳定阈值（watchdog 的 polling 模式可按需配置，
    但默认不开启）；上游默认 200ms 稳定阈值
  * 无 fs/observed 事件桥（mini 工具不产出 fs/observed 事件）
  * 无 unhealthy root rewatch（根删除后 watcher 标记停止，下次 list()
    重新探测时恢复）
"""
from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

if TYPE_CHECKING:
    pass

logger = logging.getLogger("miniharness.skills.watcher")

DEFAULT_WATCH_STABILITY_THRESHOLD_MS = 200
DEFAULT_WATCH_POLL_INTERVAL_MS = 100
DEFAULT_WATCH_MAX_PROJECTS = 128


# ---------- 事件过滤（对齐 isRelevantWatchEvent） ----------

def _contained_segments(root_path: str, event_path: str) -> list[str] | None:
    """返回 event_path 相对于 root_path 的路径段；不在 root 下返回 None。"""
    try:
        rel = os.path.relpath(event_path, root_path)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    if rel == ".":
        return []
    parts = Path(rel).parts
    return list(parts)


def is_relevant_watch_event(
    root_path: str,
    event_type: str,
    event_path: str,
    skip_system: bool = False,
) -> bool:
    """判断 watchdog 事件是否与 skill 目录变更相关（对齐 isRelevantWatchEvent）。

    过滤规则（对齐上游 index.ts:658-675）：
      * 路径在 root 外 → False
      * 路径 == root → 仅 addDir/unlinkDir（根目录创建/删除）
      * 跳过 .system 子目录
      * 根下一级：目录 always True；文件仅 .md
      * 根下二级：仅 SKILL.md 文件（排除目录事件）
      * 更深层 → False
    """
    segments = _contained_segments(root_path, event_path)
    if segments is None:
        return False
    if len(segments) == 0:
        return event_type in ("created", "deleted") and _is_dir(event_path)
    if skip_system and segments[0] == ".system":
        return False
    if len(segments) == 1:
        is_directory = _is_dir(event_path)
        if event_type in ("created", "deleted") and is_directory:
            return True
        return segments[0].endswith(".md") and not is_directory
    if len(segments) == 2 and segments[1] == "SKILL.md":
        return not _is_dir(event_path)
    return False


def _is_dir(path: str) -> bool:
    try:
        return os.path.isdir(path)
    except OSError:
        return False


# ---------- 去抖器 ----------

class _Debouncer:
    """轻量去抖：多次 call 合并为一次回调（对齐 queueMicrotask 去抖）。"""

    def __init__(self, callback: Callable[[], None], delay: float = 0.01) -> None:
        self._callback = callback
        self._delay = delay
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def schedule(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
        try:
            self._callback()
        except Exception:
            logger.exception("skill watcher invalidation callback failed")

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


# ---------- 根 watcher 状态 ----------

class _RootState:
    """单个 root 的 watcher 状态。"""

    __slots__ = ("root_path", "source", "skip_system", "observer", "healthy")

    def __init__(
        self,
        root_path: str,
        source: str,
        skip_system: bool = False,
    ) -> None:
        self.root_path = root_path
        self.source = source
        self.skip_system = skip_system
        self.observer: Observer | None = None
        self.healthy = True

    def stop(self) -> None:
        if self.observer is not None:
            self.observer.stop()
            self.observer.join(timeout=5.0)
            self.observer = None


# ---------- 事件处理器 ----------

class _SkillEventHandler(FileSystemEventHandler):
    """watchdog 事件处理器：过滤 + 去抖 → invalidate 回调。"""

    def __init__(
        self,
        root_path: str,
        skip_system: bool,
        on_invalidated: Callable[[], None],
    ) -> None:
        super().__init__()
        self._root_path = root_path
        self._skip_system = skip_system
        self._on_invalidated = on_invalidated

    def on_any_event(self, event: FileSystemEvent) -> None:
        if getattr(event, "is_synthetic", False):
            return
        event_type = event.event_type
        # 对于 moved 事件，检查 src（deleted）和 dest（created）
        if event_type == "moved":
            src_relevant = is_relevant_watch_event(
                self._root_path, "deleted", event.src_path, self._skip_system,
            )
            dest_relevant = is_relevant_watch_event(
                self._root_path, "created", event.dest_path, self._skip_system,
            )
            if not src_relevant and not dest_relevant:
                return
        else:
            path = event.src_path
            if not is_relevant_watch_event(self._root_path, event_type, path, self._skip_system):
                return
        self._on_invalidated()


# ---------- SkillWatchManager ----------

class SkillWatchManager:
    """watchdog-based skill file watcher（对齐上游 SkillWatchManager）。

    配置键（对齐上游 Config）：watch / watchUsePolling /
    watchStabilityThresholdMs / watchPollIntervalMs / watchMaxProjects /
    watchFollowSymlinks。

    mini 简化：无 awaitWriteFinish；无 fs/observed 桥；
    无 unhealthy root rewatch（根删除后标记停止，下次 list() 恢复）。
    """

    def __init__(
        self,
        invalidate_callback: Callable[[], None],
        config: dict | None = None,
    ) -> None:
        config = config or {}
        self._invalidate_callback = invalidate_callback
        self._enabled = config.get("watch", True)
        self._use_polling = config.get("watchUsePolling", False)
        self._max_projects = config.get("watchMaxProjects", DEFAULT_WATCH_MAX_PROJECTS)
        self._follow_symlinks = config.get("watchFollowSymlinks", True)
        self._poll_interval = config.get("watchPollIntervalMs", DEFAULT_WATCH_POLL_INTERVAL_MS) / 1000.0

        self._roots: OrderedDict[str, _RootState] = OrderedDict()
        self._lock = threading.Lock()
        self._debouncer = _Debouncer(self._do_invalidate)
        self._closing = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def update_roots(self, roots: list[dict]) -> None:
        """同步 root 列表：新增的开启 watcher，消失的停止 watcher。

        roots 格式与 FileSystemSkillProvider._roots() 返回一致：
        [{path, source, skip_system?}, ...]
        """
        if not self._enabled or self._closing:
            return
        wanted: dict[str, dict] = {}
        for root in roots:
            path = root["path"]
            wanted[path] = root

        with self._lock:
            # 停止不再需要的 roots
            stale = [p for p in self._roots if p not in wanted]
            for p in stale:
                state = self._roots.pop(p)
                state.stop()
                logger.debug("skill watcher: stopped root %s (%s)", p, state.source)

            # 新增需要的 roots
            for path, root in wanted.items():
                if path not in self._roots:
                    self._maybe_evict()
                    self._start_root(root)

    def _start_root(self, root: dict) -> None:
        """为单个 root 开启 watcher（目录不存在时监听父目录）。"""
        path = root["path"]
        source = root.get("source", "unknown")
        skip_system = root.get("skip_system", False)

        watch_path = path
        if not os.path.isdir(path):
            # 根目录不存在 → 监听父目录（对齐 upstream watchFile probe）
            parent = os.path.dirname(path)
            if not os.path.isdir(parent):
                logger.debug("skill watcher: root %s parent %s also missing, skip", path, parent)
                return
            watch_path = parent

        state = _RootState(path, source, skip_system)
        handler = _SkillEventHandler(path, skip_system, self._schedule_invalidation)
        observer = Observer()
        try:
            observer.schedule(handler, watch_path, recursive=True)
        except Exception:
            logger.warning("skill watcher: failed to watch %s for root %s", watch_path, path)
            return
        observer.daemon = True
        observer.start()
        state.observer = observer
        self._roots[path] = state
        logger.debug("skill watcher: watching root %s via %s (%s)", path, watch_path, source)

    def _maybe_evict(self) -> None:
        """LRU 淘汰最旧的 root（调用方须持锁）。"""
        while len(self._roots) >= self._max_projects:
            _oldest_path, oldest_state = self._roots.popitem(last=False)
            oldest_state.stop()
            logger.debug("skill watcher: evicted root %s (LRU)", _oldest_path)

    def _schedule_invalidation(self) -> None:
        """去抖 invalidation（对齐 queueMicrotask 级别去抖）。"""
        if self._closing:
            return
        self._debouncer.schedule()

    def _do_invalidate(self) -> None:
        """实际执行 invalidation 回调。"""
        if self._closing:
            return
        try:
            self._invalidate_callback()
        except Exception:
            logger.exception("skill watcher: invalidate callback failed")

    def dispose(self) -> None:
        """停止所有 watcher。"""
        self._closing = True
        self._debouncer.cancel()
        with self._lock:
            for state in self._roots.values():
                state.stop()
            self._roots.clear()
