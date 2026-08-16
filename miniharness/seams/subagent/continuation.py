"""可继续子代理：durable 子会话 + 冷恢复 + 结算投递 + 枚举。

上游对照：packages/subagent/subagent/src/continuation.ts（可继续子代理
管理器 startContinuable / sendMessage / interrupt / coldResume /
listChildren / listDescendants）+ subagent-in-process-driver（进程内
驱动）+ subagent-spawn-in-process（进程内 spawn 后端）+
tool-subagent-report + tool-subagent-control（send_message /
interrupt_agent / list_agents）+ AgentLoop 干预面。

mini 同步阻塞模型的落点（决策见 status/mini-harness/migration-log.md A7）：
  * startContinuable 只做 durable 创建（durable before dispatch）：header
    declare（meta）+ 父 completed-turn 前缀 seed + `subagent/descriptor`
    事件，全部先落盘；此后每次 sendMessage 冷恢复该子会话。
  * sendMessage 在调用栈内同步跑完子回合（子 AgentLoop 复用父 _pump）；
    激活只活在 _activations 内，结算在 pump 返回后计算并整体 flush 子会话。
  * 投递规则（结算通知与 report wakeup 同规则）：父 running → inbox.append
    （非唤醒，当前 pump 下一步边界捡起）；父 idle → followup（唤醒新回合）。
    reportDelivery 'background' → 一律 inbox.append。
  * coldResume：inspect → meta.parentSession 校验（UNAUTHORIZED）→ 折叠
    描述符（mode != 'continuable' → NOT_RESUMABLE）→ 以持久化事件为 seed
    重建会话、上下文与组合。

同步模型简化（须在文档标注；上游为异步事件驱动，对齐列为 A8 里程碑）：
  * **执行模型差异（最大简化）**：上游 followup() 投递进 inbox 立即返回
    message id，Activation 跨回合驻留（一次 residency epoch 可跑多个 FIFO
    turns），watchSettlement 经 whenIdle()+ownedChildren 事件驱动判静默，
    ChildLock 串行化每 child 投递/销毁，steer 批内合并多子结算，结算在
    释放所有权之前投递；mini 的 sendMessage 同步跑完整个子回合，无跨会话
    并发，"后台"坍缩为同步。
  * activations 只存在于 sendMessage 调用栈内；interrupt 的真实可达路径是
    "子回合内自中断"（子工具/钩子调 manager.interrupt(child_id)），父在
    step 边界调用时子已 settle → NOT_FOUND。
  * 子事件仅在 settle 时整体 flush（进程崩溃丢在途日志，未做到上游 commitRepair
    的 torn 尾部修复）。
  * 子会话不装 send_message 等控制工具 → 不支持嵌套续跑（delegationDepth
    仍写入 meta 供诊断，恒为父深度 + 1）。
  * 无 subagent/start|end 生命周期域事件（上游经 ctx.subagents 生命周期
    发射器发布），结算经 user/message 消息投递。
"""
from __future__ import annotations

import json
import uuid
from types import MappingProxyType
from typing import Any, Callable

from ...core.agent_loop.agent import AgentLoop
from ...core.scope import Context
from ...core.session import Session, create_message, is_json_safe, text_block, thaw
from ...core.session.persistence import SessionPersistence
from ...core.system_prompt import SYSTEM_PROMPT_SERVICE, SystemPromptService
from ...core.tools import Tool, ToolExec, ToolRegistry
from ...llm import FakeLlmAdapter, LlmAdapter
from .descriptor import fold_subagent_descriptor, seed_descriptor_turn
from .providers import completed_turn_prefix

__all__ = [
    "CONTEXT_SUMMARY_MAX_CHARS",
    "SubagentContinuationManager",
    "SubagentError",
    "bound_context_summary",
    "delegation_depth_of",
    "epoch_stop_reason",
    "final_assistant_output",
    "install_subagent_control_tools",
    "settlement_summary",
]

CONTEXT_SUMMARY_MAX_CHARS = 120

# 结算文案（对齐上游 continuation.ts settlementSummary 措辞，逐字）
_SETTLEMENT_SUMMARIES = {
    "completed": "finished and will do no further work unless you send it more.",
    "aborted": "was stopped before it finished.",
    "max-tokens": "ran out of room before it finished.",
    "refusal": "declined the task.",
    "error": "failed before it finished.",
}

_DEFAULT_CHILD_PERSONA = (
    "You are a background subagent working on behalf of the parent agent. "
    "Complete the task you are given and report the result back."
)

_REPORT_GUIDANCE = (
    "You may call the report tool to send an interim or final report to the "
    "parent agent. Use reportDelivery 'foreground' to wake the parent, or "
    "'background' to leave it running silently."
)


class SubagentError(Exception):
    """子代理错误：携带上游错误码（UNAUTHORIZED / NOT_RESUMABLE /
    ACTIVATION_CLOSING / NOT_FOUND / MAX_DEPTH_EXCEEDED / UNAVAILABLE）。"""

    def __init__(self, message: str, code: str = "SUBAGENT_ERROR"):
        super().__init__(message)
        self.code = code


def bound_context_summary(summary: str, max_chars: int = CONTEXT_SUMMARY_MAX_CHARS) -> str:
    """截断到 max_chars 字符 + 省略号（上游 llm/src/message.ts CONTEXT_SUMMARY_MAX_CHARS）。"""
    if len(summary) <= max_chars:
        return summary
    return summary[:max_chars] + "…"


def delegation_depth_of(loop: AgentLoop) -> int:
    """会话 meta 里的委托深度；非法值 → 0（上游 depth.ts delegationDepthOf）。"""
    depth = loop.session.meta.get("delegationDepth")
    if isinstance(depth, int) and not isinstance(depth, bool) and depth >= 0:
        return depth
    return 0


def epoch_stop_reason(events) -> str:
    """片段内最后一个 turn/end 的 reason → 结算关键词。

    mini 词汇：max-tokens / aborted / refusal / error / completed（对齐上游
    settle 的 stopReason 归一）；interrupted 并入 aborted。
    """
    for ev in reversed(events):
        if not isinstance(ev, (dict, MappingProxyType)) or ev.get("type") != "turn/end":
            continue
        reason = ev.get("data", {}).get("reason")
        kind = reason.get("kind") if isinstance(reason, (dict, MappingProxyType)) else reason
        if kind == "max-tokens":
            return "max-tokens"
        if kind in ("aborted", "interrupted"):
            return "aborted"
        if kind == "blocked":
            return "refusal"
        if kind == "error":
            return "error"
        if kind in ("completed", None):
            return "completed"
        return "error"
    return "completed"


def final_assistant_output(events) -> list | None:
    """片段内最后非空 assistant 文本块；无则首个有内容的 assistant 消息。

    对齐上游 assistant-output.ts AssistantOutputFold：最后非空 assistant
    文本胜出，空内容 assistant 消息只记 usage；两者都无 → None。
    返回普通 dict/list（已解冻），可直接进消息构造。
    """
    last: list | None = None
    accumulated: list | None = None
    for ev in events:
        if not isinstance(ev, (dict, MappingProxyType)) or ev.get("type") != "assistant/message":
            continue
        msg = ev.get("data", {}).get("message")
        if not msg or msg.get("role") != "assistant":
            continue
        content = msg.get("content") or []
        texts = [b for b in content if b.get("type") == "text" and b.get("text")]
        if texts:
            last = [thaw(b) for b in content if b.get("type") == "text"]
        if accumulated is None and content:
            accumulated = [thaw(b) for b in content]
    return last if last is not None else accumulated


def settlement_summary(stop: str, child_id: str) -> str:
    """按 stop 关键词构造结算一句话（上游 settlementSummary，逐字对齐）。"""
    if stop in _SETTLEMENT_SUMMARIES:
        phrase = _SETTLEMENT_SUMMARIES[stop]
    else:
        phrase = f"ended abnormally ({stop}) before it finished."
    return f"Background subagent {child_id} {phrase}"


def _default_adapter_factory(provider: str, model: str) -> LlmAdapter:
    if provider == "fake":
        return FakeLlmAdapter()
    raise SubagentError(f"provider {provider!r} 不可用（mini 仅内建 fake）", "UNAVAILABLE")

class SubagentContinuationManager:
    """可继续子代理管理器：创建 / 续跑 / 中断 / 结算投递 / 枚举。

    @param parent - 父 AgentLoop（子代理的宿主与投递目标）。
    @param persistence - 会话持久化后端（declare / inspect / list_headers）。
    @param max_depth - 委托深度上限（上游 subagent-max-depth 默认 8）。
    @param adapter_factory - 冷恢复时按 (provider, model) 重建适配器；
        缺省仅支持 'fake'（复用父适配器当 provider 一致时）。
    """

    def __init__(
        self,
        parent: AgentLoop,
        persistence: SessionPersistence,
        max_depth: int = 8,
        adapter_factory: Callable[[str, str], LlmAdapter] | None = None,
    ):
        self.parent = parent
        self.persistence = persistence
        self.max_depth = max_depth
        self._adapter_factory = adapter_factory or _default_adapter_factory
        self._activations: dict[str, dict] = {}
        self._persisted: dict[str, int] = {}

    @property
    def activations(self) -> dict[str, dict]:
        """簿记视图：激活中的子代理（同步模型下仅 sendMessage 期间存在）。"""
        return {cid: {"status": a["status"], "label": a["label"]}
                for cid, a in self._activations.items()}

    # ---------- 创建与续跑 ----------

    def start_continuable(
        self,
        label: str | None = None,
        tool_filter: list[str] | None = None,
        persona: str | None = None,
    ) -> str:
        """创建可继续子会话（durable before dispatch），返回子 id。

        子会话 = 父 completed-turn 前缀 seed + meta + 描述符事件；全部先
        落盘。父当前 in-flight 回合未平衡 → seed 为空（全新子会话）。
        """
        depth = delegation_depth_of(self.parent)
        if depth >= self.max_depth:
            raise SubagentError(
                f"子代理嵌套深度 {depth} 达到上限 {self.max_depth}", "MAX_DEPTH_EXCEEDED",
            )
        child_id = "child-" + uuid.uuid4().hex[:12]
        child_depth = depth + 1
        seed = completed_turn_prefix(self.parent.session.events)
        meta: dict[str, Any] = {
            "parentSession": self.parent.id,
            "origin": "subagent",
            "delegationDepth": child_depth,
        }
        if label:
            meta["label"] = label
        if seed:
            meta["seedLength"] = len(seed)
        descriptor: dict[str, Any] = {
            "kind": "continuable",
            "mode": "continuable",
            "agentProvider": getattr(self.parent.adapter, "provider", None),
            "agentModel": getattr(self.parent.adapter, "model", None),
        }
        if label:
            descriptor["label"] = label
        if persona:
            descriptor["persona"] = persona
        if tool_filter:
            descriptor["toolFilter"] = tool_filter

        child_session = Session(child_id, seed=seed, meta=meta)
        seed_descriptor_turn(child_session, descriptor)
        self.persistence.declare(child_id, meta, created_at=child_session.created_at)
        self._persist_delta(child_session, start=0)
        self._persisted[child_id] = len(child_session.events)
        return child_id

    def send_message(self, child_id: str, message: str | dict, source: str = "parent") -> None:
        """向子代理发一条消息并同步跑完子回合；结算通知投递父代理。

        子回合失败不冒泡（失败已是子会话内的 error turn/end，结算通知
        全量报告；对齐上游 sendMessage 不因子进程失败抛错）。
        """
        activation = self._get_or_resume(child_id)
        child = activation["loop"]
        boundary = len(child.session.events)
        msg = message if isinstance(message, dict) else create_message(
            "user", [text_block(message)], {"kind": "user"},
        )
        try:
            child.followup(msg)
        except Exception:
            pass
        finally:
            self._settle(child_id, activation, boundary)

    def report_from(self, child_id: str, report: str, quiet: bool = False) -> None:
        """子代理 report 工具：把报告投递父代理（quiet → 非唤醒 inbox）。"""
        message = create_message(
            "user",
            [text_block(report)],
            {
                "kind": "subagent-report",
                "form": "background-report" if quiet else "report",
                "senderSessionId": child_id,
            },
        )
        if quiet:
            self.parent.inbox.append(message)
        else:
            self._deliver(message)

    def interrupt(self, child_id: str, cause: str = "user") -> None:
        """中断激活中的子代理（child.cancel(keep_inbox=True)）。

        同步模型下仅子回合内可中断（调用栈内 _activations 存在）；子已
        settle / 未激活 → NOT_FOUND。
        """
        activation = self._activations.get(child_id)
        if activation is None:
            raise SubagentError(f"子代理 {child_id} 未激活，无法中断", "NOT_FOUND")
        activation["status"] = "stopping"
        activation["loop"].cancel(cause, keep_inbox=True)

    def state_of(self, child_id: str) -> dict:
        """簿记查询（上游 stateOf 的 mini 形态：未激活视为 idle）。"""
        act = self._activations.get(child_id)
        if act is not None:
            return {"kind": act["status"], "id": child_id, "label": act.get("label") or child_id}
        return {"kind": "idle", "id": child_id, "label": child_id}

    # ---------- 枚举 ----------

    def list_children(self) -> list[dict]:
        """直属子代理：meta.parentSession == 本父的所有持久化子会话 + 激活中。"""
        return [self._child_entry(cid) for cid in self._known_child_ids()]

    def list_descendants(self) -> list[dict]:
        """全部后代。mini 无 agents 注册表且子会话不装控制工具（无嵌套），
        descendants == children（简化标注）。"""
        return self.list_children()

    def _child_entry(self, child_id: str) -> dict:
        depth = 0
        label = child_id
        status = "idle"
        for header in self.persistence.list_headers():
            if header.get("id") != child_id:
                continue
            meta = header.get("meta")
            if isinstance(meta, dict):
                d = meta.get("delegationDepth")
                if isinstance(d, int) and not isinstance(d, bool) and d >= 0:
                    depth = d
                if meta.get("label"):
                    label = meta["label"]
            break
        act = self._activations.get(child_id)
        if act is not None:
            status = act["status"]
            if act.get("label"):
                label = act["label"]
        return {"kind": "child", "id": child_id, "label": label, "status": status, "depth": depth}

    def _known_child_ids(self) -> list[str]:
        ids = set(self._activations)
        for header in self.persistence.list_headers():
            meta = header.get("meta")
            if isinstance(meta, dict) and meta.get("parentSession") == self.parent.id:
                ids.add(header.get("id"))
        return sorted(cid for cid in ids if cid)

    # ---------- 冷恢复与激活 ----------

    def _get_or_resume(self, child_id: str) -> dict:
        existing = self._activations.get(child_id)
        if existing is not None:
            # 同步模型下重复进入不可达（sendMessage 串行且激活在 settle 时
            # 先行删除再投递）；命中即防御性拒绝。
            raise SubagentError(f"子代理 {child_id} 正在激活中", "ACTIVATION_CLOSING")
        return self._cold_resume(child_id)

    def _cold_resume(self, child_id: str) -> dict:
        info = self.persistence.inspect(child_id)
        meta = info["meta"]
        if not isinstance(meta, dict) or meta.get("parentSession") != self.parent.id:
            raise SubagentError("子会话不存在或不属于当前父代理", "UNAUTHORIZED")
        events = info["events"]
        descriptor = fold_subagent_descriptor(events)
        if descriptor is None or descriptor.get("mode") != "continuable":
            raise SubagentError("子会话不可继续（描述符缺失或非 continuable）", "NOT_RESUMABLE")
        child_session = Session(child_id, seed=events, meta=meta)
        return self._build_activation(child_session, descriptor, persisted=len(events))

    def _build_activation(self, child_session: Session, descriptor: dict, persisted: int) -> dict:
        child_id = child_session.session_id
        child_ctx = self.parent.ctx.create_scope(f"subagent:{child_id}")

        reg = ToolRegistry(child_ctx)
        allow = set(descriptor.get("toolFilter") or [])
        for name in self.parent.tools.names():
            if allow and name not in allow:
                continue
            tool = self.parent.tools.resolve(name)
            if tool is not None:
                reg.register(tool)
        reg.register(self._report_tool(child_id))

        child = AgentLoop(
            child_session, self._resolve_adapter(descriptor), reg, child_ctx,
            system_prompt=self.parent.system_prompt,
            max_steps=self.parent.max_steps,
            max_parallel_tool_calls=self.parent.max_parallel_tool_calls,
        )

        # 子作用域自己的 system prompt 服务（mini 的 SystemPromptService 是
        # 全局单例非 scope-aware → 子作用域提供独立实例）
        svc = SystemPromptService(child_ctx)
        child_ctx.provide(SYSTEM_PROMPT_SERVICE, svc)
        svc.section("persona", 0, descriptor.get("persona") or _DEFAULT_CHILD_PERSONA)
        svc.section("report-guidance", 117, _REPORT_GUIDANCE)
        svc.section("delegation-context", 120, lambda c: self._delegation_context(child_id))

        activation = {
            "loop": child,
            "ctx": child_ctx,
            "registry": reg,
            "label": descriptor.get("label") or child_id,
            "status": "running",
            "persisted": persisted,
            "descriptor": descriptor,
        }
        self._activations[child_id] = activation
        return activation

    def _resolve_adapter(self, descriptor: dict) -> LlmAdapter:
        """按描述符重建子适配器（上游按 agentProvider/agentModel 重建 provider）。

        总是新建而非复用父适配器：子代理的模型实例彼此独立（共享父适配器
        会串调用计数等状态）。provider/model 缺省继承父。
        """
        provider = descriptor.get("agentProvider") or getattr(self.parent.adapter, "provider", None)
        model = descriptor.get("agentModel") or getattr(self.parent.adapter, "model", None)
        return self._adapter_factory(provider, model)

    def _delegation_context(self, child_id: str) -> str:
        return (
            f"Parent session: {self.parent.id}. This subagent is {child_id}; "
            "it inherits the parent's completed conversation history."
        )

    # ---------- 结算 ----------

    def _settle(self, child_id: str, activation: dict, boundary: int) -> None:
        child = activation["loop"]
        child_ctx = activation["ctx"]
        summary = None
        output = None
        try:
            stop = epoch_stop_reason(child.session.events[boundary:])
            output = final_assistant_output(child.session.events[boundary:])
            summary = settlement_summary(stop, child_id)
            self._persist_delta(child.session, start=activation["persisted"])
            self._persisted[child_id] = len(child.session.events)
        finally:
            # 先摘激活再投递：投递可能唤醒父 pump，父工具可对同一子代理
            # 再次 send_message（此时必须冷恢复而非撞 ACTIVATION_CLOSING）
            child_ctx.dispose()
            self._activations.pop(child_id, None)
        self._deliver_settlement(child_id, summary, output)

    def _persist_delta(self, session: Session, start: int) -> None:
        events = session.events[start:]
        if not events:
            return
        for event in events:
            self.persistence.append(session.session_id, event)
        self.persistence.flush()

    def _deliver_settlement(self, child_id: str, summary: str, output: list | None) -> None:
        blocks = [text_block(summary)]
        if output is None:
            blocks.append(text_block("It left no closing message."))
        else:
            blocks.append(text_block("Its closing message:"))
            blocks.extend(thaw(b) if not isinstance(b, dict) else b for b in output)
        message = create_message(
            "user",
            blocks,
            {
                "kind": "subagent-settled",
                "form": "notice",
                "summary": bound_context_summary(summary),
                "senderSessionId": child_id,
            },
        )
        self._deliver(message)

    def _deliver(self, message: dict) -> None:
        if self.parent.status == "idle":
            self.parent.followup(message)
        else:
            self.parent.inbox.append(message)

    # ---------- 子专属 report 工具 ----------

    def _report_tool(self, child_id: str) -> Tool:
        manager = self

        def execute(args: dict, exec_: ToolExec):
            quiet = args.get("reportDelivery") == "background"
            manager.report_from(child_id, args["report"], quiet=quiet)
            return args["report"]

        return Tool(
            name="report",
            description=(
                "Send an interim or final report to the parent agent. "
                "foreground wakes the parent (default); background does not."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "report": {"type": "string", "description": "Report content."},
                    "reportDelivery": {
                        "type": "string",
                        "enum": ["foreground", "background"],
                        "description": "foreground wakes the parent; background leaves it idle.",
                    },
                },
                "required": ["report"],
            },
            execute=execute,
        )


# ---------- 父注册表的控制工具（tool-subagent-control） ----------

def install_subagent_control_tools(
    ctx: Context, reg: ToolRegistry, manager: SubagentContinuationManager,
) -> None:
    """在父注册表安装 send_message / interrupt_agent / list_agents。

    对齐上游 tool-subagent-control（发送即唤醒续跑；interrupt 需要激活中
    子代理；list_agents 返回子代理清单的 JSON 文本）。
    """
    reg.register(_send_message_tool(manager))
    reg.register(_interrupt_agent_tool(manager))
    reg.register(_list_agents_tool(manager))


def _send_message_tool(manager: SubagentContinuationManager) -> Tool:
    def execute(args: dict, exec_: ToolExec):
        manager.send_message(args["subagentId"], args["message"])
        return f"Message sent to subagent {args['subagentId']}."

    return Tool(
        name="send_message",
        description=(
            "Send a message to a subagent and continue its run. The subagent "
            "settles with a report that is delivered back to you."
        ),
        parameters={
            "type": "object",
            "properties": {
                "subagentId": {"type": "string", "description": "Subagent id from list_agents."},
                "message": {"type": "string", "description": "Message content."},
            },
            "required": ["subagentId", "message"],
        },
        execute=execute,
    )


def _interrupt_agent_tool(manager: SubagentContinuationManager) -> Tool:
    def execute(args: dict, exec_: ToolExec):
        manager.interrupt(args["subagentId"], cause="parent")
        return f"Interrupted subagent {args['subagentId']}."

    return Tool(
        name="interrupt_agent",
        description="Stop a running subagent.",
        parameters={
            "type": "object",
            "properties": {
                "subagentId": {"type": "string", "description": "Subagent id from list_agents."},
            },
            "required": ["subagentId"],
        },
        execute=execute,
    )


def _list_agents_tool(manager: SubagentContinuationManager) -> Tool:
    def execute(args: dict, exec_: ToolExec):
        return json.dumps(manager.list_descendants(), ensure_ascii=False, indent=2)

    return Tool(
        name="list_agents",
        description="List the subagents that this agent owns.",
        parameters={"type": "object", "properties": {}},
        execute=execute,
    )
