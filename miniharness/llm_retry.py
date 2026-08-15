"""第 4 章：模型请求重试执行器 —— provider 路由的失败恢复。

对应 dsh 真实源码：packages/llm/llm-retry/src/index.ts（+ history.ts）。

上游语义（已核实，llm-retry/src/index.ts）：
  * 挂在 agent/request-error waterfall 扩展点：监听器返回 {kind:'retry'}
    且不调 next() = 自己接管恢复；调 next() 委派；默认 undefined 失败终局。
  * normal 模式：failure.code 不在 retryableCodes → 立即委派下游（不重试）；
    同 turn/step/provider/policyKey 的 llm/retry 事件计数 ≥ maxRetries →
    委派下游（放弃）。always 模式：不判 code——先委派下游，下游未给出
    retry 决策或失败时自己重试（无限）。
  * 每次重试安排都先落 llm/retry 事件（含策略细节/policyKey/retry 序数/
    delayMs/failure 快照），等待结束后落 llm/retry-started 并返回
    {kind:'retry'}；取消（signal aborted）则不落 started、返回 undefined。
  * retryId：同一 (turn, step, provider, policyKey) 首次生成后全程复用
    （跨次重试同一身份），从会话日志 findLast 恢复。
  * 延迟：providerRetryAfterMs（429 的 Retry-After）有效时优先——超过
    maxDelayMs 则 normal 放弃（委派下游）/ always 改用本地延迟；否则
    localDelay = min(initial * 2^min(retry-1, 1024), max) * 抖动
    (1 - ratio + 2*ratio*random)，再封顶 maxDelayMs。
  * 策略为 undefined（provider 未配置策略）→ 直接委派。

载体简化（须在文档标注）：上游 async + AbortSignal 融合；mini 同步——
等待以分片 sleep 轮询 signal.aborted 实现可取消（无计时器泄漏语义），
请求仍阻塞事件循环（与既有 LLM 同步阻塞一致）。
"""
from __future__ import annotations

import random
import time
import uuid
from typing import Any, Callable

from .retry_policy import retry_policy_key

_POLL_MS = 50


def local_delay(policy: dict, retry: int, random: Callable[[], float]) -> int:
    """有界指数退避 + 对称抖动（上游 localDelay 同构）。

    retry-1 指数封顶 1024（防止浮点爆炸）；结果再封顶 maxDelayMs。
    """
    exponent = min(retry - 1, 1024)
    exponential = min(policy["initialDelayMs"] * (2 ** exponent), policy["maxDelayMs"])
    jitter = 1 - policy["jitterRatio"] + 2 * policy["jitterRatio"] * random()
    return int(min(exponential * jitter, policy["maxDelayMs"]))


def cancellable_delay(delay_ms: int, signal: Any) -> bool:
    """等待 delay_ms，期间轮询 signal.aborted；被取消返回 False。

    对齐上游 cancellableDelay：signal 已中止 → 立即 False；正常等待 →
    True。signal 为 None 时视为永不取消。
    """
    if signal is not None and signal.aborted:
        return False
    deadline = time.monotonic() + delay_ms / 1000
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        if signal is not None and signal.aborted:
            return False
        time.sleep(min(_POLL_MS / 1000, remaining))


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


def recover_llm_failure(
    session,
    turn: int,
    step: int,
    provider: str,
    failure: Any,
    policy: dict | None,
    signal: Any = None,
    next_fn: Callable[[], Any] | None = None,
    random: Callable[[], float] = random.random,
) -> Any:
    """执行一次失败恢复：返回 {kind:'retry'} 或委派/放弃（None）。

    与上游 recover() 同语义。next_fn 缺省时委派即返回 None。
    """
    failure = _failure_proxy(failure)
    if policy is None:
        return next_fn() if next_fn is not None else None
    if policy["mode"] == "always":
        if signal is not None and signal.aborted:
            return None
        if next_fn is not None:
            try:
                downstream = next_fn()
            except Exception:
                downstream = None
            if isinstance(downstream, dict) and downstream.get("kind") == "retry":
                return downstream
        # 下游未接管或失败 → 自己无限重试（任何 code）
    else:
        if failure.code not in policy["retryableCodes"]:
            return next_fn() if next_fn is not None else None
        previous = _previous_retry(session, turn, step, provider, policy)
        if previous >= policy["maxRetries"]:
            return next_fn() if next_fn is not None else None

    policy_key = retry_policy_key(policy)
    prior = _previous_retry_event(session, turn, step, provider, policy_key)
    retry = (prior["data"]["retry"] if prior is not None else 0) + 1
    retry_id = prior["data"]["retryId"] if prior is not None else "retry_" + uuid.uuid4().hex

    delay_ms = _resolve_delay(policy, retry, failure, random)
    if delay_ms is None:
        # providerRetryAfterMs 超过 maxDelayMs 且 normal：放弃（委派下游）
        return next_fn() if next_fn is not None else None

    event_data: dict[str, Any] = {
        "retryId": retry_id, "turn": turn, "step": step, "provider": provider,
        "mode": policy["mode"], "policyKey": policy_key, "retry": retry,
        "delayMs": delay_ms, "failure": _failure_snapshot(failure),
    }
    if policy["mode"] == "normal":
        event_data["maxRetries"] = policy["maxRetries"]
    session.append("llm/retry", event_data)
    if not cancellable_delay(delay_ms, signal):
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


def _on_request_error(payload: dict, next: Callable[[], Any] | None = None) -> Any:
    """agent/request-error 监听器：复刻上游 llm-retry 的 recover 入口。

    payload.failure 是 LlmFailure 实例（loop 以异常传入，同上游携带
    normalized facts 的对象）；retryPolicy 由适配器注册时捕获（上游经
    注册表 providerRetryPolicy 决议，mini 在请求时直接读适配器字段）。
    """
    agent = payload["agent"]
    return recover_llm_failure(
        session=agent.session,
        turn=payload["turn"],
        step=payload["step"],
        provider=payload["provider"],
        failure=payload["failure"],
        policy=payload.get("retryPolicy"),
        signal=payload.get("signal"),
        next_fn=next,
    )


def apply_retry_planner(ctx) -> None:
    """在 ctx 上挂载 agent/request-error 恢复监听器（幂等，可多次调用）。

    对应上游 llm-retry 插件的 apply（app-boot 激活 llm-retry 即注册）；
    mini 无插件装配路径，由 AgentLoop 构造时确保挂载。
    """
    if getattr(ctx, "_miniharness_retry_planner", False):
        return
    ctx.on("agent/request-error", _on_request_error)
    ctx._miniharness_retry_planner = True