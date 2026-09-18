"""语义持久化检查点策略（session-checkpoint-policy）。

对应 dsh 真实源码：packages/session/session-checkpoint-policy/src/index.ts。

三种屏障在真实副作用前把工作落盘：

  1. **模型请求**——适配器流构造前，把已记录的请求前缀刷盘（崩溃后不会重放
     未持久化的请求）；
  2. **顶层工具派发**——工具体执行前，把已记录的工具调用刷盘（外部副作用前
     call 已 durable）；嵌套派发复用外层调用的检查点；
  3. **每步边界**（`agent/pre-step`）——上一步提交的响应与有序工具结果在下一
     请求派生前刷盘。

检查点失败在两个边界 fail-closed：下游适配器或工具体不得进入。取消落在工具
检查点窗口内时，返回 canonical `ABORTED_BEFORE_DISPATCH` 结果，绝不进入工具体。

载体差异（mini）：上游经 `llm/stream` 服务 waterfall 延迟适配器流构造；mini 为
单一 adapter 实例直接调用，故模型请求屏障挂在 `agent/request` waterfall（在
adapter.stream 之前、请求信封落日志之后）。epoch 检查点的 flush 同步载体经
`SessionStore.checkpoint`（无参与者即 fail-closed）。
"""
from __future__ import annotations

from typing import Any

from ..core.session_store import SessionCheckpointError
from ..llm.protocol import StreamAborted

__all__ = [
    "CHECKPOINT_ABORTED_CODE",
    "install_checkpoint_policy",
]

#: 工具在派发前被取消的 canonical 错误码（上游 TOOL_ABORTED_BEFORE_DISPATCH）。
CHECKPOINT_ABORTED_CODE = "ABORTED_BEFORE_DISPATCH"


def _aborted_before_dispatch_result():
    """工具在派发前被取消的 canonical 错误结果（上游 abortedBeforeDispatchResult 逐字）。"""
    from ..core.tools import ToolResult

    return ToolResult(
        ok=False, is_error=True,
        error="Error: tool call aborted before dispatch",
        error_info={"name": "AbortError", "code": CHECKPOINT_ABORTED_CODE},
    )


def _is_aborted(exec_) -> bool:
    signal = getattr(exec_, "signal", None)
    if signal is None:
        return False
    if getattr(signal, "aborted", False):
        return True
    is_set = getattr(signal, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def install_checkpoint_policy(ctx: Any):
    """安装语义检查点监听（上游 apply）。

    @param ctx - 拥有监听器与服务（`ctx.sessions`）的插件上下文。
    @returns disposer。
    """
    registered: list[tuple[str, Any]] = []

    def _sessions():
        return ctx.get("sessions")

    def _register(event: str, handler) -> None:
        registered.append((event, handler))
        ctx.on(event, handler)

    def on_agent_checkpoint(payload: dict, next_fn):
        """模型请求屏障：prepared route 决议后、adapter 派发前刷盘。

        只处理 boundary == 'request'；其它边界（若将来扩展）委派下游。
        """
        sessions = _sessions()
        agent = payload.get("agent")
        if (sessions is None or agent is None
                or payload.get("boundary") != "request"):
            return next_fn()
        sessions.checkpoint(agent.session)
        return next_fn()

    _register("agent/checkpoint", on_agent_checkpoint)

    def on_pre_execute(payload: dict, next_fn):
        """工具屏障：顶层工具体执行前刷盘；取消则折叠为 ABORTED_BEFORE_DISPATCH。

        嵌套派发（exec_.parent 非空）复用外层调用的检查点，不再刷盘。
        """
        sessions = _sessions()
        exec_ = payload.get("exec")
        if sessions is None or exec_ is None or getattr(exec_, "agent", None) is None:
            return next_fn()
        if getattr(exec_, "parent", None) is not None:
            return next_fn()
        sessions.checkpoint(exec_.agent.session)
        if _is_aborted(exec_):
            # 取消落在检查点窗口内：返回 canonical ABORTED_BEFORE_DISPATCH，
            # 绝不进入工具体（上游 abortedBeforeDispatchResult）。
            return {"kind": "deny", "result": _aborted_before_dispatch_result()}
        return next_fn()

    _register("tools/pre-execute", on_pre_execute)

    def on_pre_step(payload: dict, next_fn):
        """步边界屏障：上一响应/结果批在下一请求派生前刷盘（首步 intentionally no-op）。"""
        sessions = _sessions()
        agent = payload.get("agent")
        if sessions is None or agent is None:
            return next_fn()
        sessions.checkpoint(agent.session)
        return next_fn()

    _register("agent/pre-step", on_pre_step)

    def dispose() -> None:
        """移除三个监听器（mini 的 ctx.on 由 owning fiber 生命周期托管；
        本显式 disposer 供测试/独立装配按需拆解）。"""
        for event, handler in registered:
            listeners = getattr(ctx, "_listeners", {}).get(event)
            if listeners and handler in listeners:
                listeners.remove(handler)

    return dispose
