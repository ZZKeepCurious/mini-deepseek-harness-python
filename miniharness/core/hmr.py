"""HMR 配置热重载命令面 —— watch 文件变更 → 单飞刷新循环 → epoch 重载触发源。

对应 dsh 真实源码：vendor/hmr/src/index.ts 的 registerConfig / refreshConfig
（配置文件热重载切片）+ packages/boot/app-boot/src/index.ts watchUserPatches
（用户补丁层 watch，见 boot.boot.watch_user_patches）。

上游语义（已核实，vendor/hmr/src/index.ts:134-187, 297-324）：
  * registerConfig(filename, refresh)：解析到 baseDir 下；findWatchRoot 自
    filename 逐级上溯到第一个存在的目录（realpath 规范化，depth 记步数）；
    同一路径重复注册 fail loud；watch 根目录、事件按精确路径过滤
    （add/change/unlink 三类都触发刷新）；注册返回经 ctx.effect 包裹的
    disposer——注销登记、关闭 watcher、等待在飞刷新结束。
  * refreshConfig：每登记一份 {dirty} 状态；触发时置 dirty，若无在飞刷新
    则启动单飞任务：do { dirty=false; 刷新 } while (dirty)——刷新期间到达
    的多次变更折叠为恰好一次补跑；刷新抛错 → logger.warn + 经
    'hmr/config-update-failed' 并行事件外泄（监听器拒绝只 warn，不毒化
    后续热重载），循环继续消费 dirty。
  * ignoreInitial=false：已存在的目标文件在初始扫描期发 'add' → 注册即
    触发一次刷新。
  * 服务销毁：关闭全部 watcher、等齐全部在飞刷新（allSettled 语义）。

载体简化（须在文档标注）：①上游 HMR 全服务含 Node ESM 模块图热重载
（ModuleLoader/externals/accepted/declined/stash），Python 无模块重载语义，
架构不适用，不复现；②文件 watch 载体 chokidar→watchdog（黄金法则 #4），
watchdog 无初始扫描与 depth 上限——"已存在文件注册即刷"由显式初扫复现，
递归 watch 以精确路径过滤兜底；③上游异步单飞为 Promise 链，mini 用后台
线程承载（阻塞的刷新期间 dirty 照常累积），disposer join 在飞线程。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .scope import Context, CordisError, FiberState, INACTIVE_EFFECT, Service

__all__ = ["Hmr", "find_watch_root", "CONFIG_UPDATE_FAILED"]

#: 刷新失败外泄事件名（上游 'hmr/config-update-failed'，index.ts:29）
CONFIG_UPDATE_FAILED = "hmr/config-update-failed"


def _event_key(path: str) -> str:
    """事件路径与登记键的统一规范化：realpath 消解符号链接/短路径 + 大小写
    归一（Windows 观察器回报长路径而宿主可能持短路径）。"""
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError:
        return os.path.normcase(os.path.abspath(path))


def find_watch_root(filename: str) -> tuple[str, str, int]:
    """自 filename 上溯到第一个存在的目录；返回 (规范文件路径, 规范根, 步数)。

    对齐上游 findWatchRoot（index.ts:107-125）：目录不存在继续上溯；
    realpath 消解符号链接后把 filename 重定基到规范根；抵达文件系统根仍
    找不到任何存在目录 → 抛 FileNotFoundError。
    """
    path = Path(filename).absolute()
    root = path.parent
    depth = 0
    while True:
        if root.exists():
            if not root.is_dir():
                raise NotADirectoryError(f"config watch parent is not a directory: {root}")
            canonical_root = str(Path(os.path.realpath(root)))
            # 把 filename 重定基到规范根（对齐上游 resolve(canonicalRoot, relative(root, filename))）
            canonical_name = str(Path(canonical_root) / os.path.relpath(path, root))
            return canonical_name, canonical_root, depth
        parent = root.parent
        if parent == root:
            raise FileNotFoundError(f"no existing directory above {filename}")
        root = parent
        depth += 1


class _RefreshState:
    """单份登记的刷新状态（上游 ConfigRefresh {dirty, running?}）。"""

    def __init__(self) -> None:
        self.dirty = False
        self.running: threading.Thread | None = None


class _WatchHandle:
    """一次登记的 watch 载体：真实 Observer 或测试注入的假柄。"""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _ExactPathHandler(FileSystemEventHandler):
    """watchdog 事件 → 精确路径过滤 → 刷新触发（上游 onChange，index.ts:151-155）。"""

    def __init__(self, hmr: "Hmr", targets: frozenset[str]) -> None:
        super().__init__()
        self._hmr = hmr
        self._targets = targets

    @staticmethod
    def _paths_of(event) -> tuple[str, str]:
        """取事件的 src/dest 路径（测试可传 {"src_path": ...} 形态的假事件）。"""
        if isinstance(event, dict):
            return event.get("dest_path", "") or "", event.get("src_path", "") or ""
        dest = getattr(event, "dest_path", "") or ""
        src = getattr(event, "src_path", "") or ""
        return dest, src

    def _maybe(self, event: FileSystemEvent) -> None:
        for raw in self._paths_of(event):
            if raw and _event_key(raw) in self._targets:
                self._hmr.on_file_event(raw)
                return

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._maybe(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._maybe(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._maybe(event)


class Hmr(Service):
    """HMR 配置热重载服务（ctx.get('hmr')；上游 vendor/hmr 的 Hmr 类）。

    internals 注入钩子（测试载体）：watcher_factory(root, handler) → 带
    .close() 的柄；缺省用 watchdog Observer 递归 watch 根目录。服务销毁
    （拥有 fiber dispose）时关闭全部 watcher 并等齐在飞刷新。
    """

    provide = "hmr"

    def __init__(self, ctx: Context, base_dir: str = ".",
                 internals: dict | None = None):
        super().__init__(ctx)
        self.base_dir = os.path.abspath(base_dir)
        self._watcher_factory = (internals or {}).get("watcher_factory") or self._default_watcher
        # registration key(规范化路径) → (handle, refresh, state, filename)
        self._configs: dict[str, tuple] = {}
        self._lock = threading.Lock()
        # 同上：execute 立即执行，返回 _teardown_all 方法本身作为销毁期回调
        ctx.effect(lambda: self._teardown_all, "hmr")

    # ---------- 注册面 ----------

    def register_config(self, filename: str,
                        refresh: Callable[[], Any]) -> Callable[[], None]:
        """watch 一个文件，变更时经单飞循环调用 refresh；返回同步 disposer。

        对齐上游 registerConfig（index.ts:134-187）：重复注册同一路径
        fail loud；已存在的目标文件注册即触发一次刷新（ignoreInitial=false
        语义）；disposer 注销登记 + 关闭 watcher + 等待在飞刷新结束。
        """
        resolved = os.path.abspath(os.path.join(self.base_dir, filename))
        watch_filename, root, _depth = find_watch_root(resolved)
        targets = frozenset({_event_key(resolved), _event_key(watch_filename)})
        with self._lock:
            if watch_filename in self._configs:
                raise RuntimeError(f"config path already registered: {resolved}")
            handler = _ExactPathHandler(self, targets)
            handle = self._watcher_factory(root, lambda ev: handler._maybe(ev))
            state = _RefreshState()
            self._configs[watch_filename] = (handle, refresh, state, resolved)
        # 初扫：已存在的目标文件触发首次刷新（chokidar 'add' 语义）
        if os.path.exists(resolved) or os.path.exists(watch_filename):
            self.refresh_config(watch_filename)
        # ctx.effect(execute) 立即执行 execute，返回值才是 disposer（上游
        # `ctx.effect(() => async () => {...})` 同构）；宿主销毁期的注册失败
        # 归一为上游 INACTIVE_EFFECT 语义（watchUserPatches index.ts:262 豁免）
        try:
            return self.ctx.effect(
                lambda: lambda: self._dispose_registration(watch_filename),
                "hmr.registerConfig()")
        except RuntimeError as error:
            fiber = self.ctx.fiber
            if fiber.uid is None or fiber.state in (FiberState.DISPOSED, FiberState.UNLOADING):
                raise CordisError(INACTIVE_EFFECT, str(error)) from error
            raise

    def on_file_event(self, observed: str) -> None:
        """watcher 回调入口：按登记键路由到刷新循环。"""
        key = _event_key(observed)
        for registered, (_handle, _refresh, _state, filename) in list(self._configs.items()):
            if key in (_event_key(registered), _event_key(filename)):
                self.refresh_config(registered)
                return

    # ---------- 单飞 + dirty 合并（上游 refreshConfig，index.ts:297-324） ----------

    def refresh_config(self, key: str) -> threading.Thread | None:
        with self._lock:
            entry = self._configs.get(key)
            if entry is None:
                return None
            _handle, _refresh, state, _filename = entry
            state.dirty = True
            if state.running is not None and state.running.is_alive():
                return None
            worker = threading.Thread(
                target=self._refresh_loop, args=(key,), daemon=True,
                name=f"hmr-refresh-{os.path.basename(key)}")
            state.running = worker
            # 锁内 start：避免 dispose 在 start 前 join 未启动线程的竞态
            worker.start()
        return worker

    def _refresh_loop(self, key: str) -> None:
        entry = self._configs.get(key)
        if entry is None:
            return
        _handle, refresh, state, filename = entry
        while True:
            state.dirty = False
            try:
                refresh()
            except BaseException as reason:  # noqa: BLE001 - 外泄后循环必须存活
                error = reason if isinstance(reason, Exception) else RuntimeError(str(reason))
                logger = self.ctx.logger("hmr") if self.ctx.logger is not None else None
                if logger is not None:
                    logger.warn(f"config reload at {filename} failed")
                    logger.warn(str(error))
                try:
                    self.ctx.parallel(CONFIG_UPDATE_FAILED, {"filename": filename, "error": error})
                except Exception as rejection:  # noqa: BLE001 - 监听器拒绝只 warn
                    if logger is not None:
                        logger.warn(str(rejection))
            if not state.dirty:
                return

    # ---------- 销毁面 ----------

    def _dispose_registration(self, key: str) -> None:
        with self._lock:
            entry = self._configs.pop(key, None)
        if entry is None:
            return
        handle, _refresh, state, _filename = entry
        handle.close()
        worker = state.running
        if worker is not None:
            worker.join()

    def _teardown_all(self) -> None:
        with self._lock:
            entries = list(self._configs.values())
            self._configs.clear()
        for handle, _refresh, state, _filename in entries:
            handle.close()
        for _handle, _refresh, state, _filename in entries:
            if state.running is not None:
                state.running.join()

    @staticmethod
    def _default_watcher(root: str, on_event: Callable[[FileSystemEvent], None]) -> Any:
        observer = Observer()
        observer.schedule(_RawForwarder(on_event), root, recursive=True)
        observer.daemon = True
        observer.start()
        return _ObserverHandle(observer)


class _RawForwarder(FileSystemEventHandler):
    def __init__(self, sink: Callable[[FileSystemEvent], None]) -> None:
        super().__init__()
        self._sink = sink

    def on_any_event(self, event: FileSystemEvent) -> None:
        if not event.is_directory and not getattr(event, "is_synthetic", False):
            self._sink(event)


class _ObserverHandle(_WatchHandle):
    def __init__(self, observer: Observer) -> None:
        super().__init__()
        self._observer = observer

    def close(self) -> None:
        if not self.closed:
            self._observer.stop()
            self._observer.join(timeout=5.0)
        super().close()
