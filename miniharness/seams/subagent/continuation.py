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
   * 生命周期边经委托父做 **scoped dispatch**（2026-08-23 对齐）：父 ctx 有
     dsh-scope 标号时以 scope_target 载体过滤监听器，无标号退化为无载体
     全量派发；`subagent/provider-removed` 边由命名 provider 注册表的注销
     发布（上游 lifecycle.ts:88 无载体边同款，逐监听器收容）。上游另有
     invariant 运行时校验系统，mini 无对应机制（架构不适用）。
   * **DRAINING 拒绝面已对齐**（2026-08-23）：drain()/drain_descendants()
     同步关闭准入（manager 级 / 按父树 scoped），assertAdmitting 在创建与
     投递边界拒绝新准入并抛 DRAINING（措辞逐字对齐 continuation.ts:855-857）；
     上游物化窗口的 in-flight 等待在 mini 同步载体无对应窗口。
    * LLM 流式已 async 化（2026-08-18 asyncio 化重构），DeepSeek SSE 仍为阻塞读
      线程桥接（不可中断，超时兜底）——异步窗口真异步，流式本身受载体限制。
   """
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from types import MappingProxyType
from typing import Any, Callable

from ...core.agent_loop.agent import AgentLoop
from ...core.dsh_scope import scope_of, scope_target
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
    "fold_consumed_work",
    "install_subagent_control_tools",
    "settlement_summary",
]

CONTEXT_SUMMARY_MAX_CHARS = 120

logger = logging.getLogger(__name__)

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

_REPORT_TOOL_NAME = "report"


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


def _reason_kind(reason: Any) -> Any:
    """turn/end reason 的 kind（dict 或裸字符串两形态）。"""
    return reason.get("kind") if isinstance(reason, (dict, MappingProxyType)) else reason


def fold_consumed_work(events) -> dict:
    """把事件流折叠成「已消费工作的交代」（上游 core/agent consumed-work.ts
    foldConsumedWork，逐语义对齐）。

    返回 {"end": 最新一个对工作有交代的 turn/end 事件或 None,
          "dropped_unrun": 该 turn 之后是否有被取消且未运行的已接受输入}。

    有交代 = 进入过 model step（step/start），或认领过 inbox 输入后以非
    completed 结束（blocked/aborted/interrupted/error 都算——拒绝同样把认领
    的输入一并丢弃）。认领批次被改写清空后正常结束的 no-step turn 不描述
    工作。agent/inbox/spliced 的 outcome=='canceled' 且 inserted 为空 → 输入
    被取消未运行；replacement（inserted 非空）让工作在新身份下继续 pending。
    """
    stepped: set = set()
    claimed: set = set()
    open_turn = None
    end = None
    dropped_unrun = False
    for ev in events:
        if not isinstance(ev, (dict, MappingProxyType)):
            continue
        etype = ev.get("type")
        data = ev.get("data") or {}
        if etype == "turn/start":
            open_turn = data.get("turn")
        elif etype == "step/start":
            stepped.add(data.get("turn"))
        elif etype == "agent/inbox/spliced":
            if data.get("removedCount") is None:
                continue
            if data.get("outcome") == "canceled":
                dropped_unrun = dropped_unrun or not data.get("inserted")
            elif open_turn is not None:
                claimed.add(open_turn)
        elif etype == "turn/end":
            turn = data.get("turn")
            open_turn = None
            was_stepped = turn in stepped
            was_claimed = turn in claimed
            stepped.discard(turn)
            claimed.discard(turn)
            kind = _reason_kind(data.get("reason"))
            if was_stepped or (was_claimed and kind != "completed"):
                end = ev
                dropped_unrun = False     # 此前的 drop 已由该 turn 自己的结局交代
    return {"end": end, "dropped_unrun": dropped_unrun}


def epoch_stop_reason(events) -> str:
    """片段 → 结算关键词（上游 lifecycle.ts epochStopReason，逐语义对齐）。

    以 fold_consumed_work 的记账 turn/end（而非裸最后一个 turn/end）为准：
    max-tokens / error 原样；aborted、interrupted 并入 aborted；blocked（pre-step
    拒绝）→ refusal；干净结束且无记账 turn 时 droppedUnrun 决定 completed 或
    aborted（已接受的工作被取消且从未运行）；未知 reason → error。
    """
    folded = fold_consumed_work(events)
    end = folded["end"]
    kind = _reason_kind(end.get("data", {}).get("reason")) if end is not None else None
    if kind == "max-tokens":
        return "max-tokens"
    if kind in ("aborted", "interrupted"):
        return "aborted"
    if kind == "blocked":
        return "refusal"
    if kind == "error":
        return "error"
    if kind in ("completed", None):
        return "aborted" if folded["dropped_unrun"] else "completed"
    return "error"


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
        # 活体注册表（对齐上游 ctx.agents 的 exact-live 校验面）：id → AgentLoop。
        # 父构造时登记；子激活物化时登记、结算弹出。interrupt 授权矩阵与
        # 投递 lineage 授权都以"注册表里的同一对象"为准。
        self._live: dict[str, AgentLoop] = {parent.id: parent}
        # 每 child 一把锁：串行化跨线程的 resume 与 settle 临界区
        # （同步调用方的 send_message / 内联泵 与事件循环线程的 watcher 结算并发）
        self._locks: dict[str, threading.Lock] = {}
        # DRAINING 准入截止（对齐上游 continuation.ts:364,840-859）：manager 级
        # 一票 + 按父树的 scoped 成员集（root id → 成员 agent id 集）
        self._draining = False
        self._closing_scopes: dict[str, set[str]] = {}
        # 命名 provider 注册表（上游 provider 注册表的最小同构面）：注销发布
        # 无载体 subagent/provider-removed 边
        self._providers: dict[str, Callable[[str], LlmAdapter]] = {}

    @property
    def activations(self) -> dict[str, dict]:
        """簿记视图：激活中的子代理（同步模型下仅 sendMessage 期间存在）。"""
        return {cid: {"status": a["status"], "label": a["label"]}
                for cid, a in self._activations.items()}

    # ---------- 准入截止与排水（对齐上游 draining / closingScopes / drain） ----------

    def assert_admitting(self, parent: AgentLoop) -> None:
        """manager 或该父树开始排水后拒绝新准入（上游 assertAdmitting，
        continuation.ts:849-859；错误码与措辞逐字）。"""
        closing = self._closing_teardown_for(parent)
        if closing is None:
            return
        if closing == "manager":
            raise SubagentError(
                "continuable subagents are draining; the operation was not admitted",
                "DRAINING",
            )
        raise SubagentError(
            f'continuable subagents below parent "{closing}" are draining; '
            "the operation was not admitted",
            "DRAINING",
        )

    def _closing_teardown_for(self, loop: AgentLoop) -> str | None:
        """关闭该 agent 世系的拆除：'manager' / scoped 根 id / None=准入开放
        （上游 closingTeardownFor，continuation.ts:840-847）。"""
        if self._draining:
            return "manager"
        lineage = self._live_lineage_ids(loop)
        for root_id, members in self._closing_scopes.items():
            if loop.id in members or root_id in lineage:
                return root_id
        return None

    def _live_lineage_ids(self, loop: AgentLoop) -> list[str]:
        """自 loop 向上的在世世系 id 链（上游 liveLineage：首元素恒为自身，
        其后每一祖先须是注册表当前条目；durable parentSession 元数据驱动）。"""
        lineage = [loop.id]
        seen = {loop.id}
        current = loop.id
        while True:
            info = self.persistence.inspect(current)
            meta = info.get("meta") if isinstance(info, dict) else None
            parent_session = meta.get("parentSession") if isinstance(meta, dict) else None
            if not isinstance(parent_session, str) or parent_session in seen:
                break
            if self._live.get(parent_session) is None:
                break
            lineage.append(parent_session)
            seen.add(parent_session)
            current = parent_session
        return lineage

    def _activation_roots(self) -> list[str]:
        """无在世持有者的激活集（上游 drain 的 roots 快照判据）。"""
        owned: set[str] = set()
        for act in self._activations.values():
            owned |= act["owned_children"]
        return [cid for cid in self._activations if cid not in owned]

    def drain(self) -> None:
        """整管理器排水（上游 ContinuationManager.drain，continuation.ts:704-718）。

        同步关闭 manager 级准入（先于任何后续物化）→ 快照根激活 → 按
        child-first 序强制结算全部激活（所有权是森林，逐轮摘可拆根；上游
        disposeRoots 递归同序）→ 任一分支失败聚合抛 ACTIVATION_TEARDOWN_FAILED。
        上游先等物化窗口收敛；mini 同步载体无 in-flight 物化窗口（简化标注）。
        """
        self._draining = True
        failures: list[BaseException] = []
        remaining = set(self._activations)
        while remaining:
            progressed = False
            for cid in sorted(remaining):
                activation = self._activations.get(cid)
                if activation is None or activation["owned_children"] & remaining:
                    continue
                try:
                    self._settle(cid, activation, force=True)
                except BaseException as error:  # noqa: BLE001 - 聚合后统一上报
                    failures.append(error)
                remaining.discard(cid)
                progressed = True
            if not progressed:
                break  # 所有权环不可能成立；防御性退出避免死循环
        if failures:
            raise SubagentError(
                f"continuable subagent teardown failed for {len(failures)} activation(s): "
                + "; ".join(str(error) for error in failures),
                "ACTIVATION_TEARDOWN_FAILED",
            )

    def drain_descendants(self, parents: list[AgentLoop]) -> None:
        """只停指定在世宿主父的 continuable 后代（上游 drainDescendants，
        continuation.ts:729-780）：这些父树的准入保持关闭直至精确父离开
        注册表；无关树与 manager 级准入不受影响。"""
        roots = {p.id for p in parents if self._live.get(p.id) is p}
        if not roots:
            return
        # 先发布 scoped 准入截止（上游 index.ts:736-738 同步序）
        for root_id in roots:
            self._closing_scopes.setdefault(root_id, {root_id})
        targets: list[str] = []
        for cid, activation in list(self._activations.items()):
            lineage = set(self._live_lineage_ids(activation["loop"]))
            owners = [rid for rid in roots
                      if activation["loop"].id != rid and rid in lineage]
            if not owners:
                continue
            targets.append(cid)
            for rid in owners:
                self._closing_scopes[rid] |= lineage
        owned_targets: set[str] = set()
        for cid in targets:
            owned_targets |= self._activations[cid]["owned_children"]
        target_roots = [cid for cid in targets if cid not in owned_targets]

        # child-first 强制结算（同 drain）；失败聚合
        failures: list[BaseException] = []
        remaining = set(target_roots)
        while remaining:
            progressed = False
            for cid in sorted(remaining):
                activation = self._activations.get(cid)
                if activation is None or activation["owned_children"] & remaining:
                    continue
                try:
                    self._settle(cid, activation, force=True)
                except BaseException as error:  # noqa: BLE001 - 聚合后统一上报
                    failures.append(error)
                remaining.discard(cid)
                progressed = True
            if not progressed:
                break
        if failures:
            raise SubagentError(
                f"continuable subagent teardown failed for {len(failures)} "
                "scoped activation(s): " + "; ".join(str(error) for error in failures),
                "ACTIVATION_TEARDOWN_FAILED",
            )

    # ---------- 创建与续跑 ----------

    def start_continuable(
        self,
        label: str | None = None,
        tool_filter: list[str] | None = None,
        persona: str | None = None,
        prompt: str | dict | None = None,
        parent: AgentLoop | None = None,
    ):
        """创建可继续子会话（durable before dispatch）。

        无 prompt（create-only，A7 兼容形态）→ 返回子 id 字符串：子会话 =
        父 completed-turn 前缀 seed + meta + 描述符事件，全部先落盘；父当前
        in-flight 回合未平衡 → seed 为空（全新子会话）。
        有 prompt（对齐上游 startContinuable：创建即投递初始委托）→ 返回
        {"childId", "messageId"}：初始 prompt 经 send_message 同一条投递路径
        进入子 inbox（接受边界返回 message id；同步门面会内联跑完首回合）。
        @param parent - 委托父（嵌套续跑时为子代理自身的 loop）；缺省顶层父。
        """
        parent = parent or self.parent
        self.assert_admitting(parent)
        depth = delegation_depth_of(parent)
        if depth >= self.max_depth:
            raise SubagentError(
                f"子代理嵌套深度 {depth} 达到上限 {self.max_depth}", "MAX_DEPTH_EXCEEDED",
            )
        child_id = "child-" + uuid.uuid4().hex[:12]
        child_depth = depth + 1
        seed = completed_turn_prefix(parent.session.events)
        meta: dict[str, Any] = {
            "parentSession": parent.id,
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
            "agentProvider": getattr(parent.adapter, "provider", None),
            "agentModel": getattr(parent.adapter, "model", None),
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
        if prompt is None:
            return child_id
        message_id = self.send_message(child_id, prompt, source="parent", parent=parent)
        return {"childId": child_id, "messageId": message_id}

    def send_message(self, child_id: str, message: str | dict, source: str = "parent",
                     parent: AgentLoop | None = None) -> str:
        """向子代理投递一条消息，返回 message id。

        同步门面（无驱动模式）：子回合同步 pump 跑完再结算；父有 driver 时
        走 A8 异步路径（投递即返回）。事件循环内调用且父无 driver 时请用
        send_message_async（内联泵保证确定性结算）。
        @param parent - 委托父（授权与所有权主体）；缺省顶层父。嵌套续跑时
            为发起委托的子代理 loop（其激活将 owned_children 记账孙代）。
        """
        parent = parent or self.parent
        self.assert_admitting(parent)
        self._authorize_lineage(parent, child_id)
        # 所有权先于子物化（上游 submit 顺序）：父正在拆除 → 在建立任何
        # 激活前拒绝；物化失败回滚 owned 记账
        self._acquire_ownership(parent, child_id)
        try:
            activation = self._get_or_resume(child_id, parent)
        except BaseException:
            self._release_ownership(child_id)
            raise
        msg = message if isinstance(message, dict) else create_message(
            "user", [text_block(message)], {"kind": "user"},
        )
        if parent._driver is not None:
            parent._loop.call_soon_threadsafe(self._submit_on_loop, child_id, activation, msg, parent)
        else:
            self._submit_sync(child_id, activation, msg, parent)
        return msg["id"]

    async def send_message_async(self, child_id: str, message: str | dict,
                                 source: str = "parent",
                                 parent: AgentLoop | None = None) -> str:
        """async 工具契约入口：事件循环内且父无 driver 时内联泵子回合。

        旧实现经 _pump_sync_facade 的 in-loop 兜底起 fire-and-forget 子
        driver，与父瞬态 asyncio.run 的拆除竞速（子回合未完、结算丢失）。
        此处直接内联 `await child._pump_async()`，子 turn/end 先于工具返回
        落盘，结算确定性（对齐 A7 同步语义的循环内版本）。
        """
        parent = parent or self.parent
        self.assert_admitting(parent)
        self._authorize_lineage(parent, child_id)
        # 所有权先于子物化（上游 submit 顺序）：父正在拆除 → 在建立任何
        # 激活前拒绝；物化失败回滚 owned 记账
        self._acquire_ownership(parent, child_id)
        try:
            activation = self._get_or_resume(child_id, parent)
        except BaseException:
            self._release_ownership(child_id)
            raise
        msg = message if isinstance(message, dict) else create_message(
            "user", [text_block(message)], {"kind": "user"},
        )
        if parent._driver is not None:
            parent._loop.call_soon_threadsafe(self._submit_on_loop, child_id, activation, msg, parent)
        else:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                self._submit_sync(child_id, activation, msg, parent)
            else:
                await self._submit_async(child_id, activation, msg, parent)
        return msg["id"]

    # ---------- 投递记账原语（上游 submit / admitWaking / wake / 所有权） ----------

    def _authorize_lineage(self, parent: AgentLoop, child_id: str) -> None:
        """投递授权（上游 authorizeLineage）：exact-live 调用方 + durable 直属
        亲缘。其余 agent/祖先/宿主一律 UNAUTHORIZED。"""
        if self._live.get(parent.id) is not parent:
            raise SubagentError(
                f'subagent "{child_id}" delivery requires the exact live parent agent',
                "UNAUTHORIZED",
            )
        info = self.persistence.inspect(child_id)
        meta = info.get("meta")
        if not isinstance(meta, dict) or meta.get("parentSession") != parent.id:
            raise SubagentError(
                f'subagent "{child_id}" belongs to another parent session', "UNAUTHORIZED",
            )

    def _acquire_ownership(self, parent: AgentLoop, child_id: str) -> None:
        """把子登记进 continuation 托管父的 owned 集合，使该父在子在世期间
        无法被判定 settled（上游 acquireOwnership）。非托管 Agent 无激活，
        留在等待图之外；正在拆除的父 → ACTIVATION_CLOSING。"""
        pact = self._activations.get(parent.id)
        if pact is None:
            return
        if pact.get("disposal") is not None:
            raise SubagentError(
                f'subagent parent "{parent.id}" is being disposed; the child was not established',
                "ACTIVATION_CLOSING",
            )
        pact["owned_children"].add(child_id)

    def _release_ownership(self, child_id: str) -> None:
        """从在世持有者摘除并唤醒其结算观察者重观 quiescence（上游
        releaseOwnership：ownedChildren.delete + wake）。"""
        for act in list(self._activations.values()):
            if child_id in act["owned_children"]:
                act["owned_children"].discard(child_id)
                self._wake(act)

    def _wake(self, activation: dict) -> None:
        """让结算观察者重观 quiescence（上游 wake：poke.resolve + 新鲜 poke）。"""
        poke = activation.get("poke")
        if poke is not None:
            poke.set()

    def _admit_waking(self, activation: dict, message_id: str, send: Callable[[], None]) -> str:
        """把一次 waking send 记账进激活的结算窗口（上游 admitWaking）：先
        accepted.add 再 send——followup 同步发布 inbox 事件，观察者必须在
        调用前就看到 busy；send 抛错回滚 accepted；成功后唤醒观察者。"""
        activation["accepted"].add(message_id)
        try:
            send()
        except Exception:
            activation["accepted"].discard(message_id)
            raise
        self._wake(activation)
        return message_id

    def _send_waking(self, parent: AgentLoop, message: dict, send: Callable[[], None]) -> None:
        """对父执行一次 waking send 并记入父自身激活的结算窗口（上游
        sendWaking）：父有在世激活且对象同一 → admitWaking 记账后发送；否则
        直接发送（顶层/非托管父无窗口可记）。"""
        pact = self._activations.get(parent.id)
        if pact is not None and pact["loop"] is parent:
            self._admit_waking(pact, message["id"], send)
        else:
            send()

    def _submit_sync(self, child_id: str, activation: dict, msg: dict,
                     parent: AgentLoop | None = None) -> None:
        """同步路径（A7）：子 followup 同步泵跑完整个回合，返回后结算。

        子运行失败（pump 落 error turn/end 后重抛，载体差异）在此收容——
        不向委托调用方冒泡；授权/所有权拒绝在物化前已抛出。finally 保证
        激活簿记无论如何收敛。"""
        parent = parent or self.parent
        try:
            self._admit(child_id, activation, msg, parent,
                        lambda: activation["loop"].followup(msg))
        except Exception:
            pass
        finally:
            self._settle(child_id, activation)

    async def _submit_async(self, child_id: str, activation: dict, msg: dict,
                            parent: AgentLoop | None = None) -> None:
        """瞬态循环内联泵：async 工具在循环内调用且父无 driver 时，
        直接内联跑完子回合再结算（确定性，子 turn/end 先于工具返回落盘）。"""
        parent = parent or self.parent
        child = activation["loop"]

        def send() -> None:
            child.inbox.append("next-turn", msg)
            child._parked = False

        self._admit(child_id, activation, msg, parent, send)
        try:
            await child._pump_async()
        except Exception:
            pass
        finally:
            self._settle(child_id, activation)

    def _admit(self, child_id: str, activation: dict, msg: dict,
               parent: AgentLoop, send: Callable[[], None]) -> None:
        """投递提交共用序列（上游 submit）：所有权先于消息入箱 → 投递。

        driver 路径走 admitWaking 记账（先 accepted.add 再 send，同步失败
        回滚），成功后 announced=True；同步路径入箱即交代——followup 内部
        先 inbox.append 再泵，pump 中途失败时消息已认领、工作真实发生，
        调用方也照常拿到 message id，故 announced 先置位再投递。"""
        self._acquire_ownership(parent, child_id)
        if parent._driver is not None:
            self._admit_waking(activation, msg["id"], send)
            activation["announced"] = True
        else:
            activation["announced"] = True
            send()
        activation["status"] = "running"

    def _submit_on_loop(self, child_id: str, activation: dict, msg: dict,
                        parent: AgentLoop | None = None) -> None:
        """异步路径（A8）：事件循环线程上的投递提交（call_soon_threadsafe 进入）。

        与结算竞速（激活已被 watcher 结算弹出）→ 就地冷恢复新激活重投，不丢
        消息（对齐上游 followup 对 disposal 的"等释放后冷恢复"）。首次投递时
        装配 watcher + 子 driver + claimed 钩子。
        """
        parent = parent or self.parent
        if self._activations.get(child_id) is not activation or activation.get("disposal") is not None:
            activation = self._get_or_resume(child_id, parent)
        if not activation["watched"]:
            activation["watched"] = True
            activation["poke"] = asyncio.Event()
            activation["loop"].start_driver()
            activation["loop"].on_message_claimed(self._claimed_hook(child_id))
            activation["watcher"] = asyncio.ensure_future(self._watch_settlement(child_id))

        def send() -> None:
            activation["loop"].followup(msg)    # driver 模式：入队 + 唤醒，不阻塞

        self._acquire_ownership(parent, child_id)
        self._admit_waking(activation, msg["id"], send)
        activation["announced"] = True
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
            # wakeup 报告同样走 waking 记账（上游 deliverReport 'wakeup' →
            # sendWaking）：父自身是托管激活时先 accepted.add 再发
            self._send_waking(self.parent, message,
                              lambda: self._route_to_parent(self.parent, message))

    @staticmethod
    def _route_to_parent(parent: AgentLoop, message: dict) -> None:
        """父侧投递路由（结算通知与 wakeup report 共用）：idle → followup；
        driver 运行中 → steer（批内合并）；同步运行中 → 非唤醒 next-step
        （下一步边界消费，避免重入泵）。"""
        if parent.status == "idle":
            parent.followup(message)
        elif parent._driver is not None:
            parent.steer(message)
        else:
            parent.inbox.append("next-step", message)

    def interrupt(self, child_id: str, authority: dict) -> None:
        """中断一个在世可继续子的当前 turn（上游 continuation.ts interrupt
        授权矩阵，逐语义对齐）。

        @param authority - {"kind": "user", "parentSessionId": <durable 直属
            父会话 id>}（人类客户端呈现的父地址）或 {"kind": "ancestor",
            "agent": <exact live AgentLoop>}（recorded lineage 必须包含调用方）。
        授权规则：
          * ancestor：调用方必须是注册表里的同一活体（stale/同 id 替身 →
            UNAUTHORIZED，目标缺席也先校验——防同 id 探针）；自指 → UNAUTHORIZED。
          * user：目标的 durable parentSession 必须与呈现地址一致。
          * ancestor：目标物化时记录的 live ancestry 必须包含调用方。
          * 缺席目标 = 接受性 no-op（自然完成竞速 / 重复请求 / one-shot id /
            未知 id 统一覆盖，不查持久化目录）；disposal 已开同样 no-op。
        效果：cancel({cause}, keep_inbox=True) 同步返回不等静默；user → cause
        'user'、ancestor → cause 'parent'。中断后 _parked 驻留，下次 waking
        send 恢复；watcher 见 aborted turn/end 后照常结算。
        """
        if authority.get("kind") == "ancestor":
            caller = authority.get("agent")
            if not isinstance(caller, AgentLoop) or self._live.get(caller.id) is not caller:
                raise SubagentError(
                    f'interrupting "{child_id}" requires the exact live ancestor agent',
                    "UNAUTHORIZED",
                )
            if caller.id == child_id:
                raise SubagentError(
                    f'agent "{caller.id}" cannot interrupt itself', "UNAUTHORIZED",
                )
        activation = self._activations.get(child_id)
        if activation is None:
            return
        if authority.get("kind") == "user":
            if activation["loop"].session.meta.get("parentSession") != authority.get("parentSessionId"):
                raise SubagentError(
                    f'subagent "{child_id}" belongs to another parent session',
                    "UNAUTHORIZED",
                )
        else:
            caller = authority.get("agent")
            if caller not in activation["ancestry"]:
                raise SubagentError(
                    f'subagent "{child_id}" is not a live descendant of agent "{caller.id}"',
                    "UNAUTHORIZED",
                )
        # disposal 已整体停过目标；再 cancel 只是对关闭中 handle 的冗余信号
        if activation.get("disposal") is not None:
            return
        activation["interrupted"] = True
        activation["loop"].cancel(
            "user" if authority.get("kind") == "user" else "parent", keep_inbox=True,
        )

    def state_of(self, child_id: str) -> dict:
        """簿记查询（上游 stateOf 词汇与判定顺序）：running = agent status
        running 或 accepted 非空（Agent.status 单独不足凭——已接受的 waking
        send 与微任务认领之间仍是 idle）；waiting = ownedChildren 非空；否则
        settled。无激活 → idle。"""
        act = self._activations.get(child_id)
        if act is None:
            return {"kind": "idle", "id": child_id, "label": child_id}
        if act["loop"].status == "running" or act["accepted"]:
            kind = "running"
        elif act.get("owned_children"):
            kind = "waiting"                    # 静默但仍有未拆除的 owned 子
        else:
            kind = "settled"
        return {"kind": kind, "id": child_id, "label": act.get("label") or child_id}

    # ---------- 枚举 ----------

    def list_children(self) -> list[dict]:
        """直属子代理：meta.parentSession == 本父的所有持久化子会话 + 激活中。"""
        return [self._child_entry(cid) for cid in self._known_child_ids()]

    def list_descendants(self) -> list[dict]:
        """全部后代：从本父出发沿持久化 meta.parentSession 链 BFS（嵌套续跑
        后孙代及更深后代的 parentSession 指向各自直属父）。"""
        headers = {}
        for header in self.persistence.list_headers():
            hid = header.get("id")
            meta = header.get("meta")
            if hid and isinstance(meta, dict):
                headers[hid] = meta
        descendants: list[str] = []
        frontier = [self.parent.id]
        seen = {self.parent.id}
        while frontier:
            current = frontier.pop()
            for cid, meta in headers.items():
                if cid in seen or meta.get("parentSession") != current:
                    continue
                seen.add(cid)
                descendants.append(cid)
                frontier.append(cid)
        return [self._child_entry(cid) for cid in sorted(descendants)]

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

    def _get_or_resume(self, child_id: str, parent: AgentLoop | None = None) -> dict:
        parent = parent or self.parent
        lock = self._locks.setdefault(child_id, threading.Lock())
        with lock:
            existing = self._activations.get(child_id)
            if existing is not None:
                # A8 再投递并入既有激活（同一 residency epoch；对齐上游 followup
                # 对已有激活直接 submitAdmitted）。同步模型下不可达（sendMessage
                # 串行且激活在 settle 时弹出）。
                return existing
            return self._cold_resume(child_id, parent)

    def _cold_resume(self, child_id: str, parent: AgentLoop | None = None) -> dict:
        parent = parent or self.parent
        info = self.persistence.inspect(child_id)
        meta = info["meta"]
        if not isinstance(meta, dict) or meta.get("parentSession") != parent.id:
            raise SubagentError("子会话不存在或不属于当前父代理", "UNAUTHORIZED")
        events = info["events"]
        descriptor = fold_subagent_descriptor(events)
        if descriptor is None or descriptor.get("mode") != "continuable":
            raise SubagentError("子会话不可继续（描述符缺失或非 continuable）", "NOT_RESUMABLE")
        child_session = Session(child_id, seed=events, meta=meta)
        return self._build_activation(child_session, descriptor, persisted=len(events),
                                      parent=parent)

    def _build_activation(self, child_session: Session, descriptor: dict, persisted: int,
                          parent: AgentLoop | None = None) -> dict:
        parent = parent or self.parent
        child_id = child_session.session_id
        child_ctx = parent.ctx.create_scope(f"subagent:{child_id}")
        # 子作用域独立的 tools/systemPrompt 服务标签（对齐上游 agent scope 层：
        # per-agent 注册进 agent 自己的 realm，root realm 发布是进程级，冲突被拒）
        child_ctx._isolate.setdefault("tools", object())
        child_ctx._isolate.setdefault(SYSTEM_PROMPT_SERVICE, object())

        reg = ToolRegistry(child_ctx)
        # toolFilter {allow?, deny?}（上游 ToolRestriction 形状）；控制工具随父
        # 注册表继承 → 嵌套续跑天然可用（深度上限由 start_continuable 守门）
        tool_filter = descriptor.get("toolFilter") or {}
        allow = set(tool_filter.get("allow") or [])
        deny = set(tool_filter.get("deny") or [])
        for name in parent.tools.names():
            if allow and name not in allow:
                continue
            if name in deny:
                continue
            if name == _REPORT_TOOL_NAME:
                # 上一代子的专属 report 工具不继承（senderSessionId 绑定的是
                # 错误的直属父）；本代在下方注册自己的 report
                continue
            tool = parent.tools.resolve(name)
            if tool is not None:
                reg.register(tool)
        reg.register(self._report_tool(child_id))

        child = AgentLoop(
            child_session, self._resolve_adapter(descriptor), reg, child_ctx,
            system_prompt=parent.system_prompt,
            max_steps=parent.max_steps,
            max_parallel_tool_calls=parent.max_parallel_tool_calls,
        )
        # publish（上游 agent-loop 工厂同款）：子会话进店 + 公告 +
        # agent/session-start；店成员资格归子 loop，结算 dispose 即 detach
        child.publish()

        # 子作用域自己的 system prompt 服务（mini 的 SystemPromptService 是
        # 全局单例非 scope-aware → 子作用域提供独立实例）
        svc = SystemPromptService(child_ctx)
        child_ctx.provide(SYSTEM_PROMPT_SERVICE, svc)
        svc.section("persona", 0, descriptor.get("persona") or _DEFAULT_CHILD_PERSONA)
        svc.section("report-guidance", 117, _REPORT_GUIDANCE)
        svc.section("delegation-context", 120, lambda c: self._delegation_context(parent, child_id))

        # live ancestry（上游 ancestry WeakSet([handle.agent, *parentLineage])）：
        # 物化时刻的 exact live 祖先链，interrupt ancestor 授权与嵌套结算都用它
        pact = self._activations.get(parent.id)
        lineage = [parent]
        if pact is not None:
            lineage.extend(pact["ancestry"])
        activation = {
            "loop": child,
            "ctx": child_ctx,
            "registry": reg,
            "label": descriptor.get("label") or child_id,
            "status": "running",
            "parent_loop": parent,              # durable 直属父（结算投递目标）
            "run_id": uuid.uuid4().hex,         # 生命周期事件对的唯一标识
            "ancestry": tuple([child, *lineage]),
            "persisted": persisted,          # == epoch_start：结算 delta 起点
            "descriptor": descriptor,
            # A8 事件驱动字段：
            "interrupted": False,            # interrupt 已下达（诊断用）
            "announced": False,              # 已有 message id 交付调用方（结算须交代）
            "accepted": set(),               # 已投递未认领的 message id
            "poke": None,                    # asyncio.Event，_submit_on_loop 首次装配
            "disposal": None,                # 结算/关闭标记（已结算 → 不再投递）
            "watched": False,                # watcher + driver + claimed 钩子已就绪
            "owned_children": set(),         # 本激活委托出的、尚未结算的子代 id
        }
        self._locks.setdefault(child_id, threading.Lock())
        self._activations[child_id] = activation
        self._live[child_id] = child
        # 生命周期 start 边（上游 observer.start：任何 turn 运行前发布本 epoch）
        self._emit_lifecycle(parent, "subagent/start", {
            "runId": activation["run_id"],
            "provider": descriptor.get("provider") or CONTINUATION_PROVIDER,
            "id": child_id,
            "local": True,
        })
        return activation

    def _emit_lifecycle(self, parent: AgentLoop, name: str, info: dict) -> None:
        """生命周期边发布（上游 createLifecycleEmitter，lifecycle.ts:100-123）。

        run 边携带委托父的 scoped dispatch 载体：父 ctx 有 dsh-scope 标号 →
        scope_target(parent.ctx, key) 过滤（打标监听器须是载波键或其祖先，
        未打标监听器全局接纳）；父无标号退化为无载体全量派发（mini 顶层
        父不铸 scope 的等价语义）。逐监听器收容：同步抛错记 warn，不饿死
        同侪监听器、不改变 run。
        """
        key = scope_of(parent.ctx)
        this_arg = scope_target(parent.ctx, key) if key is not None else None
        for fn in parent.ctx._hooks_for(name, this_arg):
            try:
                fn(info)
            except Exception as error:
                logger.warn(f"subagent: {name} listener failed: {error}")

    def _emit_provider_removed(self, provider: str) -> None:
        """provider 移除边（上游 lifecycle.ts:88）：无父载体、无 scoped 过滤地
        达所有监听器；从 disposer 发布，监听器拒绝绝不破坏拆除（逐个收容）。"""
        for fn in self.parent.ctx._hooks_for("subagent/provider-removed", None):
            try:
                fn(provider)
            except Exception as error:
                logger.warn(f"subagent: subagent/provider-removed listener failed: {error}")

    def register_provider(self, name: str,
                          resolve_adapter: Callable[[str], LlmAdapter]) -> Callable[[], None]:
        """登记命名 provider（上游 provider 注册表的最小同构面）。

        resolve_adapter(model) 按模型重建适配器；_resolve_adapter 优先查注册
        表。返回同步 disposer：注销并发布 subagent/provider-removed（幂等：
        重复注销不再发布）。"""
        self._providers[name] = resolve_adapter

        def dispose() -> None:
            if self._providers.pop(name, None) is not None:
                self._emit_provider_removed(name)

        return dispose

    def _resolve_adapter(self, descriptor: dict) -> LlmAdapter:
        """按描述符重建子适配器（上游按 agentProvider/agentModel 重建 provider）。

        命名注册表优先（register_provider 面）；未命中回退 adapter_factory。
        总是新建而非复用父适配器：子代理的模型实例彼此独立（共享父适配器
        会串调用计数等状态）。provider/model 缺省继承父。
        """
        provider = descriptor.get("agentProvider") or getattr(self.parent.adapter, "provider", None)
        model = descriptor.get("agentModel") or getattr(self.parent.adapter, "model", None)
        entry = self._providers.get(provider)
        if entry is not None:
            return entry(model)
        return self._adapter_factory(provider, model)

    def _delegation_context(self, parent: AgentLoop, child_id: str) -> str:
        return (
            f"Parent session: {parent.id}. This subagent is {child_id}; "
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

    def _settle(self, child_id: str, activation: dict, force: bool = False) -> bool:
        """结算临界区（同步、无 await；跨线程以 _locks 串行化）。

        force=True 供排水路径使用：跳过 quiescence 检查强制释放（上游
        drain 的 disposeRoots 同义——cancel + 释放，不等静默）。返回 True =
        已结算；False = 未到结算时机（accepted 非空 / 子仍在跑 / 仍有 owned
        子代未拆除）。对齐上游 finishDisposal 顺序：cancel（top-down
        停止传播）→ best-effort final flush（失败仅告警，绝不阻断释放——扣住子
        代会把整条祖先链永久钉在 waiting）→ capture（终局事实趁子在册时快照）
        → handle/ctx 拆除 → 摘激活 → **notifySettlement 先于 releaseOwnership**
        （父此刻仍计着这个子、不可能被误判 settled）→ releaseOwnership →
        subagent/end 终局边（disposal 结果已知后发布一次，与 start 配对）。
        """
        with self._locks.setdefault(child_id, threading.Lock()):
            if activation.get("disposal") is not None:
                return True
            if not force:
                if activation["accepted"]:
                    return False                # stateOf running：仍有未认领投递
                if not activation["loop"].when_idle():
                    return False                # 仍在跑（重启后的新回合）
                if activation["owned_children"]:
                    return False                # stateOf waiting：owned 子未拆完
            activation["disposal"] = True
            parent = activation["parent_loop"]
            child = activation["loop"]
            epoch = child.session.events[activation["persisted"]:]  # 整 epoch delta
            failure = False
            try:
                # top-down 停止传播先于一切 await（上游 cancel({kind:'parent'})；
                # 静默子上无害）。mini 同步临界区无 await，等价于"cancel→await idle"
                child.cancel("parent")
                # best-effort final flush（上游 flushFinalState）：持久层失败只
                # 告警——teardown 必须继续，所有权必须释放
                self._persist_delta(child.session, start=activation["persisted"])
                self._persisted[child_id] = len(child.session.events)
            except Exception as error:
                failure = True                  # teardown 失败覆盖 epoch 自身结局
                logger.warn(
                    f'subagent "{child_id}" best-effort final session flush failed; '
                    f"the persisted state may be unavailable or stale on resume: {error}"
                )
            # capture：终局事实在子仍在册时算好（上游 observer.capture + terminal：
            # teardown 失败 → stopReason 'error' 且不交付输出——未能 durable 释放的
            # 回答不是结果）
            stop = "error" if failure else epoch_stop_reason(epoch)
            output = None if failure else final_assistant_output(epoch)
            try:
                # 先 loop.dispose（cancel + 拆 loop scope + detach 会话：离店 +
                # session/disposed），再拆子作用域 ctx（级联回已拆的 loop.scope，幂等）
                child.dispose()
                activation["ctx"].dispose()
            except Exception as error:
                failure = True
                stop = "error"
                logger.warn(f'subagent "{child_id}" activation teardown failed: {error}')
            self._activations.pop(child_id, None)
            self._live.pop(child_id, None)
        # 锁外收尾：先摘激活再投递，父 pump 可对同一子代理再次 send_message
        # （此时必须冷恢复而非撞既有激活）
        self._notify_settlement(activation, stop, output, child_id)
        self._release_ownership(child_id)
        # 终局边最后发布（上游 observer.settle 在 releaseOwnership 之后）
        info = {
            "runId": activation["run_id"],
            "provider": activation["descriptor"].get("provider") or CONTINUATION_PROVIDER,
            "id": child_id,
            "local": True,
            "stopReason": stop,
        }
        if output is not None:
            info["lastAssistantMessage"] = output
        self._emit_lifecycle(parent, "subagent/end", info)
        return True

    def _notify_settlement(self, activation: dict, stop: str, output: list | None,
                           child_id: str) -> None:
        """向 durable 直属父交代子的终局（上游 notifySettlement）。

        无条件面向每个拿到过 id 的子：不考虑它是否 report 过——token 上限、
        模型失败、取消、teardown 恰恰是子自己来不及选择的情形。announced=False
        （物化回滚、无人拿到 id）保持沉默。父不在世不是错误：子的 Session 本就
        是 durable 记录。投递失败记 warn 丢弃——为重试通知扣住子会把整个祖先链
        钉死在 waiting。
        """
        if not activation.get("announced"):
            return
        parent = activation.get("parent_loop")
        if parent is None:
            return
        summary = settlement_summary(stop, child_id)
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

        def send() -> None:
            # idle 父给一个普通 turn；busy 父 steer（claim 整批下一步输入，
            # 多子齐结算一次 step 消化而非一子一 turn）；steering 而非 inject
            # 关闭"driver 在 status 读取与发送之间退役致通知搁浅"的窗口
            self._route_to_parent(parent, message)

        try:
            # waking 记账：父自身也是 continuation 托管激活时，先把通知 id 记入
            # 其结算窗口再发（否则父会在 followup 与微任务认领之间被误判 quiescent）
            self._send_waking(parent, message, send)
        except Exception as error:
            logger.warn(
                f'subagent "{child_id}" settlement notice was not delivered to its '
                f"parent: {error}"
            )

    def _persist_delta(self, session: Session, start: int) -> None:
        events = session.events[start:]
        if not events:
            return
        for event in events:
            self.persistence.append(session.session_id, event)
        self.persistence.flush()

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
        # 调用方 agent 即授权与所有权主体（上游 exec.agent → followup(parent,…)）：
        # 嵌套续跑时子代理经同一工具委托孙代
        await manager.send_message_async(args["subagentId"], args["message"],
                                         parent=exec_.agent)
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
        # 服务以 exact live caller 对照目标 recorded lineage 授权（上游
        # {kind:'ancestor', agent: caller}）；工具自身不附加任何权限
        manager.interrupt(args["subagentId"], {"kind": "ancestor", "agent": exec_.agent})
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
