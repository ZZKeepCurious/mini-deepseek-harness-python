"""第 2 章：插件上下文 + 事件总线 + 作用域化注册（Cordis fiber 对齐版）。

对应 dsh 真实源码：vendor/cordis（Context / EventsService / Fiber / registry）
+ packages/core/scope（createScope）。

四种派发模式：
  emit       观察式，不等待、无返回值，按注册序同步调用
  waterfall  流水线（around-middleware），必须 next() 委派，不调 next 即短路
  parallel   并行，等待全部监听完成（此处为同步近似）
  serial     串行，按序执行，有返回值

阶段 7 追加：aemit / awaterfall / aparallel / aserial —— 同一语义的 asyncio 版本。
监听器可以是普通同步函数，也可以是 async 函数；async 监听器会被 await，
同步监听器直接调用。事件循环取自调用点的 running loop，不私建 loop。

作用域（dsh-scope）：create_scope 铸一枚 fiber-backed 作用域子上下文，挂独立
scope_key；经父链继承依赖与服务。dispose 逆序回滚，会话管理用 owner scope
路由事件（祖先接收、旁支隔离）。

fiber（2026-08-20 对齐 vendor/cordis/src/fiber.ts 的 Phase 1 子集；设计解读见
教程第 2 章 2.3 节与 status/mini-harness/tasks.md）：
  * Fiber 生命周期状态机 PENDING/LOADING/ACTIVE/FAILED/UNLOADING/DISPOSED，
    每次转换派发 internal/status（payload {"fiber", "old"}）。
  * ctx.effect(execute, label) 对齐上游：execute 立即执行，返回值形态决定收集——
      None        → 无 disposer（上游语义）
      可调用对象  → 即 disposer，dispose 时逆序调用
      awaitable   → 异步 setup：结算时先完成 setup 再清理（setup barrier）
      同步迭代器  → generator effect，逐项 yield 注册（None 跳过，非可调用抛 TypeError）
      异步迭代器  → async generator effect，逐项 await 后注册
    execute 抛错或 generator yield 后抛错 → 已收集项逆序回滚后重抛。
  * 注册先于执行（wrapper 先入 fiber._disposables 再跑 body）→ setup 内部触发
    unload 时重入安全（拆解会先等 setup 完成再清理）。
  * 返回的 disposer 单发（二次调用 no-op，共享同一完成）且可 await（await 触发
    并等待结算）。
  * dispose 幂等；全同步 disposer 立即逆序执行（既有同步调用方零破坏）；含异步
    项则返回完成对象（有 running loop → task，否则交 __await__）。无运行 loop
    却遇到异步 disposer 时 fail loud（记录错误，不静默丢 async 结算）。
  * inertia = 在途 load/unload 转换；重复 dispose 返回在途完成（join）。

注意：effect() 调用约定是上游形态（execute = body，返回值收集为 disposer），
与早期 mini 的"effect(fn) 中 fn 即 disposer"相反——9 处调用点迁移与理由见
status/mini-harness/migration-log.md 与 AGENTS.md 差异清单。
"""
from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
from typing import Any, Awaitable, Callable

_logger = logging.getLogger(__name__)

INACTIVE_EFFECT = "INACTIVE_EFFECT"


class CordisError(Exception):
    """框架错误（对齐上游 CordisError）：code 稳定，缺省消息为 code 文案。"""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message if message is not None else code)
        self.code = code


class FiberState:
    """fiber 生命周期状态（对齐上游 FiberState 枚举；mini 用字符串便于测试/日志）。"""

    PENDING = "pending"
    LOADING = "loading"
    ACTIVE = "active"
    FAILED = "failed"
    UNLOADING = "unloading"
    DISPOSED = "disposed"


async def _maybe_await(value: Any) -> Any:
    """监听器返回值统一化：coroutine/awaitable 循环解包（中间件 return nxt() 会
    直接返回下一层 coroutine，须逐层 await 到普通值）。"""
    while inspect.iscoroutine(value) or isinstance(value, Awaitable):
        value = await value
    return value


def _is_awaitable(value: Any) -> bool:
    return inspect.isawaitable(value)


def _disposer_is_async(disposer: Any) -> bool:
    """估算 disposer 是否异步：async def 函数一定异步；嵌套 EffectDisposer 问其自身；
    纯 lambda 无法静态判断按同步处理（运行时若返回 awaitable 由调用方兜底报告）。"""
    if isinstance(disposer, EffectDisposer):
        return not disposer.is_sync()
    return inspect.iscoroutinefunction(disposer)


def _report_error(fiber: "Fiber", error: Exception) -> None:
    """contained 记录拆解错误：上游 dispose 路径用 Promise 收编不抛出，mini 落 fiber
    错误表并记日志（优先 ctx.logger，其次模块 logger）。"""
    fiber._errors.append(error)
    ctx = fiber.context
    logger = getattr(ctx, "logger", None)
    if logger is not None and callable(getattr(logger, "error", None)):
        logger.error(error)
    else:
        _logger.error("fiber %s teardown error: %s", fiber.name, error)


class EffectDisposer:
    """单发可 await 的副作用回滚器（对齐上游 effect() 的返回形态）。

    内部收集 body 产生的 disposer；调用一次后即进入结算，二次调用 no-op 且共享
    同一完成对象。全部同步 → 立即逆序结算并返回 None；含异步 → 返回可 await 的
    完成对象（有 running loop 则 task，否则 coroutine 交给 __await__）。
    """

    __slots__ = ("_fiber", "_label", "_disposables", "_disposed",
                 "_completion", "_pending_setup", "_executing", "_setup_barrier")

    def __init__(self, fiber: "Fiber", label: str):
        self._fiber = fiber
        self._label = label
        self._disposables: list[Callable] = []
        self._disposed = False
        self._completion: Any | None = None
        self._pending_setup: Any | None = None
        self._executing = True   # body 执行中（重入拆解需等 setup 完成，setup barrier）
        self._setup_barrier: asyncio.Event | None = None

    def is_sync(self) -> bool:
        return (self._pending_setup is None and not self._executing and all(
            not _disposer_is_async(d) for d in self._disposables))

    def needs_async(self) -> bool:
        """拆解是否需要走异步路径：setup 未完成（含重入）、或含异步 disposer。"""
        return (self._executing or self._pending_setup is not None or any(
            _disposer_is_async(d) for d in self._disposables))

    def _absorb(self, result: Any) -> None:
        """解释 body 返回值并收集 disposer（对齐上游 fiber.ts interpretEffect）。"""
        if result is None:
            return
        if callable(result):
            self._disposables.append(result)
            return
        if _is_awaitable(result):
            self._pending_setup = result
            return
        if inspect.isasyncgen(result):
            self._pending_setup = self._absorb_asyncgen(result)
            return
        if inspect.isgenerator(result):
            for item in result:
                if item is None:
                    continue
                if not callable(item):
                    raise TypeError("Invalid effect")
                self._disposables.append(item)
            return
        raise TypeError("Invalid effect")

    async def _absorb_asyncgen(self, agen: Any) -> None:
        async for item in agen:
            if item is None:
                continue
            if not callable(item):
                raise TypeError("Invalid effect")
            self._disposables.append(item)

    def _rollback(self) -> None:
        """body 抛错时回滚已收集项（同步尽力而为，不再二次上报）。"""
        for d in reversed(self._disposables):
            try:
                d()
            except Exception:
                pass
        self._disposables.clear()

    def _remove(self) -> None:
        fiber = self._fiber
        lst = fiber._disposables
        if lst and lst[-1] is self:
            lst.pop()
        else:
            try:
                lst.remove(self)
            except ValueError:
                pass

    def __call__(self) -> Any | None:
        if self._disposed:
            return self._completion
        self._disposed = True
        self._remove()
        if self._executing:
            # 重入拆解（body 执行中途触发 unload）：挂 setup barrier，
            # 先等 body 完成收集再清理（对齐上游 disposeAfter(waitForSetup())）
            if self._setup_barrier is None:
                self._setup_barrier = asyncio.Event()
            if self._pending_setup is None:
                self._pending_setup = self._setup_barrier.wait()
        if self.is_sync():
            self._run_sync()
            self._completion = None
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            # 无运行 loop：只能同步结算；async 内容 fail loud（记录错误）
            self._run_sync()
            self._completion = None
            return None
        coro = self._dispose_coro()
        task = loop.create_task(coro)
        self._completion = task
        return task

    def __await__(self):
        if not self._disposed:
            self()
        completion = self._completion
        if completion is not None:
            return (yield from completion.__await__())
        if False:
            yield
        return None

    def _run_sync(self) -> None:
        error = None
        for d in reversed(self._disposables):
            try:
                result = d()
            except Exception as exc:
                if error is None:
                    error = exc
                continue
            if _is_awaitable(result):
                if error is None:
                    error = RuntimeError(
                        "async disposer 在没有运行事件循环时被同步结算；"
                        "请 await 返回的完成对象")
        self._disposables.clear()
        if error is not None:
            _report_error(self._fiber, error)

    async def _dispose_coro(self) -> None:
        error = None
        if self._pending_setup is not None:
            pending, self._pending_setup = self._pending_setup, None
            try:
                await pending
            except Exception as exc:
                error = exc
        for d in reversed(self._disposables):
            try:
                result = d()
                if _is_awaitable(result):
                    await result
            except Exception as exc:
                if error is None:
                    error = exc
        self._disposables.clear()
        if error is not None:
            _report_error(self._fiber, error)


_FIBER_UID = itertools.count(1)


class Fiber:
    """插件/作用域的生命周期单元（对齐上游 vendor/cordis/src/fiber.ts）。

    持有本上下文注册的 effect 列表；dispose 逆序回滚。状态转换派发
    internal/status；异步拆解产生的错误 contained（记录不抛出）。
    """

    def __init__(self, uid: int, context: "Context | None", name: str,
                 is_root: bool = False):
        self.uid = uid
        self.context = context
        self.name = name
        self.state = FiberState.ACTIVE if is_root else FiberState.PENDING
        self.inertia: Any | None = None
        self._disposables: list[EffectDisposer] = []
        self._errors: list[Exception] = []

    def assert_active(self) -> None:
        if self.uid is None:
            raise CordisError(INACTIVE_EFFECT,
                              "cannot create effect on inactive context")

    def get_effects(self) -> list[str]:
        return [d._label for d in self._disposables]

    def effect(self, execute: Callable, label: str = "anonymous") -> EffectDisposer:
        """登记副作用：execute 立即执行，返回值按 _absorb 规则收集为 disposer。

        注册先于执行（重入安全）；body 抛错或返回值无效 → 撤下 wrapper 并回滚已
        收集项后重抛（对齐上游 fiber 的 setup 失败语义）。
        """
        self.assert_active()
        if self.state == FiberState.UNLOADING:
            raise CordisError(INACTIVE_EFFECT,
                              "cannot create effect on inactive context")
        disposer = EffectDisposer(self, label)
        self._disposables.append(disposer)
        try:
            result = execute()
        except Exception:
            disposer._executing = False
            if disposer._setup_barrier is not None:
                disposer._setup_barrier.set()
            self._remove_disposable(disposer)
            disposer._rollback()
            raise
        try:
            disposer._absorb(result)
        except Exception:
            self._remove_disposable(disposer)
            disposer._rollback()
            raise
        finally:
            disposer._executing = False
            if disposer._setup_barrier is not None:
                disposer._setup_barrier.set()
        return disposer

    def _remove_disposable(self, disposer: EffectDisposer) -> None:
        try:
            self._disposables.remove(disposer)
        except ValueError:
            pass

    def dispose(self) -> Any | None:
        """拆解：逆序回滚全部 effect。幂等；重复调用 join 在途完成。

        全同步 → 立即结算返回 None；含异步且存在 running loop → 返回 task；
        无 loop 而含异步 → fail loud（记录错误）后返回 None。
        """
        if self.uid is None:
            return self.inertia
        self.uid = None
        self._set_state(FiberState.UNLOADING, old=self.state)
        disposables, self._disposables = self._disposables, []
        if not disposables:
            self._set_state(FiberState.DISPOSED, old=self.state)
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            error = None
            for d in reversed(disposables):
                try:
                    result = d()
                except Exception as exc:
                    if error is None:
                        error = exc
                    continue
                if _is_awaitable(result):
                    if error is None:
                        error = RuntimeError(
                            "async disposer 需在运行事件循环中结算；请 await 返回的完成对象")
            self._set_state(FiberState.DISPOSED, old=self.state)
            if error is not None:
                _report_error(self, error)
            return None
        if all(isinstance(d, EffectDisposer) and not d.needs_async()
               for d in disposables):
            for d in reversed(disposables):
                try:
                    d()
                except Exception as exc:
                    _report_error(self, exc)
            self._set_state(FiberState.DISPOSED, old=self.state)
            return None
        coro = self._unload_async(disposables)
        task = loop.create_task(coro)
        self.inertia = task
        return task

    async def _unload_async(self, disposables: list) -> None:
        async def _run(d: Any) -> Exception | None:
            try:
                result = d()
                if _is_awaitable(result):
                    await result
            except Exception as exc:
                return exc
            return None

        results = await asyncio.gather(*(_run(d) for d in reversed(disposables)))
        self._set_state(FiberState.DISPOSED, old=self.state)
        for exc in results:
            if exc is not None:
                _report_error(self, exc)

    def _set_state(self, state: str, old: str) -> None:
        self.state = state
        if self.context is not None:
            self.context.emit("internal/status",
                              {"fiber": self, "old": old})


class Context:
    """服务仓库 + 事件总线 + 可逆副作用容器（fiber 生命周期承载）。"""

    def __init__(self, parent: "Context | None" = None, name: str = "root",
                 *, _fiber: Fiber | None = None):
        self.parent = parent
        self.name = name
        self._services: dict[str, Any] = {}
        self._listeners: dict[str, list[Callable]] = {}
        self._scope_key: Any = None  # create_scope 打标的身份键（对齐上游 dsh-scope ScopeKey）
        if _fiber is not None:
            self.fiber = _fiber
        elif parent is None:
            self.fiber = Fiber(0, self, name, is_root=True)
        else:
            # 普通子上下文共享父 fiber（mini 实际只用 create_scope 建子上下文）
            self.fiber = parent.fiber

    @property
    def scope_key(self) -> Any:
        """作用域身份键：None = 根/无标号上下文；create_scope 产物有独立对象键。"""
        return self._scope_key

    def is_scope(self) -> bool:
        return self._scope_key is not None

    def _assert_alive(self) -> None:
        if self.fiber.uid is None or self.fiber.state in (
                FiberState.DISPOSED, FiberState.UNLOADING):
            raise RuntimeError(f"上下文 {self.name} 已销毁，拒绝注册")

    # ---------- 服务（Service） ----------

    def provide(self, key: str, value: Any) -> EffectDisposer:
        """提供服务，返回 disposer。同 key 重复提供 = 冲突（fail loud）。"""
        self._assert_alive()
        if key in self._services:
            raise RuntimeError(f"服务 {key} 已在 {self.name} 提供")
        self._services[key] = value
        return self.effect(
            lambda: (lambda: self._services.pop(key, None)),
            f"ctx.provide({key})",
        )

    def inject(self, key: str) -> Any:
        """按 key 查找服务：沿父子链向上（作用域可见性）。"""
        if key in self._services:
            return self._services[key]
        if self.parent is not None:
            return self.parent.inject(key)
        raise KeyError(f"服务 {key} 未提供")

    # ---------- 事件派发（四种模式） ----------

    def on(self, event: str, fn: Callable) -> EffectDisposer:
        """注册监听器，返回 disposer。"""
        self._assert_alive()
        self._listeners.setdefault(event, []).append(fn)

        def disposer() -> None:
            lst = self._listeners.get(event)
            if lst and fn in lst:
                lst.remove(fn)

        return self.effect(lambda: disposer, f"ctx.on({event})")

    def _listeners_for(self, event: str) -> list[Callable]:
        """收集自身 + 祖先链的监听器（子先于父，各层保持注册序）。"""
        chain: list[Callable] = []
        node: Context | None = self
        while node is not None:
            chain = list(node._listeners.get(event, [])) + chain
            node = node.parent
        return chain

    def emit(self, event: str, payload: Any = None) -> None:
        for fn in self._listeners_for(event):
            fn(payload)

    def waterfall(self, event: str, payload: Any = None) -> Any:
        """around-middleware：监听器签名 fn(payload, next)。
        调用 next(new) 继续下一位；不调用即短路，当前返回值就是最终决策。"""
        listeners = self._listeners_for(event)
        idx = 0

        def step(cur: Any) -> Any:
            nonlocal idx
            if idx >= len(listeners):
                return cur
            fn = listeners[idx]
            idx += 1
            return fn(cur, lambda new=cur: step(new))

        return step(payload)

    def parallel(self, event: str, payload: Any = None) -> list:
        return [fn(payload) for fn in self._listeners_for(event)]

    def serial(self, event: str, payload: Any = None) -> list:
        return [fn(payload) for fn in self._listeners_for(event)]

    # ---------- 阶段 7：asyncio 变体 ----------

    async def aemit(self, event: str, payload: Any = None) -> None:
        """观察式异步派发：按注册序 await 每个监听器（async 监听器 await，同步直调）。"""
        for fn in self._listeners_for(event):
            await _maybe_await(fn(payload))

    async def awaterfall(self, event: str, payload: Any = None) -> Any:
        """流水线异步版：语义与 waterfall 相同（next() 委派、不调即短路）。"""
        listeners = self._listeners_for(event)
        idx = 0

        async def step(cur: Any) -> Any:
            nonlocal idx
            if idx >= len(listeners):
                return cur
            fn = listeners[idx]
            idx += 1
            result = fn(cur, lambda new=cur: step(new))
            return await _maybe_await(result)

        return await step(payload)

    async def aparallel(self, event: str, payload: Any = None) -> list:
        """并行异步版：全部监听器并发启动（asyncio.gather），结果按注册序返回。"""
        coros = []
        for fn in self._listeners_for(event):
            result = fn(payload)
            if inspect.iscoroutine(result):
                coros.append(result)
            else:
                async def _const(value=result):
                    return value
                coros.append(_const())
        return await asyncio.gather(*coros)

    async def aserial(self, event: str, payload: Any = None) -> list:
        """串行异步版：语义与 serial 相同（按注册序执行，async 监听器 await）。"""
        results = []
        for fn in self._listeners_for(event):
            results.append(await _maybe_await(fn(payload)))
        return results

    # ---------- 副作用与生命周期 ----------

    def effect(self, execute: Callable, label: str = "anonymous") -> EffectDisposer:
        """对齐上游 ctx.effect：execute 立即执行，返回值按形态收集为 disposer。

        注意与早期 mini 的"effect(fn) 中 fn 即 disposer"语义相反；调用点已迁移
        （见 AGENTS.md 差异清单与 migration-log.md）。
        """
        self._assert_alive()
        return self.fiber.effect(execute, label)

    def dispose(self) -> Any | None:
        """拆解：fiber 逆序回滚全部注册；幂等、可 await（同步 disposer 立即执行）。"""
        if self.fiber.context is not self:
            raise RuntimeError(f"上下文 {self.name} 不拥有其 fiber，无法拆解")
        return self.fiber.dispose()

    # 便利方法
    def create_scope(self, name: str) -> "Context":
        """创建 fiber-backed 作用域子上下文（对齐上游 dsh-scope createScope）。

        子上下文挂独立身份键（scope_key）+ 独立 fiber，经父链继承依赖与服务；
        该 fiber 的拆解注册为父 fiber 的一个 effect → 父拆解时按序收回全部作用域。
        dispose 幂等、可 await、竞态共享同一完成。
        """
        fiber = Fiber(next(_FIBER_UID), None, name)
        child = Context(parent=self, name=name, _fiber=fiber)
        child._scope_key = object()
        fiber.context = child
        fiber._set_state(FiberState.LOADING, old=FiberState.PENDING)
        fiber._set_state(FiberState.ACTIVE, old=fiber.state)
        self.fiber.effect(lambda: (lambda: fiber.dispose()),
                          f"create_scope({name})")
        return child


class PluginManager:
    """依赖驱动的插件激活：inject 满足才 apply，全部激活或明确报错。

    简化说明：真实 Cordis 由 apply 期间的 provide/effect 动态登记依赖，
    这里用声明式 provides 字段近似（见教程第 2 章 2.3 节）。
    """

    def __init__(self, root: Context):
        self.root = root

    def activate(self, plugins: list[dict]) -> list[tuple[str, Callable]]:
        remaining = [dict(p) for p in plugins]
        provided = set(self.root._services)
        done: list[tuple[str, Callable]] = []
        while remaining:
            progressed = False
            for p in list(remaining):
                if all(k in provided for k in p.get("inject", [])):
                    snapshot = len(self.root.fiber._disposables)
                    p["apply"](self.root)
                    disposer = self._collect_after(snapshot)
                    done.append((p["name"], disposer))
                    provided.update(p.get("provides", []))
                    remaining.remove(p)
                    progressed = True
            if not progressed:
                raise RuntimeError(
                    "插件依赖无法满足（可能循环或缺失）: " + ", ".join(p["name"] for p in remaining)
                )
        return done

    def _collect_after(self, snapshot: int) -> Callable:
        added = self.root.fiber._disposables[snapshot:]

        def dispose() -> None:
            for fn in reversed(added):
                fn()

        return dispose