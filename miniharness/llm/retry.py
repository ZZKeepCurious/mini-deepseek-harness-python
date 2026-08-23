"""第 4 章：模型请求重试执行器 —— provider 路由的失败恢复。

对应 dsh 真实源码：packages/llm/llm-retry/src/index.ts（+ history.ts）。

上游语义（已核实，llm-retry/src/index.ts）：
  * 挂在 agent/request-error waterfall 扩展点：监听器返回 {kind:'retry'}
    且不调 next() = 自己接管恢复；调 next() 委派；默认 undefined 失败终局。
  * normal 模式：failure.code 不在 retryableCodes → 立即委派下游（不重试）；
    同 turn/step/provider/policyKey 的 llm/retry 事件计数 ≥ maxRetries →
    委派下游（放弃）。always 模式：不判 code——先委派下游，下游未给出
    retry 决策或失败时自己重试（无限）；下游失败经 logger.warn 容错。
  * 每次重试安排都先落 llm/retry 事件（含策略细节/policyKey/retry 序数/
    delayMs/failure 快照），等待结束后落 llm/retry-started 并返回
    {kind:'retry'}；取消（信号中止）则不落 started、返回 undefined。
    **派发前即检查熔合信号**（backoff 首行 fusedSignal.aborted → return）：
    已中止时连 llm/retry 都不落。
  * retryId：同一 (turn, step, provider, policyKey) 首次生成后全程复用
    （跨次重试同一身份），从会话日志 findLast 恢复。
  * 延迟：providerRetryAfterMs（429 的 Retry-After）有效时优先——超过
    maxDelayMs 则 normal 放弃（委派下游）/ always 改用本地延迟；否则
    localDelay = min(initial * 2^min(retry-1, 1024), max) * 抖动
    (1 - ratio + 2*ratio*random)，再封顶 maxDelayMs。
  * 策略为 undefined（provider 未配置策略）→ 直接委派。
  * **插件生命周期**（lifetime AbortController）：全部等待使用
    AbortSignal.any([请求信号, lifetime.signal]) 熔合；always 模式在下游
    结算后复查熔合信号（中止胜过决策与回退）；监听器包装带陈旧回调守卫
    （lifetime 已中止 → 不再进入下游）；ctx.effect 拆解时注销监听器 +
    lifetime.abort + 排干全部在途恢复（Promise.allSettled 语义）。

async 载体（2026-08-17 asyncio 化重构 + 2026-08-23 生命周期对齐）：
recover_llm_failure 为 async，延迟等待以 asyncio.Event 事件驱动取消
（与上游 AbortSignal 融合的 cancellableDelay 同构）；请求信号与 lifetime
以多信号竞速表达 AbortSignal.any。signal 无 event 事件源时（测试替身）
回退分片轮询 .aborted。
"""
from __future__ import annotations

import asyncio
import inspect
import random
import uuid
from typing import Any, Awaitable, Callable

from .retry_policy import retry_policy_key

_POLL_MS = 50


class RetryLifetime:
    """重试规划器生命周期中止控制器（上游 llm-retry 的 lifetime AbortController）。

    自身即 AbortSignal 面（aborted 布尔 + 可等待 event）：apply 时挂到
    ctx.effect 的拆解回调里 abort，使全部在途恢复立即退出且此后到达的
    陈旧瀑布调用直接放弃。
    """

    def __init__(self) -> None:
        self._reason: BaseException | None = None
        self._event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def aborted(self) -> bool:
        return self._reason is not None

    @property
    def reason(self) -> BaseException | None:
        return self._reason

    @property
    def event(self) -> asyncio.Event | None:
        """惰性创建并绑定当前运行循环（供等待方竞速；无循环时返回既有实例）。"""
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is None:
            return self._event
        if self._event is None or self._loop is not current or self._loop.is_closed():
            self._event = asyncio.Event()
            self._loop = current
        return self._event

    def abort(self, reason: BaseException | None = None) -> None:
        """置位中止标记；事件已物化则（必要时跨线程）置位唤醒等待方。"""
        self._reason = reason or RuntimeError("retry lifetime aborted")
        event, loop = self._event, self._loop
        if event is None or loop is None or loop.is_closed():
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if loop is not current:
            try:
                loop.call_soon_threadsafe(event.set)
                return
            except RuntimeError:
                return   # 循环已关闭：保留 aborted 标记即可
        event.set()


def _aborted(*signals: Any) -> bool:
    """AbortSignal.any 的 aborted 面：任一信号已中止即为真。"""
    return any(s is not None and getattr(s, "aborted", False) for s in signals)


def _events_of(*signals: Any) -> list[asyncio.Event]:
    out = []
    for s in signals:
        if s is None:
            continue
        event = getattr(s, "event", None)
        if event is not None:
            out.append(event)
    return out


def local_delay(policy: dict, retry: int, random: Callable[[], float]) -> int:
    """有界指数退避 + 对称抖动（上游 localDelay 同构）。

    retry-1 指数封顶 1024（防止浮点爆炸）；结果再封顶 maxDelayMs。
    """
    exponent = min(retry - 1, 1024)
    exponential = min(policy["initialDelayMs"] * (2 ** exponent), policy["maxDelayMs"])
    jitter = 1 - policy["jitterRatio"] + 2 * policy["jitterRatio"] * random()
    return int(min(exponential * jitter, policy["maxDelayMs"]))


async def _maybe_await(value: Any) -> Any:
    """next_fn 返回值统一化：coroutine/awaitable 循环解包（awaterfall 的
    next() 会返回下一层 coroutine，须 await 到普通值）。"""
    while inspect.iscoroutine(value) or isinstance(value, Awaitable):
        value = await value
    return value


async def cancellable_delay(delay_ms: int, *signals: Any) -> bool:
    """等待 delay_ms，期间可被任一信号取消；被取消返回 False。

    对齐上游 cancellableDelay(delayMs, AbortSignal)：信号已中止 → 立即
    False；正常等待 → True。多信号 = 上游 AbortSignal.any([请求信号,
    lifetime]) 熔合语义。signal 为 None 时视为永不取消。

    async 载体：全部信号都有事件源（.event）时纯事件驱动——sleep 任务与
    各 event.wait() 竞速（FIRST_COMPLETED），任一置位即醒；存在无事件源的
    裸信号（测试替身）时回退分片轮询 .aborted。
    """
    if _aborted(*signals):
        return False
    if delay_ms <= 0:
        return True
    events = _events_of(*signals)
    has_bare = any(s is not None and getattr(s, "event", None) is None
                   for s in signals)
    if not events or has_bare:
        deadline = asyncio.get_running_loop().time() + delay_ms / 1000
        while True:
            if _aborted(*signals):
                return False
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(_POLL_MS / 1000, remaining))
    sleep_task = asyncio.ensure_future(asyncio.sleep(delay_ms / 1000))
    event_tasks = [asyncio.ensure_future(event.wait()) for event in events]
    done, pending = await asyncio.wait(
        {sleep_task, *event_tasks}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    return sleep_task in done


def _failure_snapshot(failure: Any) -> dict:
    """LlmFailure 的稳定快照（上游序列化 facts 同构）。"""
    if hasattr(failure, "failure"):
        return dict(failure.failure)
    return {"code": "UNKNOWN", "message": str(failure)}


def _failure_proxy(failure: Any) -> Any:
    """容忍 dict 形态的 failure 事实（可序列化载荷），归一为带属性对象。"""
    if not isinstance(failure, dict):
        return failure
    return type("_FailureFacts", (), {
        "code": failure.get("code"),
        "provider_retry_after_ms": failure.get("providerRetryAfterMs"),
        "failure": failure,
    })()


async def recover_llm_failure(
    session,
    turn: int,
    step: int,
    provider: str,
    failure: Any,
    policy: dict | None,
    signal: Any = None,
    next_fn: Callable[[], Any] | None = None,
    random: Callable[[], float] = random.random,
    lifetime: RetryLifetime | None = None,
    logger: Any = None,
) -> Any:
    """执行一次失败恢复：返回 {kind:'retry'} 或委派/放弃（None）。

    与上游 recover() 同语义。next_fn 缺省时委派即返回 None。
    `signal`（回合取消信号）与 `lifetime`（插件生命周期信号）按
    AbortSignal.any 熔合参与全部中止判定。
    """
    failure = _failure_proxy(failure)
    if policy is None:
        return await _maybe_await(next_fn()) if next_fn is not None else None
    if policy["mode"] == "always":
        if _aborted(signal, lifetime):
            return None
        if next_fn is not None:
            try:
                downstream = await _maybe_await(next_fn())
            except Exception as error:
                # 上游 settleDownstream 错误路径经 logger.warn 容错后继续自重试
                if logger is not None and hasattr(logger, "warn"):
                    logger.warn(
                        f'llm-retry: provider "{provider}" always policy '
                        f"ignored a downstream recovery failure: {error!r}")
                downstream = None
            # 上游：下游结算后复查熔合信号——中止胜过决策与回退
            if _aborted(signal, lifetime):
                return None
            if isinstance(downstream, dict) and downstream.get("kind") == "retry":
                return downstream
        # 下游未接管或失败 → 自己无限重试（任何 code）
    else:
        if failure.code not in policy["retryableCodes"]:
            return await _maybe_await(next_fn()) if next_fn is not None else None
        previous = _previous_retry(session, turn, step, provider, policy)
        if previous >= policy["maxRetries"]:
            return await _maybe_await(next_fn()) if next_fn is not None else None

    policy_key = retry_policy_key(policy)
    prior = _previous_retry_event(session, turn, step, provider, policy_key)
    retry = (prior["data"]["retry"] if prior is not None else 0) + 1
    retry_id = prior["data"]["retryId"] if prior is not None else "retry_" + uuid.uuid4().hex

    delay_ms = _resolve_delay(policy, retry, failure, random)
    if delay_ms is None:
        # providerRetryAfterMs 超过 maxDelayMs 且 normal：放弃（委派下游）
        return await _maybe_await(next_fn()) if next_fn is not None else None

    # 上游 backoff 首行：派发前检查熔合信号——已中止则连 llm/retry 都不落
    if _aborted(signal, lifetime):
        return None

    event_data: dict[str, Any] = {
        "retryId": retry_id, "turn": turn, "step": step, "provider": provider,
        "mode": policy["mode"], "policyKey": policy_key, "retry": retry,
        "delayMs": delay_ms, "failure": _failure_snapshot(failure),
    }
    if policy["mode"] == "normal":
        event_data["maxRetries"] = policy["maxRetries"]
    session.append("llm/retry", event_data)
    if not await cancellable_delay(delay_ms, signal, lifetime):
        return None
    session.append("llm/retry-started", {"retryId": retry_id, "turn": turn,
                                         "step": step, "retry": retry})
    return {"kind": "retry"}


def _resolve_delay(policy: dict, retry: int, failure: Any, random: Callable[[], float]) -> int | None:
    """延迟决议：providerRetryAfterMs 优先，越界按模式处置（上游同构）。"""
    after = getattr(failure, "provider_retry_after_ms", None)
    if isinstance(after, (int, float)) and after > 0:
        if after > policy["maxDelayMs"]:
            if policy["mode"] == "normal":
                return None
            return local_delay(policy, retry, random)
        return int(after)
    return local_delay(policy, retry, random)


def _previous_retry_event(session, turn: int, step: int, provider: str, policy_key: str) -> dict | None:
    """会话日志中同 (turn, step, provider, policyKey) 的最后一条 llm/retry。"""
    for event in reversed(session.events):
        if event["type"] != "llm/retry":
            continue
        data = event["data"]
        if (data["turn"] == turn and data["step"] == step
                and data["provider"] == provider and data["policyKey"] == policy_key):
            return event
    return None


def _previous_retry(session, turn: int, step: int, provider: str, policy: dict) -> int:
    event = _previous_retry_event(session, turn, step, provider, retry_policy_key(policy))
    return event["data"]["retry"] if event is not None else 0


def apply_retry_planner(ctx) -> None:
    """在 ctx 上挂载 agent/request-error 恢复监听器（幂等，可多次调用）。

    对应上游 llm-retry 插件的 apply（app-boot 激活 llm-retry 即注册）；
    mini 无插件装配路径，由装配方（headless / sessions / acp / sdk /
    demo / 测试）在构造 AgentLoop 之前显式调用（迁移步骤 3：构造无副作用）。

    生命周期对齐上游 index.ts apply：
      * 监听器包装带陈旧回调守卫——插件拆解后仍可能被既有瀑布引用捕获，
        lifetime 已中止时直接返回 undefined 不再进入下游策略；
      * 每次恢复经 active 集合跟踪（上游 track()）；
      * ctx.effect 登记 teardown：注销监听器 → lifetime.abort → 排干
        全部在途恢复（Promise.allSettled 语义，label 逐字）。
    """
    if getattr(ctx, "_miniharness_retry_planner", False):
        return
    lifetime = RetryLifetime()
    active: set[asyncio.Future] = set()
    logger = getattr(ctx, "logger", None)

    async def _tracked_recover(payload: dict, next: Callable | None) -> Any:
        return await recover_llm_failure(
            session=payload["agent"].session,
            turn=payload["turn"],
            step=payload["step"],
            provider=payload["provider"],
            failure=payload["failure"],
            policy=payload.get("retryPolicy"),
            signal=payload.get("signal"),
            next_fn=next,
            lifetime=lifetime,
            logger=logger,
        )

    async def _on_request_error(payload: dict, next: Callable[[], Any] | None = None) -> Any:
        """agent/request-error 监听器：复刻上游 llm-retry 的 recover 入口。

        payload.failure 是 LlmFailure 实例（loop 以异常传入，同上游携带
        normalized facts 的对象）；retryPolicy 由适配器注册时捕获（上游经
        注册表 providerRetryPolicy 决议，mini 在请求时直接读适配器字段）。
        """
        if lifetime.aborted:
            return None   # 陈旧回调守卫（上游 listener wrapper 首行）
        task = asyncio.get_running_loop().create_task(_tracked_recover(payload, next))
        active.add(task)
        try:
            return await task
        finally:
            active.discard(task)

    disposer = ctx.on("agent/request-error", _on_request_error)

    def _register() -> Callable[[], Any]:
        """effect body：立即执行、返回 disposer（上游 () => async () => {...} 形态）。
        disposer 为 async def——拆解时注销监听器 + lifetime.abort + 排干在途恢复
        （await allSettled 语义；无循环时由 fiber 拆解路径 fail-loud 收容）。"""

        async def _teardown() -> None:
            disposer()
            lifetime.abort(RuntimeError("llm-retry plugin disposed"))
            if active:
                await asyncio.gather(*active, return_exceptions=True)

        return _teardown

    ctx.effect(_register, "llm-retry: abort and drain active recovery")
    ctx._miniharness_retry_planner = True