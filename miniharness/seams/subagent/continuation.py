"""可继续子代理：durable 子会话 + 冷恢复 + 结算投递 + 枚举。

上游对照：packages/subagent/subagent/src/continuation.ts（可继续子代理
管理器 startContinuable / sendMessage / interrupt / coldResume /
listChildren / listDescendants）+ subagent-in-process-driver（进程内
驱动）+ subagent-spawn-in-process（进程内 spawn 后端）+
tool-subagent-report + tool-subagent-control（send_message /
interrupt_agent / list_agents）+ AgentLoop 干预面。

mini 的落点（决策见 status/mini-harness/migration-log.md A7/A8）：
  * startContinuable 只做 durable 创建（durable before dispatch）：header
    declare（meta）+ 父 completed-turn 前缀 seed + `subagent/descriptor`
    事件，全部先落盘；此后每次 sendMessage 冷恢复该子会话。
  * coldResume：inspect → meta.parentSession 校验（UNAUTHORIZED）→ 折叠
    描述符（mode != 'continuable' → NOT_RESUMABLE）→ 以持久化事件为 seed
    重建会话、上下文与组合。
  * 结算措辞 / subagent-settled 消息格式逐字对齐上游（settlementSummary）。

A8 起执行模型对齐上游异步事件驱动，由"父是否有 driver"自动路由双路径：
  * **同步路径**（父无 driver，A7 行为保留）：sendMessage 在调用栈内同步
    跑完子回合（子 AgentLoop 复用父 _pump）；激活只活在 _activations 内，
    结算在 pump 返回后计算并整体 flush 子会话。
  * **异步路径**（父有 driver，A8 语义）：sendMessage 投递即返回 message id；
    子 driver 在事件循环上跑回合（一次 residency epoch 可跑多个 FIFO turns），
    watchSettlement（when_idle_async + poke 竞速）在真静默后结算，结算投递
    父（idle→followup / running→steer 批内合并）；interrupt 缺省 no-op；
    再投递并入既有激活；与结算竞速的投递冷恢复新激活重投不丢消息。
  * 投递规则（结算通知与 report wakeup 同规则）：父 idle → followup（唤醒
    新回合）；父 running → driver 模式 steer / 同步模式 inbox.append（非唤醒，
    下一步边界捡起）。reportDelivery 'background' → 一律 inbox.append。

简化标注（须在文档中标注；对齐粒度见 AGENTS.md 差异清单）：
  * **无 subagent/start|end 生命周期 emit 事件**（上游经 ctx.subagents 生命
    周期发射器发布），结算经 user/message 消息投递。
  * **droppedUnrun 恒 false**：mini 从不裁剪 inbox（无 agent/inbox/spliced），
    结算照常进行，completed 不再被重判为 aborted。
  * **无 sendWaking/admitWaking**：子会话不装 send_message 等控制工具 → 无
    嵌套 → 父恒无激活（ownedChildren/acquireOwnership/releaseOwnership 为
    占位，恒空/no-op）；结算投递直接 followup/steer。
  * **interrupt 无授权矩阵**（上游 ancestor/user authority 校验），mini 仅父
    调用方，无 UNAUTHORIZED。
  * **子事件仅在 settle 时整体 flush**（上游 flushFinalState best-effort +
    崩溃 torn 修复 commitRepair 仍不在 mini，A7 已注）。
  * LLM 流式已 async 化（2026-08-18 asyncio 化重构），DeepSeek SSE 仍为阻塞读
    线程桥接（不可中断，超时兜底）——异步窗口真异步，流式本身受载体限制。
  """
from __future__ import annotations

import asyncio
import json
import threading
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
from .descriptor import CONTINUATION_PROVIDER, fold_subagent_descriptor, seed_descriptor_turn
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
    MAX_DEPTH_EXCEEDED / UNAVAILABLE）。"""

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
        # 每 child 一把锁：串行化跨线程的 resume 与 settle 临界区
        # （同步调用方的 send_message / 内联泵 与事件循环线程的 watcher 结算并发）
        self._locks: dict[str, threading.Lock] = {}

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
        # 描述符对齐上游 descriptor.ts schema：{version, mode, provider, label?,
        # agentProvider?, agentModel?, persona?, toolFilter?}（无 kind 字段）
        descriptor: dict[str, Any] = {
            "mode": "continuable",
            "provider": CONTINUATION_PROVIDER,
            "label": label or "",
            "agentProvider": getattr(self.parent.adapter, "provider", None),
            "agentModel": getattr(self.parent.adapter, "model", None),
        }
        if persona:
            descriptor["persona"] = persona
        if tool_filter:
            # 上游 toolFilter 形状：{allow?: string[], deny?: string[]}
            descriptor["toolFilter"] = {"allow": list(tool_filter)}

        child_session = Session(child_id, seed=seed, meta=meta)
        seed_descriptor_turn(child_session, descriptor)
        self.persistence.declare(child_id, meta, created_at=child_session.created_at)
        self._persist_delta(child_session, start=0)
        self._persisted[child_id] = len(child_session.events)
        return child_id

    def send_message(self, child_id: str, message: str | dict, source: str = "parent") -> str:
        """向子代理投递一条消息，返回 message id。

        同步门面（无驱动模式）：子回合同步 pump 跑完再结算；父有 driver 时
        走 A8 异步路径（投递即返回）。事件循环内调用且父无 driver 时请用
        send_message_async（内联泵保证确定性结算）。
        """
        activation = self._get_or_resume(child_id)
        msg = message if isinstance(message, dict) else create_message(
            "user", [text_block(message)], {"kind": "user"},
        )
        if self.parent._driver is not None:
            self.parent._loop.call_soon_threadsafe(self._submit_on_loop, child_id, activation, msg)
        else:
            self._submit_sync(child_id, activation, msg)
        return msg["id"]

    async def send_message_async(self, child_id: str, message: str | dict,
                                 source: str = "parent") -> str:
        """async 工具契约入口：事件循环内且父无 driver 时内联泵子回合。

        旧实现经 _pump_sync_facade 的 in-loop 兜底起 fire-and-forget 子
        driver，与父瞬态 asyncio.run 的拆除竞速（子回合未完、结算丢失）。
        此处直接内联 `await child._pump_async()`，子 turn/end 先于工具返回
        落盘，结算确定性（对齐 A7 同步语义的循环内版本）。
        """
        activation = self._get_or_resume(child_id)
        msg = message if isinstance(message, dict) else create_message(
            "user", [text_block(message)], {"kind": "user"},
        )
        if self.parent._driver is not None:
            self.parent._loop.call_soon_threadsafe(self._submit_on_loop, child_id, activation, msg)
        else:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                self._submit_sync(child_id, activation, msg)
            else:
                await self._submit_async(child_id, activation, msg)
        return msg["id"]

    def _submit_sync(self, child_id: str, activation: dict, msg: dict) -> None:
        """同步路径（A7）：子 followup 同步 pump 跑完整个回合，返回后结算。"""
        child = activation["loop"]
        try:
            child.followup(msg)
        except Exception:
            pass
        finally:
            self._settle(child_id, activation)

    async def _submit_async(self, child_id: str, activation: dict, msg: dict) -> None:
        """瞬态循环内联泵：async 工具在循环内调用且父无 driver 时，
        直接内联跑完子回合再结算（确定性，子 turn/end 先于工具返回落盘）。"""
        child = activation["loop"]
        try:
            child.inbox.append("next-turn", msg)
            child._parked = False
            await child._pump_async()
        except Exception:
            pass
        finally:
            self._settle(child_id, activation)

    def _submit_on_loop(self, child_id: str, activation: dict, msg: dict) -> None:
        """异步路径（A8）：事件循环线程上的投递提交（call_soon_threadsafe 进入）。

        与结算竞速（激活已被 watcher 结算弹出）→ 就地冷恢复新激活重投，不丢
        消息（对齐上游 followup 对 disposal 的"等释放后冷恢复"）。首次投递时
        装配 watcher + 子 driver + claimed 钩子。
        """
        if self._activations.get(child_id) is not activation or activation.get("disposal") is not None:
            activation = self._get_or_resume(child_id)
        if not activation["watched"]:
            activation["watched"] = True
            activation["poke"] = asyncio.Event()
            activation["loop"].start_driver()
            activation["loop"].on_message_claimed(self._claimed_hook(child_id))
            activation["watcher"] = asyncio.ensure_future(self._watch_settlement(child_id))
        activation["accepted"].add(msg["id"])
        activation["poke"].set()            # 对齐 admitWaking 的 wake
        activation["loop"].followup(msg)    # driver 模式：入队 + 唤醒，不阻塞
        activation["status"] = "running"

    def _claimed_hook(self, child_id: str) -> Callable[[dict | None], None]:
        """认领钩子：消息被子回合认领 → 从 accepted 移除 + 唤醒 watcher
        （对齐上游 agent/inbox/claimed → accepted.delete + wake）。"""
        def hook(claimed: dict | None) -> None:
            act = self._activations.get(child_id)
            if act is None or not isinstance(claimed, dict):
                return
            msg_id = claimed.get("id")
            if not msg_id:
                return
            act["accepted"].discard(msg_id)
            if act["poke"] is not None:
                act["poke"].set()
        return hook

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
            self.parent.inbox.append("next-step", message)
        else:
            self._deliver(message)

    def interrupt(self, child_id: str, cause: str = "user") -> None:
        """中断激活中的子代理（child.cancel(keep_inbox=True)）。

        对齐上游 continuation.ts:528-568：缺省目标是**接受性 no-op**（"An
        absent target is an accepted no-op without consulting the durable
        catalog"）——子未激活或已结算 → 直接返回，不抛 NOT_FOUND。
        中断后 keep_inbox 置 _parked：driver 不再自动续跑，下次 send_message
        （waking send）清 _parked 恢复驻留队列；watcher 见 aborted turn/end +
        静默后照常结算（措辞 aborted）。
        """
        activation = self._activations.get(child_id)
        if activation is None or activation.get("disposal") is not None:
            return
        activation["status"] = "stopping"
        activation["interrupted"] = True
        activation["loop"].cancel(cause, keep_inbox=True)

    def state_of(self, child_id: str) -> dict:
        """簿记查询（上游 stateOf：running = status running 或 accepted 非空；
        waiting = ownedChildren 非空；否则 settled）。无激活 → idle。"""
        act = self._activations.get(child_id)
        if act is None:
            return {"kind": "idle", "id": child_id, "label": child_id}
        if act.get("owned_children"):
            kind = "waiting"                    # 无嵌套 → 不可达（保留字段）
        elif act["status"] == "running" or act["accepted"]:
            kind = "running"
        else:
            kind = "settled"
        return {"kind": kind, "id": child_id, "label": act.get("label") or child_id}

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
        lock = self._locks.setdefault(child_id, threading.Lock())
        with lock:
            existing = self._activations.get(child_id)
            if existing is not None:
                # A8 再投递并入既有激活（同一 residency epoch；对齐上游 followup
                # 对已有激活直接 submitAdmitted）。同步模型下不可达（sendMessage
                # 串行且激活在 settle 时弹出）。
                return existing
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
        # 子作用域独立的 tools/systemPrompt 服务标签（对齐上游 agent scope 层：
        # per-agent 注册进 agent 自己的 realm，root realm 发布是进程级，冲突被拒）
        child_ctx._isolate.setdefault("tools", object())
        child_ctx._isolate.setdefault(SYSTEM_PROMPT_SERVICE, object())

        reg = ToolRegistry(child_ctx)
        # toolFilter {allow?, deny?}（上游 ToolRestriction 形状）
        tool_filter = descriptor.get("toolFilter") or {}
        allow = set(tool_filter.get("allow") or [])
        deny = set(tool_filter.get("deny") or [])
        for name in self.parent.tools.names():
            if allow and name not in allow:
                continue
            if name in deny:
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
            "persisted": persisted,          # == epoch_start：结算 delta 起点
            "descriptor": descriptor,
            # A8 事件驱动字段：
            "interrupted": False,            # interrupt 已下达（诊断用）
            "accepted": set(),               # 已投递未认领的 message id
            "poke": None,                    # asyncio.Event，_submit_on_loop 首次装配
            "disposal": None,                # 结算/关闭标记（已结算 → 不再投递）
            "watched": False,                # watcher + driver + claimed 钩子已就绪
            "owned_children": set(),         # 恒空（无嵌套，保留字段指向嵌套场景）
        }
        self._locks.setdefault(child_id, threading.Lock())
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

    async def _watch_settlement(self, child_id: str) -> None:
        """异步结算观察者（对齐上游 watchSettlement）：when_idle_async 与 poke
        竞速 → 真静默且 accepted 空 → _settle。

        poke 置位（新投递 / 认领）→ 重观 quiescence；_settle 返回 False
        （仍有 accepted 或子仍在跑）→ 等下一次 wake。
        """
        activation = self._activations[child_id]
        while True:
            if activation.get("disposal") is not None:
                return
            poke = activation["poke"]
            poke.clear()
            idle_fut = activation["loop"].when_idle_async()
            poke_task = asyncio.ensure_future(poke.wait())
            try:
                await asyncio.wait({idle_fut, poke_task}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                poke_task.cancel()
                if not idle_fut.done():
                    idle_fut.cancel()
            if activation.get("disposal") is not None:
                return
            if poke.is_set():
                continue            # 有新投递/认领：重观 quiescence
            if self._settle(child_id, activation):
                return
            await poke.wait()       # accepted 非空或仍在跑：等下一次 wake

    def _settle(self, child_id: str, activation: dict) -> bool:
        """结算临界区（同步、无 await；跨线程以 _locks 串行化）。

        返回 True = 已结算（activation 弹出 + 结算投递）；False = 未到结算
        时机（accepted 非空 / 子仍在跑）。
        要点：
          * epoch_start == persisted（激活物化时事件数）→ delta 只算新事件，
            不误读父 seed（A7 已立教训）。
          * accepted 排空判据：_settle 只在 idle_fut（= 驱动 _mark_quiescent，
            inbox 已排空）之后进入；此刻所有投递消息必已认领（claimed hook
            已 discard）。
          * 结算先于所有权释放：_deliver_settlement（投递父）在 pop 之后但
            "所有权释放"在 mini 是 no-op（无嵌套），投递即最后一步，顺序天然满足。
        """
        with self._locks.setdefault(child_id, threading.Lock()):
            if activation.get("disposal") is not None:
                return True
            if activation["accepted"]:
                return False                    # stateOf running：仍有未认领投递
            if not activation["loop"].when_idle():
                return False                    # 仍在跑（重启后的新回合）
            activation["disposal"] = True
            child = activation["loop"]
            epoch = child.session.events[activation["persisted"]:]  # 整 epoch delta
            stop = epoch_stop_reason(epoch)
            output = final_assistant_output(epoch)
            summary = settlement_summary(stop, child_id)
            self._persist_delta(child.session, start=activation["persisted"])
            self._persisted[child_id] = len(child.session.events)
            activation["ctx"].dispose()
            self._activations.pop(child_id, None)
        # 锁外投递：先摘激活再投递，父 pump 可对同一子代理再次 send_message
        # （此时必须冷恢复而非撞既有激活）
        self._deliver_settlement(child_id, summary, output)
        return True

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
        if self.parent._driver is not None:
            # A8 异步路径：父 idle → followup（唤醒新回合）；父 running →
            # steer（批内合并，下一步边界消费）——对齐上游 sendWaking。
            if self.parent.status == "idle":
                self.parent.followup(message)
            else:
                self.parent.steer(message)
        else:
            # A7 同步路径：父 idle → followup；父 running → 非唤醒 inbox
            # （next-step：下一步边界消费，对齐上游 inject 语义）。
            if self.parent.status == "idle":
                self.parent.followup(message)
            else:
                self.parent.inbox.append("next-step", message)

    # ---------- 子专属 report 工具 ----------

    def _report_tool(self, child_id: str) -> Tool:
        manager = self

        async def execute(args: dict, exec_: ToolExec):
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
    async def execute(args: dict, exec_: ToolExec):
        await manager.send_message_async(args["subagentId"], args["message"])
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
    async def execute(args: dict, exec_: ToolExec):
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
    async def execute(args: dict, exec_: ToolExec):
        return json.dumps(manager.list_descendants(), ensure_ascii=False, indent=2)

    return Tool(
        name="list_agents",
        description="List the subagents that this agent owns.",
        parameters={"type": "object", "properties": {}},
        execute=execute,
    )
