"""第 2 章：插件上下文 + 事件总线 + 作用域化注册（Cordis fiber 对齐版）。

对应 dsh 真实源码：vendor/cordis（Context / EventsService / Fiber / registry /
reflect）+ packages/core/scope（createScope）。

四种派发模式：
  emit       观察式，不等待、无返回值，按注册序同步调用
  waterfall  流水线（around-middleware），必须 next() 委派，不调 next 即短路
  parallel   并行，等待全部监听完成（此处为同步近似）
  serial     串行，按序执行，有返回值

阶段 7 追加：aemit / awaterfall / aparallel / aserial —— 同一语义的 asyncio 版本。
监听器可以是普通同步函数，也可以是 async 函数；async 监听器会被 await，
同步监听器直接调用。事件循环取自调用点的 running loop，不私建 loop。

作用域（dsh-scope）：create_scope 铸一枚 fiber-backed 作用域子上下文，挂独立
scope_key；dispose 逆序回滚，会话管理用 owner scope 路由事件。简化标注：上游
作用域 fiber 都是 root 子节点，作用域父子关系走独立的 scopeParents 图 +
scopeTarget 事件载波；mini 用上下文树近似（作用域 fiber 挂在调用方下，
_listeners_for 祖先链即作用域链，事件上溯、旁支隔离）。

fiber（2026-08-20 对齐 vendor/cordis/src/fiber.ts）：
  * Phase 1 —— 卸载半边：生命周期状态机 PENDING/LOADING/ACTIVE/FAILED/
    UNLOADING/DISPOSED + internal/status；ctx.effect(execute, label) 上游语义
    （execute 立即执行，返回值按形态收集为 disposer）；setup barrier 重入保护；
    disposer 单发可 await；错误 contained。
  * Phase 2 —— 装载半边 + 注册表（registry.ts / fiber.ts _setEpoch 对齐）：
    RegistryService（ctx.plugin / ctx.inject 缩写 / 插件形态归一 / 运行记录按
    callback 键控）；Fiber 携带 inject 依赖 + _runner 装载期（epoch/execute/
    collect）：PENDING（依赖缺失）→ LOADING → ACTIVE（依赖满足即装载），依赖
    变化触发 epoch 重载（卸载→重装）；restart()/update()（internal/update
    waterfall）；internal/config waterfall + Config schema 校验（resolve_config）；
    每次装载/卸载派发 internal/plugin；dispose 经父 fiber 的 "ctx.plugin()"
    effect 归位（退运行记录 + 排水在途转换）。同步 body 同步激活（同步门面零
    破坏）；异步 body 返回待结算 coroutine，由 wait()/瞬态事件循环排空。

服务（Service，reflect.ts 对齐）：ctx.provide(key, value) 登记实现，同一隔离
标签下重复提供 fail loud（对齐上游已注册检查）；ctx.isolate(name) 新建子上下文
并为该 name 分配新标签（同一标签传给两次 isolate 则共享作用域），同名不同标签
提供互不冲突（per-agent 的 tools/systemPrompt 经此隔离，对齐上游 agent scope
realm——进程级 root realm 发布冲突被拒，见 preset/mount.ts）。实现存根级全局
store，按 (name 的隔离标签) 键控；_label_of 沿祖先链解析最近标签，否则用根
默认标签（原型继承等价）。服务查找为 ctx.get(name)（对齐上游 get：strict 只
返回提供者 fiber 处于 ACTIVE 的实现，缺省返回 None，不再抛 KeyError）。依赖
满足/解除经 _notify 遍历注册表全部 fiber（对齐 reflect.notify，通知按标签过滤
依赖者）；服务可见性在 ACTIVE↔非 ACTIVE 转换时广播（对齐 fiber._updateState
的 notify 段）。

服务基类（2026-08-20 Phase 3 对齐 vendor/cordis/src/service.ts）：class Service
子类构造时即经 ctx.provide 自动登记，随 fiber 自动注销；定义 _invoke 方法则该
服务可调用（如 ctx.logger(name)）；_check 提供可用性谓词、_init 构造后运行
（对齐 [init]）。intercept（对齐 context.ts）：ctx.intercept(name, config) 返回
子上下文，携带该服务的一条 intercept 配置；Service._resolve_config 沿祖先链
合并（近根者优先，base 前置、head 后置），供插件下方装载的子插件取用。

日志（2026-08-20 Phase 3 对齐 vendor/cordis/src/logger.ts）：ctx.logger 内建
服务（LoggerService extends Service，可调用）：ctx.logger(name) 铸具名 Logger
门面（error/info/warn/debug，printf 风格 %s %d %f %o %c %%）；门面本身按
exporter.levels[name]/default/自身 level 过滤；exporter 注册即 effect，默认
缓冲导出器（bufferSize 1000）。LoggerService.error(...) 等直接以当前 fiber 名
（hyphenate）记录。

注意：effect() 调用约定是上游形态（execute = body，返回值收集为 disposer），
与早期 mini 的"effect(fn) 中 fn 即 disposer"相反——调用点迁移与理由见
status/mini-harness/migration-log.md 与 AGENTS.md 差异清单。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from typing import Any, Awaitable, Callable

from .schema import resolve_config

_logger = logging.getLogger(__name__)

INACTIVE_EFFECT = "INACTIVE_EFFECT"
INACTIVE = "__INACTIVE__"


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


class Fiber:
    """插件/作用域的生命周期单元（对齐上游 vendor/cordis/src/fiber.ts）。

    持有本上下文注册的 effect 列表；dispose 逆序回滚。状态转换派发
    internal/status；异步拆解产生的错误 contained（记录不抛出）。

    Phase 2 起：插件 fiber 携带 inject 依赖 + 装载期 _runner（epoch），
    依赖满足才装载（PENDING→LOADING→ACTIVE），依赖变化触发 epoch 重载。
    """

    def __init__(self, parent: "Context | None", config: Any = None,
                 inject: dict | None = None, runtime: dict | None = None, *,
                 name: str = "anonymous", is_root: bool = False):
        self.uid: int | None = 0 if is_root else parent.registry.counter
        self.runtime = runtime          # 插件运行记录（root 为 None）
        self.inject: dict = inject or {}
        self.config: Any = None         # 校验后的配置
        self._config: Any = config      # 原始配置（每次重载前重解析）
        self.store: dict | None = None  # 装载期依赖快照
        self._store: dict = {}
        self._error: Exception | None = None
        self.state = FiberState.ACTIVE if is_root else FiberState.PENDING
        self.inertia: Any | None = None
        self._disposables: list = []
        self._errors: list = []
        self._loading = False
        self._unloading = False
        self._runner: dict = {
            "epoch": "" if is_root else INACTIVE,
            "execute": (lambda: None) if is_root else self._body_call,
            "collect": self._collect_disposer,
            "get_outer_stack": (lambda: []),
        }
        if is_root:
            self.context = parent
            self.name = name
            self.store = {}
        else:
            self.name = name
            self.context = Context(parent=parent, name=name, _fiber=self)
            self._register_plugin(parent)
            self.context.emit("internal/plugin", self)
            if parent.fiber.state != FiberState.UNLOADING:
                for service in list(self.inject):
                    self._check_impl(service)
                self._refresh()

    # ---------- 装载半边（registry.ts / fiber.ts 对齐） ----------

    def _body_call(self) -> Any:
        """运行插件 body：函数直接调用；类实例化后取 init() 结果（对齐上游
        isConstructor 分支），均以 (ctx, config) 传入。"""
        callback = self.runtime["callback"]
        if inspect.isclass(callback):
            instance = callback(self.context, self.config)
            init = getattr(instance, "init", None)
            return init() if callable(init) else None
        return callback(self.context, self.config)

    def _collect_disposer(self, dispose: Any) -> None:
        if dispose is None:
            return
        if callable(dispose):
            self._disposables.append(dispose)
            return
        raise TypeError("Invalid effect")

    def _register_plugin(self, parent: "Context") -> None:
        """把本 fiber 挂到父 fiber 的 effect 上（对齐上游 'ctx.plugin()' disposer）：
        注册期 push 进运行记录；父拆解时归位（退注册表 + 排水在途转换）。"""
        parent.effect(lambda: self._make_plugin_disposer(), "ctx.plugin()")

    def _make_plugin_disposer(self) -> Callable:
        runtime = self.runtime
        runtime["fibers"].append(self)

        def disposer() -> Any:
            return self._plugin_dispose()
        return disposer

    def _plugin_dispose(self) -> Any:
        """卸载插件 fiber：退注册表 + 置 INACTIVE + 排水在途转换。幂等。"""
        if self.uid is None:
            return self.inertia
        self.uid = None
        self._emit_plugin_disposed()
        runtime = self.runtime
        if self in runtime["fibers"]:
            runtime["fibers"].remove(self)
        if not runtime["fibers"]:
            self.context.registry.delete(runtime["callback"])
        self._set_epoch(INACTIVE)
        if self.state not in (FiberState.UNLOADING, FiberState.DISPOSED):
            self._begin_unload()
        return self.inertia

    def _emit_plugin_disposed(self) -> None:
        try:
            self.context.emit("internal/plugin", self)
        except Exception as exc:
            _report_error(self, exc)

    def _find_impl(self, name: str) -> dict | None:
        """按本 fiber 上下文解析的隔离标签读全局 store 的实现记录（对齐上游
        reflect._getImpl：同一隔离作用域即共享，isolate() 遮蔽换标签）。"""
        return self.context._find_impl(name)

    def _check_impl(self, name: str) -> None:
        """检查依赖可用性（提供者须 ACTIVE + 可选 check 谓词），更新装载期快照。"""
        impl = self._find_impl(name)
        if impl is None or impl["fiber"].state != FiberState.ACTIVE:
            self._store.pop(name, None)
            return
        check = impl["check"]
        if check is not None:
            try:
                if not check():
                    self._store.pop(name, None)
                    return
            except Exception as exc:
                _report_error(self, exc)
                self._store.pop(name, None)
                return
        self._store[name] = impl

    def _refresh(self) -> None:
        """重算 epoch（依赖提供者 uid 串）；变化触发 _set_epoch。"""
        epoch = ""
        for name in self.inject:
            impl = self._store.get(name)
            if impl is None:
                epoch = INACTIVE
                break
            provider = impl["fiber"]
            epoch += ":" + str(provider.uid if provider is not None else 0)
        self._set_epoch(epoch)

    def _set_epoch(self, epoch: str) -> None:
        old = self._runner["epoch"]
        if epoch == old:
            return
        self._runner["epoch"] = epoch
        if self.inertia is not None or self._loading or self._unloading:
            return
        if epoch != INACTIVE and old == INACTIVE:
            self._begin_load()
        else:
            self._begin_unload()

    def _begin_load(self) -> None:
        if self._loading:
            return
        self._update_state(lambda: FiberState.LOADING)
        self._loading = True
        try:
            pending = self._reload()
        finally:
            self._loading = False
        if pending is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            self.inertia = pending if loop is None else loop.create_task(pending)

    def _begin_unload(self) -> None:
        if self._unloading:
            return
        self._update_state(lambda: FiberState.UNLOADING)
        self._unloading = True
        try:
            pending = self._unload()
        finally:
            self._unloading = False
        if pending is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            self.inertia = pending if loop is None else loop.create_task(pending)

    def _reload(self) -> Any:
        """装载：解析 config → 运行 body 收集 disposer。同步 body 同步完成；
        异步 body 返回待结算 coroutine（state 停留 LOADING 直至排空）。"""
        self.store = dict(self._store)
        old_epoch = self._runner["epoch"]
        try:
            self.config = self._resolve_config(self._config)
            result = self._execute(self._runner, old_epoch)
            self._error = None
        except Exception as reason:
            _logger.error("plugin %s load failed: %s", self.name, reason)
            self._error = reason
            self._runner["epoch"] = INACTIVE
            # 对齐上游：装载失败仍逆序清掉已收集 disposer（终态落 FAILED）
            self._begin_unload()
            return None
        if _is_awaitable(result):
            return self._finalize_load_async(result, old_epoch)
        if self._runner["epoch"] != old_epoch:
            self._begin_unload()
        else:
            self._update_state(lambda: FiberState.ACTIVE)
        return None

    async def _finalize_load_async(self, awaitable: Any, old_epoch: str) -> None:
        try:
            await awaitable
            self._error = None
            if self._runner["epoch"] != old_epoch:
                self._begin_unload()
            else:
                self._update_state(lambda: FiberState.ACTIVE)
                self.inertia = None
        except Exception as reason:
            _logger.error("plugin %s load failed: %s", self.name, reason)
            self._error = reason
            self._runner["epoch"] = INACTIVE
            self._begin_unload()

    def _execute(self, runner: dict, old_epoch: str | None = None) -> Any:
        """运行 body 并按其返回值形态收集（对齐上游 fiber.ts _execute 派发）：
        callable 优先于 awaitable（EffectDisposer 同时可调用与可 await）。"""
        result = runner["execute"]()
        if result is None:
            return None
        if callable(result):
            runner["collect"](result)
            return None
        if inspect.isgenerator(result):
            for item in result:
                runner["collect"](item)
            return None
        if _is_awaitable(result):
            return self._await_collect(result)
        if inspect.isasyncgen(result):
            return self._asyncgen_collect(result, old_epoch)
        raise TypeError("Invalid effect")

    async def _await_collect(self, awaitable: Any) -> None:
        result = await awaitable
        self._collect_disposer(result)

    async def _asyncgen_collect(self, agen: Any, old_epoch: str | None) -> None:
        async for item in agen:
            if old_epoch is not None and self._runner["epoch"] != old_epoch:
                return
            self._collect_disposer(item)

    def _resolve_config(self, config: Any) -> Any:
        """internal/config waterfall → Config schema 校验（对齐上游）。"""
        listeners = self.context._listeners_for("internal/config")
        idx = 0

        def step(cur: Any) -> Any:
            nonlocal idx
            if idx >= len(listeners):
                return cur
            fn = listeners[idx]
            idx += 1
            return fn(self, cur, lambda new=cur: step(new))

        config = step(config)
        if self.runtime is not None:
            schema = self.runtime.get("Config")
            if schema is not None:
                return resolve_config(schema, config)
        return config

    def _update_waterfall(self, config: Any, no_save: bool,
                          default_action: Callable) -> Any:
        """internal/update waterfall：监听器可否决/替换重载（对齐上游）。"""
        listeners = self.context._listeners_for("internal/update")
        idx = 0

        def step(cur: Any) -> Any:
            nonlocal idx
            if idx >= len(listeners):
                return default_action()
            fn = listeners[idx]
            idx += 1
            return fn(self, cur, no_save, lambda new=cur: step(new))

        return step(config)

    def _unload(self) -> Any:
        """卸载：逆序运行全部 disposer，错误 contained。之后按 epoch 决定是否
        立即重装（epoch 重载链）。含异步项且存在 loop → 返回待结算 coroutine。"""
        disposables, self._disposables = self._disposables, []
        async_needed = any(
            (isinstance(d, EffectDisposer) and d.needs_async()) or
            (not isinstance(d, EffectDisposer) and _disposer_is_async(d))
            for d in disposables)
        if async_needed:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                return self._unload_async(disposables)
        errors = self._run_disposers_sync(disposables)
        self.store = None
        self._update_state(lambda: None)
        if self._runner["epoch"] != INACTIVE:
            self._begin_load()
        for exc in errors:
            _report_error(self, exc)
        return None

    def _run_disposers_sync(self, disposables: list) -> list:
        errors = []
        for d in reversed(disposables):
            try:
                result = d()
            except Exception as exc:
                errors.append(exc)
                continue
            if _is_awaitable(result):
                errors.append(RuntimeError(
                    "async disposer 在没有运行事件循环时被同步结算；请 await"))
        return errors

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
        self.store = None
        self._update_state(lambda: None)
        if self._runner["epoch"] != INACTIVE:
            self._begin_load()
        for exc in results:
            if exc is not None:
                _report_error(self, exc)

    def _get_state(self) -> str:
        if self.uid is None:
            return FiberState.DISPOSED
        if self._error is not None:
            return FiberState.FAILED
        if self._runner["epoch"] != INACTIVE:
            return FiberState.ACTIVE
        return FiberState.PENDING

    def _update_state(self, transition: Callable) -> None:
        old = self.state
        self.state = transition() or self._get_state()
        if old == self.state:
            return
        self.context.emit("internal/status", {"fiber": self, "old": old})
        # 对齐上游 fiber._updateState：ACTIVE 与非 ACTIVE 互转时通知本 fiber
        # 提供的服务（依赖者据此 _check_impl → epoch 重载/卸载）
        if (old == FiberState.ACTIVE) != (self.state == FiberState.ACTIVE):
            names_labels = [
                (impl["name"], self.context._label_of(impl["name"]))
                for impl in self.context.root._reflect_store.values()
                if impl["fiber"] is self
            ]
            if names_labels:
                self.context.root._notify(names_labels)

    # ---------- 公共 API ----------

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
        """拆解：root fiber 逆序回滚全部 effect；插件 fiber 走 plugin disposer
        （退注册表 + 排水在途转换）。幂等；重复调用 join 在途完成。"""
        if self.runtime is None:
            return self._root_dispose()
        return self._plugin_dispose()

    def _root_dispose(self) -> Any | None:
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

    async def wait(self) -> "Fiber":
        """等待当前生命周期工作停稳并重抛装载错误（对齐上游 fiber.await()）。"""
        while self.inertia is not None:
            await self.inertia
        if self._error is not None:
            raise self._error
        return self

    def restart(self) -> Any:
        """卸载并立即以当前 config 重载（对齐上游 fiber.restart()）。"""
        self.assert_active()
        self._set_epoch(INACTIVE)
        self._refresh()
        return self.wait()

    def update(self, config: Any, no_save: bool = False) -> Any:
        """校验并应用新 config，再重载插件（对齐上游 fiber.update()）。"""
        self.assert_active()
        self._config = config
        if self.state != FiberState.ACTIVE:
            self._error = None
            self._set_epoch(INACTIVE)
            self._refresh()
            return
        config = self._resolve_config(config)

        def default_action() -> Any:
            self.config = config
            self._error = None
            return self.restart()

        return self._update_waterfall(config, no_save, default_action)

    def _set_state(self, state: str, old: str) -> None:
        self.state = state
        if self.context is not None:
            self.context.emit("internal/status",
                              {"fiber": self, "old": old})


class Inject:
    """插件依赖声明归一化（对齐 vendor/cordis/src/registry.ts Inject.resolve）。

    数组 → {name: None, ...}；字典 → {name: config|None, ...}。
    （上游还支持类继承链的 inject 元数据，mini 以平面数组/字典为准。）
    """

    @staticmethod
    def resolve(inject: Any, result: dict | None = None) -> dict:
        result = result if result is not None else {}
        if not inject:
            return result
        if isinstance(inject, (list, tuple)):
            for name in inject:
                result[name] = None
        elif isinstance(inject, dict):
            for name, value in inject.items():
                result[name] = value if value is not None else None
        else:
            raise TypeError("invalid inject declaration")
        return result


def _scope_noop(ctx: "Context", config: Any = None) -> None:
    """create_scope 的背衬 no-op 插件（对齐上游 createScope = ctx.plugin(scope)）。"""
    return None


class RegistryService:
    """插件注册表 + 动态依赖激活（对齐 vendor/cordis/src/registry.ts）。

    以插件 callback 为身份键维护运行记录 {name, callback, fibers, Config}；
    ctx.plugin(plugin, config) 铸造新 fiber 并复用运行记录；所有 scope 共享
    no-op 插件的运行记录（每作用域一 fiber）。
    """

    def __init__(self, ctx: "Context"):
        self.ctx = ctx
        self._counter = 0
        self._internal: dict[Callable, dict] = {}

    @property
    def counter(self) -> int:
        """分配下一个 fiber uid（每次读取自增，对齐上游）。"""
        self._counter += 1
        return self._counter

    @property
    def size(self) -> int:
        return len(self._internal)

    @staticmethod
    def _plugin_attr(plugin: Any, name: str) -> Any:
        """dict 形态插件（mini boot 组合契约）与对象插件统一取属性。"""
        if isinstance(plugin, dict):
            return plugin.get(name)
        return getattr(plugin, name, None)

    def resolve(self, plugin: Any) -> Callable | None:
        """插件形态归一：函数 / 类 / {apply} 对象 → 可执行 callback。"""
        if inspect.isfunction(plugin) or inspect.isclass(plugin):
            return plugin
        apply = self._plugin_attr(plugin, "apply")
        return apply if callable(apply) else None

    def get(self, plugin: Any) -> dict | None:
        key = self.resolve(plugin)
        return self._internal.get(key) if key is not None else None

    def has(self, plugin: Any) -> bool:
        key = self.resolve(plugin)
        return key is not None and key in self._internal

    def delete(self, plugin: Any) -> dict | None:
        key = self.resolve(plugin)
        runtime = self._internal.get(key) if key is not None else None
        if runtime is None:
            return None
        self._internal.pop(key)
        for fiber in list(runtime["fibers"]):
            fiber.dispose()
        return runtime

    def keys(self):
        return iter(self._internal.keys())

    def values(self):
        return iter(self._internal.values())

    def entries(self):
        return iter(self._internal.items())

    def forEach(self, callback: Callable) -> None:
        for key, runtime in self._internal.items():
            callback(runtime, key)

    def inject(self, deps: Any, callback: Callable) -> "Fiber":
        """ctx.inject(deps, cb) 缩写：依赖满足即装载（对齐上游 registry.inject）。"""
        return self.plugin({
            "name": getattr(callback, "__name__", "anonymous"),
            "inject": deps,
            "apply": callback,
        })

    def plugin(self, plugin: Any, config: Any = None, *,
               parent: "Context | None" = None) -> "Fiber":
        """装载插件并返回其 fiber。依赖缺失 → PENDING；满足 → 装载（可能同步
        ACTIVE）；异步 body 需 wait()/瞬态事件循环排空。

        parent 仅供 create_scope 使用（mini 用上下文树近似上游 scopeParents
        图：作用域 fiber 挂在调用方下，祖先链即作用域链）。
        """
        callback = self.resolve(plugin)
        if callback is None:
            raise ValueError(
                'invalid plugin, expect function or object with an "apply" method, '
                f"received {type(plugin).__name__}")
        parent = parent if parent is not None else self.ctx
        parent.fiber.assert_active()
        runtime = self._internal.get(callback)
        if runtime is None:
            name = self._plugin_attr(plugin, "name")
            if name == "apply":
                name = None
            runtime = {
                "name": name,
                "callback": callback,
                "fibers": [],
                "Config": self._plugin_attr(plugin, "Config"),
            }
            self._internal[callback] = runtime
        inject = Inject.resolve(self._plugin_attr(plugin, "inject"))
        name = (self._plugin_attr(plugin, "name") or runtime["name"]
                or getattr(callback, "__name__", "plugin"))
        return Fiber(parent, config, inject, runtime, name=name)


class Service:
    """服务基类（对齐 vendor/cordis/src/service.ts）。

    子类构造时调用 super().__init__(ctx, name)：立即经 ctx.provide(name, self)
    登记，随拥有 fiber 自动注销。子类可定义：
      _invoke   —— 实例可调用（如 ctx.logger(name) 铸子 Logger）
      _check    —— 可用性谓词，透传给 provide（对齐 [check]）
      _init     —— 构造后运行（类插件场景，对齐 [init]）
    provide 类属性为缺省服务名（name 参数缺省时取用）。
    """

    provide: str | None = None

    def __init__(self, ctx: "Context", name: str | None = None):
        name = name or type(self).provide
        if name is None:
            raise TypeError("service must declare a name")
        self.ctx = ctx
        self.name = name
        ctx.provide(name, self, getattr(self, "_check", None))
        init = getattr(self, "_init", None)
        if callable(init):
            init()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        invoke = getattr(self, "_invoke", None)
        if invoke is None:
            raise TypeError(f"service {self.name!r} is not callable")
        return invoke(*args, **kwargs)

    def _resolve_config(self, base: Any = None, head: Any = None,
                        ctx: "Context | None" = None) -> dict:
        """合并本服务祖先链的 intercept 配置（近根者优先；base 前置、head
        后置），对齐 service.ts 的 [resolveConfig]（浅合并）。ctx 指定解析
        上下文（日志 invoke 以访问方 ctx 解析），缺省用 self.ctx。"""
        ctx = ctx or self.ctx
        configs = list(ctx._resolve_intercept(self.name))
        if base is not None:
            configs.insert(0, base)
        if head is not None:
            configs.append(head)
        merged: dict = {}
        for config in configs:
            if config:
                merged.update(config)
        return merged


def _hyphenate(name: str) -> str:
    """camelCase / snake_case / 空格 → kebab-case（对齐 cosmokit hyphenate）。"""
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    return name.replace("_", "-").replace(" ", "-").lower()


class Logger:
    """具名日志门面（对齐 vendor/cordis/src/logger.ts 的 Logger 类）。"""

    def __init__(self, options: dict, service: "LoggerService"):
        self.name: str = options["name"]
        self.meta: dict | None = options.get("meta")
        self.level: int | None = options.get("level")
        self.service = service

    def error(self, *args: Any) -> None:
        self._log("error", 0, args)

    def info(self, *args: Any) -> None:
        self._log("info", 1, args)

    def warn(self, *args: Any) -> None:
        self._log("warn", 2, args)

    def debug(self, *args: Any) -> None:
        self._log("debug", 3, args)

    def _log(self, type_: str, level: int, args: tuple) -> None:
        if len(args) == 1 and isinstance(args[0], BaseException):
            if args[0].__cause__ is not None:
                getattr(self, type_)(args[0].__cause__)
                return
            errors = getattr(args[0], "errors", None)
            if isinstance(errors, (list, tuple)):
                for error in errors:
                    getattr(self, type_)(error)
                return
        sn = self.service._sn_message
        self.service._sn_message += 1
        ts = int(time.time() * 1000)
        message: dict = {
            "sn": sn, "ts": ts, "type": type_, "level": level,
            "name": self.name, "args": args,
        }
        if self.meta:
            message.update(self.meta)
        for exporter in list(self.service.exporters.values()):
            target = (exporter.get("levels") or {}).get(
                self.name, (exporter.get("levels") or {}).get("default",
                                                              self.level if self.level is not None else 1))
            if target < level:
                continue
            exporter["export"](message)


class LoggerService(Service):
    """内建日志服务（对齐 vendor/cordis/src/logger.ts LoggerService）。

    可调用：ctx.get("logger")("name") 铸具名 Logger；ctx.get("logger").warn(...)
    以当前 fiber 名（hyphenate）记录。默认缓冲导出器（bufferSize 1000）。
    """

    provide = "logger"
    buffer_size = 1000

    def __init__(self, ctx: "Context"):
        self.buffer: list[dict] = []
        self.exporters: dict[int, dict] = {}
        self._sn_message = 0
        self._sn_exporter = 0
        super().__init__(ctx, "logger")
        self.exporter({"colors": 3, "export": self._buffer_append})

    def _buffer_append(self, message: dict) -> None:
        self.buffer.append(message)
        if len(self.buffer) > self.buffer_size:
            self.buffer = self.buffer[-self.buffer_size:]

    def exporter(self, exporter: dict) -> EffectDisposer:
        """注册导出器，随当前 fiber 注销（对齐 ctx.logger.exporter()）。"""
        def register() -> Callable:
            self._sn_exporter += 1
            self.exporters[self._sn_exporter] = exporter

            def disposer() -> None:
                self.exporters.pop(self._sn_exporter, None)

            return disposer

        return self.ctx.effect(register, "ctx.logger.exporter()")

    def _invoke(self, name: str | None = None,
                ctx: "Context | None" = None) -> Logger:
        ctx = ctx or self.ctx
        config = self._resolve_config(ctx=ctx)
        fiber = ctx.fiber
        name = name or config.get("name") or _hyphenate(fiber.name)
        return Logger({
            "name": name,
            "level": config.get("level"),
            "meta": {"fiber": fiber},
        }, self)

    def error(self, *args: Any) -> None:
        self().error(*args)

    def info(self, *args: Any) -> None:
        self().info(*args)

    def warn(self, *args: Any) -> None:
        self().warn(*args)

    def debug(self, *args: Any) -> None:
        self().debug(*args)


class _LoggerView:
    """ctx.logger 属性视图：绑定访问上下文，intercept 从该上下文解析（对齐上游
    traceable ctx.logger——invoke 以访问方 ctx 解析 [resolveConfig]，而非服务
    构造时的根 ctx）。保持可调用 + error/info/warn/debug + exporter/buffer 面。"""

    def __init__(self, service: LoggerService, ctx: "Context"):
        self._service = service
        self._ctx = ctx

    def __call__(self, name: str | None = None) -> Logger:
        return self._service._invoke(name, self._ctx)

    def error(self, *args: Any) -> None:
        self().error(*args)

    def info(self, *args: Any) -> None:
        self().info(*args)

    def warn(self, *args: Any) -> None:
        self().warn(*args)

    def debug(self, *args: Any) -> None:
        self().debug(*args)

    def exporter(self, exporter: dict) -> EffectDisposer:
        return self._service.exporter(exporter)

    @property
    def buffer(self) -> list[dict]:
        return self._service.buffer


class Context:
    """服务仓库 + 事件总线 + 可逆副作用容器（fiber 生命周期承载）。"""

    def __init__(self, parent: "Context | None" = None, name: str = "root",
                 *, _fiber: Fiber | None = None):
        self.parent = parent
        self.name = name
        self._listeners: dict[str, list[Callable]] = {}
        self._isolate: dict[str, object] = {}   # 隔离标签：name → label（仅本节点的遮蔽）
        self._intercept: dict[str, Any] = {}    # intercept 配置：name → config（仅本节点的条目）
        self._scope_key: Any = None  # create_scope 打标的身份键（对齐上游 dsh-scope ScopeKey）
        if _fiber is not None:
            self.fiber = _fiber
        elif parent is None:
            self.fiber = Fiber(self, name=name, is_root=True)
            self._registry = RegistryService(self)
            self._reflect_store: dict[object, dict] = {}   # 全局服务实现表（按 label 键控）
            self._reflect_labels: dict[str, object] = {}   # name → 默认 label（上游 root.isolate）
            LoggerService(self)
        else:
            # 普通子上下文共享父 fiber（mini 实际只用 create_scope/plugin 建子上下文）
            self.fiber = parent.fiber

    @property
    def root(self) -> "Context":
        node = self
        while node.parent is not None:
            node = node.parent
        return node

    @property
    def registry(self) -> RegistryService:
        return self.root._registry

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

    # ---------- 服务（Service，reflect.ts 对齐：隔离标签键控的全局 store） ----------

    def _label_of(self, name: str) -> object:
        """解析 name 的隔离标签：沿祖先链找最近的 _isolate 遮蔽，否则用根的默认
        标签（对齐上游 ctx[symbols.isolate][name]，原型继承 + isolate() 遮蔽）。"""
        node: Context | None = self
        while node is not None:
            lab = node._isolate.get(name)
            if lab is not None:
                return lab
            node = node.parent
        root = self.root
        lab = root._reflect_labels.get(name)
        if lab is None:
            lab = object()
            root._reflect_labels[name] = lab
        return lab

    def _find_impl(self, key: str) -> dict | None:
        """按当前上下文的隔离标签读全局 store 中的实现记录（对齐上游 _getImpl）。"""
        label = self._label_of(key)
        root = self.root
        return root._reflect_store.get(label)

    def isolate(self, name: str, label: object | None = None) -> "Context":
        """创建子上下文：name 服务在新标签下解析，不再命中父的作用域（对齐上游
        ctx.isolate()）。同一 label 传给两次 isolate 则共享同一作用域。
        返回共享父 fiber 的裸子上下文（mini 的 create_scope 另有 fiber 与事件层）。
        """
        child = self.extend(name=f"iso:{name}")
        child._isolate[name] = label if label is not None else object()
        return child

    def extend(self, meta: dict | None = None, *, name: str = "child") -> "Context":
        """创建共享本 fiber 的裸子上下文，携带给定自有属性（对齐上游
        ctx.extend(meta)：父上下文不被修改，子上下文原型继承——mini 以父链近似，
        事件经 _listeners_for 上溯，隔离标签经 _label_of 上溯）。"""
        child = Context(parent=self, name=name, _fiber=self.fiber)
        for key, value in (meta or {}).items():
            setattr(child, key, value)
        return child

    def intercept(self, name: str, config: Any) -> "Context":
        """返回子上下文：在其下方装载的插件可见服务 name 的额外 intercept
        配置（对齐上游 ctx.intercept(name, config)）。父上下文不被修改。"""
        child = self.extend(name=f"intercept:{name}")
        child._intercept = {name: config}
        return child

    def _resolve_intercept(self, name: str) -> list:
        """收集 name 的祖先链 intercept 配置（近根者优先），对齐 service.ts
        [resolveConfig] 的 prototype 链走查（own 条目 unshift 语义）。"""
        nodes: list[Context] = []
        node: Context | None = self
        while node is not None:
            nodes.append(node)
            node = node.parent
        configs: list = []
        for node in reversed(nodes):
            config = node.__dict__.get("_intercept")
            if config and name in config:
                configs.append(config[name])
        return configs

    @property
    def logger(self) -> "_LoggerView | None":
        """内建日志服务（对齐上游 ctx.logger 属性访问；返回绑定本上下文的视图）。
        可写：显式赋值（含 None）遮蔽服务视图（测试替身 / 宿主覆写场景）。"""
        if "logger" in self.__dict__:
            return self.__dict__["logger"]
        service = self.get("logger")
        if service is None:
            return None
        return _LoggerView(service, self)

    @logger.setter
    def logger(self, value: Any) -> None:
        self.__dict__["logger"] = value

    def provide(self, key: str, value: Any, check: Callable | None = None) -> EffectDisposer:
        """提供服务，返回 disposer。同一标签下重复提供 = 冲突（fail loud，对齐上游
        已注册检查）；不同标签同名提供 = 隔离作用域（isolate() 遮蔽）。登记后通知
        依赖该服务的 PENDING fiber；提供者未 ACTIVE 时延迟到 ACTIVE 转换再通知
        （对齐 reflect 通知时机，notify 按标签过滤）。
        """
        self._assert_alive()
        label = self._label_of(key)
        root = self.root
        if label in root._reflect_store:
            raise RuntimeError(
                f'service "{key}" has been registered at <'
                f'{root._reflect_store[label]["fiber"].name}>')
        root._reflect_store[label] = {
            "name": key, "value": value, "fiber": self.fiber, "check": check,
        }
        if self.fiber.state == FiberState.ACTIVE:
            root._notify([(key, label)])

        def disposer() -> None:
            if root._reflect_store.get(label) is not None:
                root._reflect_store.pop(label, None)
                root._notify([(key, label)])

        return self.effect(lambda: disposer, f"ctx.provide({key})")

    def get(self, key: str, strict: bool = True) -> Any:
        """按 key 读服务（对齐上游 reflect.get 语义）：缺省 strict 只返回提供者
        fiber 处于 ACTIVE 的实现；未提供 → None（不再抛 KeyError）。"""
        impl = self._find_impl(key)
        if impl is None:
            return None
        if strict and impl["fiber"].state != FiberState.ACTIVE:
            return None
        return impl["value"]

    def set(self, key: str, value: Any) -> bool:
        """覆写已提供服务（仅提供者 fiber 可 set；对齐上游 reflect.set）。"""
        impl = self._find_impl(key)
        if impl is None:
            raise RuntimeError(f'cannot set property "{key}" without provide')
        if impl["fiber"] is not self.fiber:
            raise RuntimeError(f'cannot set property "{key}" in multiple fibers')
        impl["value"] = value
        return True

    def _notify(self, names_labels: list[tuple[str, object]]) -> None:
        """服务变化 → 重估依赖它的 fiber（对齐上游 reflect.notify：遍历注册表
        全部 fiber，仅命中 inject 声明且隔离标签一致的依赖者，_check_impl +
        _refresh）。"""
        for runtime in self.registry.values():
            for fiber in list(runtime["fibers"]):
                has_update = False
                for name, label in names_labels:
                    if name not in fiber.inject:
                        continue
                    if fiber.context._label_of(name) != label:
                        continue  # 隔离标签不一致（上游 notify 的 filter）
                    fiber._check_impl(name)
                    has_update = True
                if has_update:
                    fiber._refresh()

    # ---------- 插件（registry） ----------

    def plugin(self, plugin: Any, config: Any = None) -> Fiber:
        """装载插件（对齐上游 ctx.plugin）。"""
        return self.registry.plugin(plugin, config)

    def inject(self, deps: Any, callback: Callable) -> Fiber:
        """ctx.inject(deps, cb) 缩写（对齐上游）：依赖满足即装载。"""
        return self.registry.inject(deps, callback)

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

    def create_scope(self, name: str) -> "Context":
        """创建 fiber-backed 作用域子上下文（对齐上游 dsh-scope createScope）。

        底层经 ctx.plugin(noop) 铸造一枚独立 fiber：该 fiber 的拆解自动成为
        父 fiber 的一个 effect（'ctx.plugin()'）→ 父拆解时按序收回全部作用域。
        dispose 幂等、可 await、竞态共享同一完成。

        简化标注：上游作用域 fiber 都是 root 子节点，作用域父子关系走独立的
        scopeParents 图 + scopeTarget 事件载波；mini 用上下文树近似（作用域
        fiber 挂在调用方下，_listeners_for 祖先链即作用域链，事件上溯）。
        """
        fiber = self.registry.plugin({"name": name, "apply": _scope_noop}, parent=self)
        child = fiber.context
        child._scope_key = object()
        return child