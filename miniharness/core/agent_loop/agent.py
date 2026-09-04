"""第 4 章：Agent Loop —— turn/step 状态机（单一 async 驱动模型 + 同步门面）。

对应 dsh 真实源码：packages/core/agent-loop。

状态机要点（与真实实现一致的契约）：
  * turn 打开于认领输入之前 —— "被拒绝的尝试"也会留下持久化记录
  * turn/step 编号从 1 起，每 turn 内 step 重置为 1（session/invariant.ts）
  * step = 一次模型请求 + 它调用的工具；正常完成落 assistant/message（V2：
    完整流压缩内嵌 stream 字段）；失败/中止 attempt 落 assistant/attempt
    （同样内嵌流，不留 surface message）——模型可见 ⟺ 已记录
  * 工具结果回灌后在同一 turn 内自动进入下一步（next-step 继续）
  * pre-step 拒绝 → turn 以 {kind:'blocked'} 结束；step/end 与 turn/end
    在 finally 中必定落日志（失败时 reason 为 {kind:'error'|'aborted'|...}）

asyncio 化（2026-08-17 重构，对齐上游异步事件驱动；设计见
status/mini-harness/asyncio-refactor-design.md）：
  * 唯一 async 引擎 _pump_async（driver 模式下由 _drive 驱动；同步门面
    经进程级常驻事件循环驱动（resident_loop.py，对齐上游 Node 单循环
    载体）——headless/demo/REPL/测试的 run/followup/steer 零改动）。
  * LLM 流式 async 迭代器；取消为协作式（asyncio.Event，对齐上游
    AbortSignal）——cancel 不杀 driver，流桥/重试等待事件驱动退出、
    工具调度器排干 started + 未启动的按模型序补合成错误；取消时按流序
    定稿可安全落盘的前缀为 interrupted assistant/message（上游
    interruptedBlocks：仅 text/reasoning，丢一切 tool-call），无可见内容
    则落 assistant/attempt。
  * start_driver() 切换到事件驱动模式：followup/steer 只入队 + 线程安全
    唤醒（_request_wake → call_soon_threadsafe），回合由 _drive 在事件
    循环上执行。
  * _drive 在"真静默"（无未消费 inbox / 无 _continue / work_event 未再置位）
    时才 _mark_quiescent 并结算 when_idle_async 等待者（关闭"steer 结算通知
    在 pump 退出后到达导致 idle waiters 早结算"的竞态）。
  * _parked：interrupt keep_inbox 的驻留队列，仅下次唤醒 send（followup/steer
    清 _parked）恢复（对齐上游"waking send resumes the parked queue"）。
  * on_message_claimed 通道：每条被认领消息（含 id）回调，供 continuation
    跟踪激活 accepted 集合（对齐上游 agent/inbox/claimed 会话事件）。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Callable

# 死循环守卫上限（对齐上游：dsh 无硬性 step 上限，由模型停止/宿主干预终止；
# mini 保留一个保守的可配置守卫避免病态情况下挂死）。可通过构造参数或
# 环境变量 MINIHARNESS_MAX_STEPS 覆盖（设为极大值即等效关闭）。
DEFAULT_MAX_STEPS = 50

from ..dsh_scope import scope_target
from ..scope import Context
from ..system_prompt import join_context_sections, render_context_sections, render_prompt
from ...llm import LlmAdapter, LlmFailure, StreamAborted
from .assistant_stream import AssistantStreamAttempt
from .inbox import Inbox
from .resident_loop import run_on_resident
from .runtime_context import RuntimeContextProjection
from .tool_calls import schedule_tool_calls
from ..session import (
    Session,
    create_message,
    derive_messages,
    text_block,
)
from ..tools import ToolExec, ToolRegistry


def canonical_header(config: dict, *, system: str = "", tools: list | None = None,
                     adapter_defaults: dict | None = None) -> dict:
    """规范化请求信封（上游 session/src/request-header.ts canonicalHeader）：
    config 透传（剔除内部键 turn/step/signal 与 None 值，保留 provider/model/
    reasoningEffort/maxTokens 等连接默认）；adapterDefaults 仅当
    reasoningEffort/maxTokens 布尔标记之一为真；system 非空才带；tools 非空才带。
    headerEquals 的 mini 版 = 字典相等。
    """
    _INTERNAL = ("turn", "step", "signal")
    # provider/model 恒携带（即便为 None，保持信封形状稳定）；其余字段仅当非 None
    cfg = {"provider": config.get("provider"), "model": config.get("model")}
    cfg.update({k: v for k, v in config.items()
                if k not in _INTERNAL and k not in ("provider", "model") and v is not None})
    header = {"config": cfg}
    if adapter_defaults and (adapter_defaults.get("reasoningEffort")
                             or adapter_defaults.get("maxTokens")):
        header["adapterDefaults"] = adapter_defaults
    if system:
        header["system"] = system
    if tools:
        header["tools"] = tools
    return header


def _replayed_next_turn(session) -> int:
    """会话日志重放续号的下一回合号（对齐 invariant.ts `nextTurn`）。

    初始 1；每条 turn/end 之后 +1；尾部存在未闭合 turn/start 时停在当前号
    （暂停/中断后由 closers 先闭合，正常不会出现）。
    """
    next_turn = 1
    open_turn = None
    for event in session.events:
        if event["type"] == "turn/start":
            open_turn = event["data"]["turn"]
        elif event["type"] == "turn/end" and open_turn is not None:
            next_turn = open_turn + 1
            open_turn = None
    return open_turn if open_turn is not None else next_turn


class _AbortProxy:
    """AbortSignal 的 asyncio 替身：aborted/event 反映宿主 loop 的取消标记。

    供 agent/request-error 的 signal 字段与 llm-retry/流桥的可取消等待使用
    （对齐上游 AbortSignal：aborted 布尔 + abort 事件；取消后不再重试）。
    event 为每轮新建的 asyncio.Event（_open_turn），置位即取消。
    """

    def __init__(self, owner: "AgentLoop"):
        self._owner = owner

    @property
    def aborted(self) -> bool:
        return self._owner._cancelled

    @property
    def event(self) -> asyncio.Event | None:
        return self._owner._cancel_event


class AgentLoop:
    def __init__(
        self,
        session: Session,
        adapter: LlmAdapter,
        tools: ToolRegistry,
        ctx: Context,
        system_prompt: str = "你是一个助手。",
        max_steps: int | None = None,
        max_parallel_tool_calls: int = 10,
    ):
        self.session = session
        self.adapter = adapter
        self.tools = tools
        # 每 agent 一个作用域（对齐上游 ReactLoopAgent 构造里 createScope(loopCtx, this)）：
        # self.ctx 是作用域子上下文，经父链继承依赖与服务；本 agent 的注册归属该作用域，
        # dispose() 逆序回滚（会话管理按 owner scope 路由 session 事件）。
        self.scope = ctx.create_scope(f"agent:{session.session_id}")
        self.ctx = self.scope
        # agent 事件载波（上游 dispatch.ts agentCarrier：scopeTarget(agent, agent)，
        # subject 与 scope 键耦合不可分歧；mini 的 scope 键是 create_scope 铸的身份键
        # 而非 agent 本体，故键取 loop 自己的 scope 键）。全部 agent/* 事件经此派发：
        # 未打标监听器（root/全局）全收，打标监听器按"载波键或其祖先"接纳——
        # 兄弟作用域隔离，事件只向上流。
        self._carrier = scope_target(self, self.scope.scope_key)
        # 会话店成员资格归本 loop 所有（上游 prepare().publish() 的 detachSession）：
        # publish() 捕获 enter 的 detach disposer，dispose() 在拆 scope 后调用——
        # 会话随 loop 生命周期进/离店。
        self._detach_session: Callable[[], None] | None = None
        # R4：agent 注册的 detach disposer（publish() 时安装；effect 已随 scope
        # 自动卸载，此字段仅用于 dispose() 显式次序保障）
        self._detach_agent: Callable[[], None] | None = None
        self.system_prompt = system_prompt
        if max_steps is not None:
            self.max_steps = max_steps
        else:
            env_val = os.environ.get("MINIHARNESS_MAX_STEPS")
            if env_val is None:
                self.max_steps = DEFAULT_MAX_STEPS
            else:
                try:
                    self.max_steps = int(env_val)
                except ValueError:
                    raise ValueError(
                        f"MINIHARNESS_MAX_STEPS 必须是非负整数，收到 {env_val!r}"
                    )
        self.max_parallel_tool_calls = max_parallel_tool_calls   # 阶段 7：并行池上限（上游 DEFAULT_MAX_PARALLEL_TOOL_CALLS）
        self.status = "idle"
        # 双队列 Inbox（上游 agent/src/inbox.ts）：followup → next-turn，
        # steer/inject → next-step；每次变更落 durable agent/inbox/spliced。
        # 通知同时派发 ctx 事件（agent/inbox/inserted|discarded|claimed）；
        # claimed 是 job wake 预算恢复的权威来源（tool-jobs 经父 scope 订阅，
        # payload {agent, message, turn} 对齐上游 runtime-types.ts）。
        self.inbox = Inbox(self.session, {
            "inserted": lambda message: self.ctx.emit(
                "agent/inbox/inserted", {"agent": self, "message": message},
                this_arg=self._carrier),
            "discarded": lambda message: self.ctx.emit(
                "agent/inbox/discarded", {"agent": self, "message": message},
                this_arg=self._carrier),
            "claimed": lambda message, turn: self.ctx.emit(
                "agent/inbox/claimed", {"agent": self, "message": message, "turn": turn},
                this_arg=self._carrier),
        })
        self._turn_open = False
        self._continue = False
        self._cancelled = False
        # turn/step 编号从 1 起（session/invariant.ts `nextTurn: 1`）；若会话
        # 日志已有回合（resume 冷重建 loop），按 invariant 语义重放续号：
        # turn/end 闭一秒之后 nextTurn +1，尾部未闭合回合则停在当前号。
        self._turn = _replayed_next_turn(self.session) - 1
        self._step = 0     # 当前已打开的 step 编号（1 起，每 turn 重置）
        self._assistant_attempt_counter = 0   # Agent 生命周期内 attempt 计数（attemptId）
        self._turn_end: dict | None = None
        self._step_signal: ToolExec | None = None   # 阶段 7：当前 step 的共享取消信号
        # 协作式取消事件（asyncio.Event，_open_turn 每轮新建）：cancel 置位，
        # llm-retry 延迟等待与流桥据此事件驱动中止（对齐上游 AbortSignal 的
        # abort 事件；不用 Task.cancel——取消是协作式的，见 asyncio 化重构设计）
        self._cancel_event: asyncio.Event | None = None
        # 重试规划器（agent/request-error 监听器）由装配方显式挂载：
        # AgentLoop 构造无副作用（迁移步骤 3，对齐上游插件 apply 时挂载）
        self._abort_proxy = _AbortProxy(self)
        self._header_baseline: dict | None = None   # request/header 频次基线（上游 requestHeaderLogged）
        self._context_baseline: dict | None = None  # request/context 频次基线（provider/model 变化时落）
        # A5：最近一次 request/header 落打印时的 surface 位置替换代数
        # （上游 agent.ts requestSurfaceGeneration，undefined 起步）
        self._request_surface_generation: int | None = None
        # A8：消息认领通道（携带被认领消息本身，含 id）——continuation 管理器
        # 据此跟踪激活的 accepted 集合；job 的 wake 预算恢复不再经此，
        # 走 ctx 事件 agent/inbox/claimed（jobs/tools.py 订阅）
        self._inbox_claimed_msg_hooks: list[Callable[[dict | None], None]] = []
        # A8 事件驱动 driver：driver 任务在事件循环上消费 inbox；followup/steer
        # 只入队 + 线程安全唤醒，_quiescent 表示真静默（inbox 排空），
        # _idle_waiters 供 when_idle_async 的等待者（上游 whenIdle 可等待版）
        self._loop: asyncio.AbstractEventLoop | None = None
        self._work_event: asyncio.Event | None = None
        self._driver: asyncio.Task | None = None
        self._quiescent = False
        self._idle_waiters: list[asyncio.Future] = []
        # interrupt keep_inbox 的驻留队列：置位后 driver 不再自动续跑，
        # 仅下次唤醒 send（followup/steer 清 _parked）恢复（上游"waking send
        # resumes the parked queue"）
        self._parked = False
        # P2-19：loop 侧 runtime-context 投影（上游 agent.ts 构造里
        # new RuntimeContextProjection(ctx, session)）——懒建于首次投影
        self._rt_projection: RuntimeContextProjection | None = None

    def publish(self, source: str = "startup") -> "AgentLoop":
        """把本 agent 发布为运行态（上游 prepare().publish()，index.ts:556-570）：

        enter 会话（捕获 detach disposer 归本 loop 所有）→ announce 公告 →
        派发 `agent/session-start`（载波路由）。announce 监听器抛错时回滚
        enter（上游同款：throwing session/created listener rolls the attach
        back）。会话必须未进店（prepare 产物）；已进店 → enter fail loud。

        @param source 发布来源（上游 'startup' / resume 等调用方措辞）。
        @returns self（便于链式装配）。
        """
        sessions = self.ctx.get("sessions")
        if sessions is None:
            raise RuntimeError(
                "publish requires the sessions service: install_sessions(ctx) first")
        self._detach_session = sessions.enter(self.session)
        try:
            sessions.announce(self.session)
        except Exception:
            detach, self._detach_session = self._detach_session, None
            detach()
            raise
        # R4：登记为 ctx.agents 的 live 实例（对齐上游 publish() 的 agents.enter +
        # announce）。安装 agents 服务的组合上下文 → 注册 + agent/created；未安装
        # （裸单测装配）则跳过，assert_live 亦随之不强制（见 core/agents.py）。
        agents = self.ctx.get("agents")
        if agents is not None:
            self._detach_agent = agents.register(self)
        self.ctx.emit("agent/session-start",
                      {"agent": self, "source": source}, this_arg=self._carrier)
        return self

    def dispose(self) -> None:
        """拆解本 agent 的生命周期（上游 dispose，index.ts:497-520）：

        cancel({kind:'disposed'}) 关闭在途回合与 inbox → 逆序回滚作用域上的
        全部注册（服务/监听器/effect；拆解后作用域拒绝进一步注册）→ detach
        会话（离店 + 补发 session/disposed）。幂等：detach disposer 单发，
        scope.dispose 幂等。

        载体差异（同步模型）：上游先 await whenIdle 再拆 scope；mini 取消是
        协作式的，运行中回合在下一检查点中止——dispose 应在静默边界调用
        （测试/shutdown 处理器均如此）。
        """
        self.cancel(cause="disposed")
        self.scope.dispose()
        detach_agent, self._detach_agent = self._detach_agent, None
        if detach_agent is not None:
            detach_agent()
        detach, self._detach_session = self._detach_session, None
        if detach is not None:
            detach()

    @property
    def id(self) -> str:
        """会话 id（上游 Agent.id 即 session id，作业按此栅栏）。"""
        return self.session.session_id

    def on_message_claimed(self, hook: Callable[[dict | None], None]) -> Callable[[], None]:
        """注册消息认领钩子：每一条被认领的消息（含 None 续步）都回调，携带消息
        本身（含 id）。A8 continuation 用它跟踪激活 accepted 集合；job 的
        wake 预算恢复走 ctx 事件 agent/inbox/claimed，不占此通道。"""
        self._inbox_claimed_msg_hooks.append(hook)
        return lambda: (self._inbox_claimed_msg_hooks.remove(hook)
                        if hook in self._inbox_claimed_msg_hooks else None)

    def _fire_inbox_claimed(self, claimed: dict | None) -> None:
        """消息被认领进 step 时触发钩子（A8 的 accepted 跟踪据此排空投递集合；
        job 的 wake 预算恢复走 ctx 事件 agent/inbox/claimed）。"""
        for hook in list(self._inbox_claimed_msg_hooks):
            try:
                hook(claimed)
            except Exception as error:
                logger = getattr(self.ctx, "logger", None)
                if logger is not None and hasattr(logger, "warn"):
                    logger.warn(f"on_message_claimed hook threw: {error}")

    # ---------- 对外入口 ----------

    def followup(self, content: str | dict, source: str = "user") -> None:
        """用户输入：先进 inbox，待 pre-step 通过后才 append 进日志。

        content 为字符串时构造文本 user 消息；为 dict 时按预建消息逐字入队
        （goal 轮次的 goal 来源消息经此喂入，对齐上游 followup(message:
        UserMessage) 全消息语义；字符串形态是 mini 简化）。

        driver 模式（A8）：入队 + 线程安全唤醒，不阻塞；非 driver 模式维持
        同步 pump。清 _parked（waking send 恢复 interrupt 驻留队列）。
        """
        message = content if isinstance(content, dict) else create_message(
            "user", [text_block(content)],
            {"kind": "user"} if source == "user" else {"kind": "plugin", "plugin": source},
        )
        self.inbox.append("next-turn", message)
        self._parked = False
        if self._driver is not None and not self._driver.done():
            self._request_wake()
        else:
            self._pump_sync_facade()

    # ---------- 干预面（第 9 章：Agent 干预面） ----------

    def steer(self, content: str | dict, source: str = "user") -> None:
        """下一 step 唤醒（上游 steer）：idle 时立即开 turn；
        running 时入 inbox，当前 step 跑完后的边界消费（同步模型下
        循环条件在每个 step 之后检查，等价"下个 step 边界"）。

        content 为字符串时构造文本 user 消息；为 dict 时按预建消息逐字入队
        （子代理结算通知经此送达 running 父，对齐上游 steer(message) 全消息
        语义）。"""
        message = content if isinstance(content, dict) else create_message(
            "user", [text_block(content)],
            {"kind": "user"} if source == "user" else {"kind": "plugin", "plugin": source},
        )
        self.inbox.append("next-step", message)
        self._parked = False
        if self._driver is not None and not self._driver.done():
            self._request_wake()
        else:
            self._pump_sync_facade()

    def inject(self, content: str | dict, source: str = "plugin") -> None:
        """非唤醒注入（上游 inject(message)）：只入 inbox，不开 turn。
        后续任一 followup/steer 触发 pump 时按 FIFO 一并消费。content 为
        字符串时构造文本 user 消息；为 dict 时按预建消息逐字入队（子代理
        结算通知经此送达 idle 父代理前的非唤醒路径）。"""
        message = content if isinstance(content, dict) else create_message(
            "user", [text_block(content)],
            {"kind": "plugin", "plugin": source},
        )
        self.inbox.append("next-step", message)

    def cancel(self, cause: str | None = None, keep_inbox: bool = False) -> None:
        """取消（上游 cancel）：清 inbox（除非 keep_inbox）+ 中止活跃回合。

        协作式取消（对齐上游 AbortSignal）：不杀 driver 任务，置位取消标记
        后由执行点自行中止——流桥/重试等待事件驱动退出、工具调度器排干
        started + 未启动的按模型序补 TOOL_ABORTED_BEFORE_DISPATCH 合成错误
        结果；turn 以 {kind:'aborted', reason:{kind: cause}} 闭合；无活跃
        回合且 inbox 为空 → idle no-op。线程安全：从 executor 线程调用时
        经 call_soon_threadsafe 置位事件。
        A8：keep_inbox 且 inbox 非空 → 置 _parked（驻留队列不自动续跑，
        仅下次唤醒 send 恢复）。
        """
        if not self._turn_open and not self.inbox:
            return
        if keep_inbox and self.inbox:
            self._parked = True
        else:
            self.inbox.clear()
            self._parked = False
        if self._turn_open:
            self._cancelled = True
            # 对齐上游：turn/end {kind:'aborted', reason: AgentCancelCause}
            # （session/types.ts:158；cause 默认 user，与上游 cancel() 默认一致）
            self._turn_end = {"kind": "aborted", "reason": {"kind": cause or "user"}}
            if self._step_signal is not None:
                self._step_signal.signal.set()
            self._set_cancel_event()

    def _set_cancel_event(self) -> None:
        """线程安全地置位本轮的取消事件（跨线程经 call_soon_threadsafe）。"""
        event = self._cancel_event
        if event is None:
            return
        loop = self._loop
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if loop is not None and loop is not current and not loop.is_closed():
            loop.call_soon_threadsafe(event.set)
        else:
            event.set()

    def when_idle(self) -> bool:
        """quiescence（上游 whenIdle）：无活跃回合即 idle。
        同步模型下没有"在飞 step"，status 检查即为整机静默判定。"""
        return self.status == "idle" and not self._turn_open

    # ---------- 事件驱动 driver（A8：异步事件驱动对齐的前置） ----------

    def start_driver(self) -> None:
        """把本 loop 切换到事件驱动模式：driver 任务在事件循环上消费 inbox。

        必须在事件循环内调用；幂等。driver 模式下 followup/steer 只入队 +
        线程安全唤醒（不再同步 pump），回合由 _drive 在循环上执行。
        """
        self._ensure_driver()

    def _ensure_driver(self) -> None:
        """确保 driver 在运行：当前 loop 上绑定 _loop/_work_event 并创建驱动任务。

        driver 已存活（未 done）时 no-op；否则（未起 / 已结束）重建。
        """
        self._loop = asyncio.get_running_loop()
        if self._work_event is None:
            self._work_event = asyncio.Event()
            self._quiescent = True    # 启动即静默（无待处理工作）
        if self._driver is None or self._driver.done():
            self._driver = self._loop.create_task(self._drive())

    def _pump_sync_facade(self) -> None:
        """同步门面：无活跃 driver 时驱动完整回合（对齐旧同步 pump 语义）。

        当前线程有运行 loop 且无 driver → 起 driver + 唤醒（fire-and-forget，
        无法阻塞当前 loop；现有调用方无此路径，标注为兜底）；否则提交到
        进程级常驻事件循环阻塞至完成（resident_loop.py，对齐上游 Node
        常驻单循环载体；异常向上抛，对齐 followup 冒泡 LlmFailure 的既有契约）。
        """
        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False
        if in_loop:
            self._ensure_driver()
            self._request_wake()
        else:
            run_on_resident(self._pump_async())

    def when_idle_async(self) -> asyncio.Future:
        """驱动静默可等待版（上游 whenIdle）：驱动排空全部工作后 resolve。

        返回可取消的 future（watcher 竞速用，asyncio.wait 会取消落败方）；
        仅 driver 模式可用。错误回合不影响 resolve（回合已以 error turn/end
        闭合，对齐上游 whenIdle 不受回合错误影响）。
        """
        if self._driver is None:
            raise RuntimeError("when_idle_async 需要先调用 start_driver()")
        if self.when_idle() and self._quiescent:
            fut = self._loop.create_future()
            fut.set_result(True)
            return fut
        fut = self._loop.create_future()
        fut.add_done_callback(lambda f: (self._idle_waiters.remove(f)
                                         if f in self._idle_waiters else None))
        self._idle_waiters.append(fut)
        return fut

    def _request_wake(self) -> None:
        """线程安全唤醒 driver（executor 线程的 followup/steer 也安全）。

        同步清除 _quiescent：紧跟其后的 when_idle_async 不应在唤醒回调尚未
        执行时走"已静默"快捷路径（关闭 followup 后立即等静默的竞态窗口）。
        """
        self._quiescent = False
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._wake_now)

    def _wake_now(self) -> None:
        self._quiescent = False
        if self._work_event is not None:
            self._work_event.set()

    async def _drive(self) -> None:
        """驱动主循环：等 work_event → 跑 pump 直至排空 → 结算 idle waiters。

        仅在"真静默"（无未消费 inbox / 无 _continue / work_event 未再置位）
        时机到 _mark_quiescent，避免 when_idle_async 在 turn 运行中到达的
        steer/结算通知尚未被消费时提前返回（executor 先 append 后 wake 的
        happens-before 关系保证 inbox 检查必然看到该消息）。
        """
        while True:
            await self._work_event.wait()
            self._work_event.clear()
            self._quiescent = False
            # 内层 while：一次 wake 可排空多个回合（turn 按 next-turn 逐个消费，
            # 回合闭合后 next-turn 仍可能有余量）——对齐上游 kick 的
            # `while (await this.turn()) {}`，无需等待新 wake。
            while (self.inbox or self._continue) and not self._cancelled and not self._parked:
                try:
                    await self._pump_async()
                except Exception:
                    break   # 回合错误：驱动排空结束（对齐上游 kick 吞错后 idle），inbox 留给下次 wake
            if self.inbox or self._continue or self._work_event.is_set():
                continue   # 还有未消费工作（含 parked/cancelled 待下次 wake）：不结算 quiescence
            self._quiescent = True
            waiters, self._idle_waiters = self._idle_waiters, []
            for fut in waiters:
                if not fut.done():
                    fut.set_result(True)

    def run_maintenance(self, task: Callable[[], Any]) -> Any:
        """维护任务（上游 runMaintenance）：仅 true idle 下执行，
        不落 turn 日志、不产生会话事件；执行期间 status='maintenance'。"""
        if not self.when_idle():
            raise RuntimeError("run_maintenance 要求 true idle（无活跃回合）")
        self.status = "maintenance"
        try:
            return task()
        finally:
            self.status = "idle"

    def run(self, content: str) -> str:
        """同步跑完一次输入，返回最终 assistant 文本。"""
        self.followup(content)
        return self.last_response()

    async def run_async(self, content: str) -> str:
        """阶段 7：async 跑完一次输入（真并行工具执行路径）。

        A8：driver 已起 → 经 driver（入队 + 唤醒 + 等静默；回合错误吞入
        turn/end，不向上抛）；未起 → 直接 _pump_async（异常向上抛，对齐
        test_parallel 既有语义）。
        """
        message = create_message(
            "user", [text_block(content)], {"kind": "user"},
        )
        self.inbox.append("next-turn", message)
        if self._driver is not None:
            self._parked = False
            self._request_wake()
            await self.when_idle_async()
        else:
            await self._pump_async()
        return self.last_response()

    def last_response(self) -> str:
        for m in reversed(derive_messages(self.session.events)):
            if m["role"] == "assistant":
                return "".join(b["text"] for b in m["content"] if b["type"] == "text")
        return ""

    # ---------- turn 生命周期 ----------

    def _set_status(self, status: str) -> None:
        """更新公开状态并在空闲/running 转换时派发 agent/status（上游 setPhase）。

        maintenance 对事件公开为 'idle'（上游 status getter 同语义），
        由 run_maintenance 直接改内部字段、不经此方法。
        """
        if self.status == status:
            return
        self.status = status
        public = "idle" if status == "maintenance" else status
        self.ctx.emit("agent/status", {"agent": self, "status": public},
                      this_arg=self._carrier)

    def _open_turn(self) -> None:
        self._set_status("running")
        self._turn += 1
        self._step = 0
        self._turn_open = True
        self._turn_end = None
        # 每轮新建取消事件（对齐上游 agent.ts:325 每 phase 新建 AbortController）；
        # 绑定当前运行循环——同步门面固定在常驻循环、run_async 直驱路径绑定
        # 调用方瞬态循环，逐轮重建规避跨 loop 复用 Event 的绑定错误
        self._cancel_event = asyncio.Event()
        if self._driver is None or self._driver.done():
            # 同步门面：无 driver 时把 _loop 刷新到常驻循环，
            # 供 cancel() 从其它线程经 call_soon_threadsafe 置位事件
            self._loop = asyncio.get_running_loop()
        self.session.append("turn/start", {"turn": self._turn})

    def _close_turn(self, reason: dict | None = None) -> None:
        if not self._turn_open:
            return
        self.session.append("turn/end", {
            "turn": self._turn,
            "reason": reason or self._turn_end or {"kind": "completed"},
        })
        self._turn_open = False
        self._cancelled = False
        self._cancel_event = None
        self._continue = False  # 回合闭合后不再有"续步"：驱动静默判定依赖它
        self._set_status("idle")

    # ---------- 主循环 ----------

    def _fire_claimed_batch(self, claimed: list[dict]) -> None:
        """逐条触发认领通知；空批次触发一次 None（对齐上游续步语义）。"""
        if not claimed:
            self._fire_inbox_claimed(None)
            return
        for message in claimed:
            self._fire_inbox_claimed(message)

    async def _pump_async(self) -> None:
        """async 主循环（asyncio 化重构后唯一 pump；driver 核心——由 _drive
        在事件循环上驱动，同步门面经一次性 asyncio.run 驱动）。"""
        steps = 0
        try:
            first = True
            while (self.inbox or self._continue) and not self._cancelled and not self._parked:
                steps += 1
                if steps > self.max_steps:
                    raise RuntimeError(f"超过最大 step 数 {self.max_steps}，疑似死循环")
                if not self._turn_open:
                    self._open_turn()
                    first = True
                target = "next-turn" if first else "next-step"
                first = False
                claimed = self.inbox.claim(target, self._turn)
                self._fire_claimed_batch(claimed)
                step_end = await self._run_step_async(claimed)
                # 只补"尚未有结局"的 step：cancel/reject/max-tokens 已先行落
                # _turn_end 的优先（max-tokens 粘滞：后续完成 step 不降级）
                if step_end is not None and self._turn_end is None:
                    self._turn_end = step_end
                if self._cancelled or self._parked:
                    break
                if step_end is not None and step_end.get("kind") == "blocked":
                    break
                if self._turn_end is not None and not self.inbox.next_step:
                    await self.ctx.aserial("agent/turn-stopping", {
                        "agent": self, "turn": self._turn, "signal": self._abort_proxy,
                    }, this_arg=self._carrier)
                if self._turn_end is not None and not self.inbox.next_step:
                    break
        except LlmFailure as e:
            self._turn_end = {"kind": "error", "error": e.failure}
            self.ctx.emit("agent/error", {"agent": self, "turn": self._turn,
                                          "step": self._step, "error": e.failure},
                          this_arg=self._carrier)
            raise
        except Exception as e:
            failure = {"code": "UNKNOWN", "message": str(e)}
            self._turn_end = {"kind": "error", "error": failure}
            self.ctx.emit("agent/error", {"agent": self, "turn": self._turn,
                                          "step": self._step, "error": failure},
                          this_arg=self._carrier)
            raise
        finally:
            if self._turn_open:
                self._close_turn(self._turn_end)

    def _project_runtime_context(self) -> dict | None:
        """P2-19：运行时上下文投影（上游 preStep 的 assemble →
        renderContextSections → project 段，agent.ts:225-234）。组装
        systemPrompt 服务的 contexts 渲染为节列表，经投影去重后铸快照
        user 消息；无 systemPrompt 服务时返回 None（行为与未装服务的历史
        路径完全一致）。"""
        service = self.ctx.get("systemPrompt")
        if service is None:
            return None
        assembly = service.assemble({"agent": self, "session": self.session})
        sections = render_context_sections(assembly)
        if self._rt_projection is None:
            self._rt_projection = RuntimeContextProjection(self.session)
        return self._rt_projection.project(join_context_sections(sections), sections)

    async def _run_step_async(self, claimed: list[dict]) -> dict | None:
        """async step：pre-step 走 awaterfall，工具走并行调度器。

        P2-19：pre-step waterfall 前先投影 runtime-context（上游同序）；
        默认进入的 messages = claimed + 快照消息（上游 default enter 工厂）；
        监听器显式返回 {kind:'enter', messages:[...]} 时整体替换、不追加
        （上游监听器决策同样完全接管 messages）。
        """
        context_message = self._project_runtime_context()
        decision = await self.ctx.awaterfall("agent/pre-step", {
            "messages": claimed,
            "agent": self,
            "signal": self._abort_proxy,
        }, this_arg=self._carrier)
        if isinstance(decision, dict) and decision.get("kind") == "reject":
            self._continue = False  # 复位：拒绝即终局（上游 agent.ts:267-269），避免泵循环跑无输入 step
            self._turn_end = {"kind": "blocked"}
            return {"kind": "blocked"}
        messages = []
        explicit_enter = isinstance(decision, dict) and decision.get("kind") == "enter"
        if isinstance(decision, dict):
            candidate = decision.get("messages")
            if isinstance(candidate, list):
                messages = candidate
        if context_message is not None and not explicit_enter:
            messages = [*messages, context_message]

        self._step += 1
        self._continue = False
        # A5：本 step 认领的消息里含 goal round 续跑（source.kind=='goal'）即视为
        # 显式新消息系列（对齐上游 goal-round-driver 在续跑 decision 上置
        # startsRequestSeries:true→ agent.ts buildRequest 传参）。
        starts_request_series = any(
            isinstance(m, dict) and (m.get("source") or {}).get("kind") == "goal"
            for m in claimed
        )
        try:
            tool_calls = await self._stream_step_async(messages, starts_request_series)
            concluded = await self._execute_tools_async(tool_calls)
            self._continue = bool(tool_calls)
        finally:
            self.session.append("step/end", {"turn": self._turn, "step": self._step})
        if self._turn_end is not None and self._turn_end.get("kind") == "max-tokens":
            return {"kind": "max-tokens"}
        if not tool_calls:
            return {"kind": "completed"}
        return {"kind": "completed"} if concluded else None

    async def _stream_step_async(self, messages: list,
                                 starts_request_series: bool = False) -> list[dict]:
        """落日志 + LLM 流式（async 迭代器）。返回模型产出的 tool-call 块
        列表（模型序）。

        失败恢复（阶段 4）：适配器抛 LlmFailure 时派发 agent/request-error
        waterfall（上游 agent-loop 同语义扩展点）；{kind:'retry'} → 同 step
        内重新发起模型请求（同一 messages，历史不因失败 attempt 改变；
        request/header 只落一次——上游仅在 header 变化时追加、attempt/重试不重复落）

        A5（上游 agent.ts:496-517 buildRequest）：request/header 信封除
        initial/resume/change 外新增 reason 'series' 与可选 startsSeries:true
        （RequestHeaderReason，session/types.ts:205-213）。startsSeries 由
        starts_request_series（goal round 等判定层系列边界信号）或最近一次
        header 落打印后 surface 发生位置替换（session.replace_generation 前进）
        触发，对齐上游 `startsSeries = startsRequestSeries ||
        (requestSurfaceGeneration !== surfaceGeneration)`。
        """
        self.session.append("step/start", {"turn": self._turn, "step": self._step})
        for message in messages:
            self.session.append("user/message", message, surfaceOp="append")

        # 请求信封入日志（模型可见 ⟺ 已记录；对齐上游 buildRequest：
        # agent/request waterfall 决议 config → canonicalHeader → 首落
        # initial/resume，之后仅 header 变化落 change、仅系列边界落 series；
        # request/context 在 provider/model 变化时追加；attempt/重试不重复落）
        config = self._request_config(self._turn, self._step)
        header = canonical_header(
            config,
            system=self._system_prompt_text(),
            tools=self._tool_definitions(),
            adapter_defaults=self._adapter_defaults(),
        )
        starts_series = (starts_request_series
                         or self._request_surface_generation != self.session.replace_generation)
        if self._header_baseline is None:
            resume = any(e["type"] == "request/header" for e in self.session.events)
            self.session.append("request/header", {
                "header": header, "reason": "resume" if resume else "initial",
            })
        elif header != self._header_baseline:
            data = {"header": header, "reason": "change"}
            if starts_series:
                data["startsSeries"] = True
            self.session.append("request/header", data)
        elif starts_series:
            self.session.append("request/header", {
                "header": header, "reason": "series",
            })
        self._header_baseline = header
        self._request_surface_generation = self.session.replace_generation
        context_window = getattr(self.adapter, "context_window", None)
        context = {"provider": config.get("provider"), "model": config.get("model")}
        if context_window is not None:
            context["contextWindow"] = context_window
        if context != self._context_baseline:
            self.session.append("request/context", context)
            self._context_baseline = context

        return await self._stream_attempt()

    def _derive_history(self) -> list[dict]:
        """Derive the full message list from the current session events.

        System prompt + history (derived messages).  Callers should invoke this
        inside each retry attempt so that newly‑added compaction checkpoint events
        are immediately visible.

        system 消息 = AgentLoop.system_prompt 基底 + ctx.systemPrompt 服务的
        有序非空节（\n\n 连接，对齐上游 renderPrompt 连接语义；无该服务时仅基底）。
        """
        text = self._system_prompt_text()
        system = create_message("system", [text_block(text)],
                                {"kind": "plugin", "plugin": "system-prompt"})
        history = derive_messages(self.session.events)
        return [system] + history

    def _system_prompt_text(self) -> str:
        """渲染系统提示文本：AgentLoop.system_prompt 基底 + systemPrompt 服务
        装配结果（sections 插值 variables 后按 \n\n 连接；对齐上游
        renderPrompt 连接语义；无该服务时仅基底）。
        request/header 的 system 字段与本方法同一来源，保证 header 与
        实际请求内容一致（上游 canonicalHeader 用 renderPrompt 结果）。"""
        parts = [self.system_prompt]
        system_prompt = self.ctx.get("systemPrompt")
        if system_prompt is not None:
            text = render_prompt(system_prompt.assemble(
                {"agent": self, "session": self.session}))
            if text:
                parts.append(text)
        return "\n\n".join(part for part in parts if part)

    def _request_config(self, turn: int, step: int) -> dict:
        """决议本 step 的请求 config（上游 buildRequest 的 config 环节）：
        派发 agent/request waterfall，seed = 路由 config {provider, model}
        （附 turn/step/signal 上下文键——mini 瀑布流为单值线程化，监听器
        可改写 config 以覆盖 model 等）。
        @returns 决议后的 config；provider 缺失 fail loud（上游同款报错）。
        """
        seed = {
            "provider": self.adapter.provider,
            "model": getattr(self.adapter, "model", None),
            "reasoningEffort": getattr(self.adapter, "reasoning_effort", None),
            "maxTokens": (getattr(self.adapter, "max_tokens", None)
                          or getattr(self.adapter, "_max_tokens", None)),
            "turn": turn, "step": step, "signal": self._abort_proxy,
        }
        config = self.ctx.waterfall("agent/request", seed, this_arg=self._carrier)
        if not isinstance(config, dict) or not config.get("provider"):
            raise RuntimeError(
                f'agent "{self.session.session_id}" has no provider/model: set '
                "adapter provider/model or supply both via the agent/request waterfall"
            )
        # 未设置的字段不进 config（对齐上游：仅当连接默认显式给出）
        for key in ("reasoningEffort", "maxTokens"):
            if config.get(key) is None:
                config.pop(key, None)
        return config

    def _adapter_defaults(self) -> dict | None:
        """适配器显式设置的默认值标记（上游 preparedCall.adapterDefaults）：
        maxTokens 已设 → {maxTokens: True}；reasoningEffort 非 off/已设 →
        {reasoningEffort: True}（布尔标记；'off' 视为未激活，省略）。
        """
        defaults: dict[str, bool] = {}
        if (getattr(self.adapter, "max_tokens", None) is not None
                or getattr(self.adapter, "_max_tokens", None) is not None):
            defaults["maxTokens"] = True
        re = getattr(self.adapter, "reasoning_effort", None)
        if re and re != "off":
            defaults["reasoningEffort"] = True
        return defaults or None

    async def _stream_attempt(self) -> list[dict]:
        """一次 step 内的模型请求 attempt 循环（上游 request → retry → 终局）。

        每次循环首次都会重新派生 messages，以便 compaction checkpoint
        的 surface replace 生效。流式 async 迭代 + 每 chunk 取消检查：取消
        （cancel 置位 → 流桥抛 StreamAborted 或循环提前 break）时先按流序
        定稿可安全落盘的前缀（interruptedBlocks：text/reasoning，丢一切
        tool-call），回合再以 cancel 置的 aborted 闭合；失败 attempt
        （LlmFailure / finish-error 路径）落 `assistant/attempt`（V2：内嵌
        完整流，不留 surface message）。正常完成落 `assistant/message`，
        同样内嵌 `stream: AssistantStreamRecord[]`（替代旧版逐 chunk 落盘 +
        sourceEventSeqs 引用）。
        """
        while True:
            messages = self._derive_history()
            live = AssistantStreamAttempt(
                self.session.session_id, self._assistant_attempt_counter,
                self._turn, self._step)
            self._assistant_attempt_counter += 1
            settled = False
            try:
                stream = self.adapter.stream(
                    messages, self._tool_definitions(), self._abort_proxy)
                live.start()
                async for chunk in stream:
                    live.push(chunk)
                    if self._cancelled:
                        break
            except StreamAborted:
                # 协作式取消：定稿前缀后返回空调用列表（无可见内容则落 attempt）
                self._finalize_interrupted_prefix(live)
                return []
            except LlmFailure as e:
                # 失败 attempt 落 assistant/attempt（内嵌流），再走 request-error
                if not settled:
                    live.settle("assistant/attempt",
                                lambda: self.session.append("assistant/attempt", {
                                    "turn": self._turn, "step": self._step,
                                    "stream": live.stream,
                                })["seq"])
                    settled = True
                action = await self.ctx.awaterfall("agent/request-error", {
                    "agent": self,
                    "turn": self._turn,
                    "step": self._step,
                    "provider": self.adapter.provider,
                    "failure": e,
                    "retryPolicy": getattr(self.adapter, "retry_policy", None),
                    "signal": self._abort_proxy,
                }, this_arg=self._carrier)
                if self._cancelled:
                    return []   # 取消落在恢复窗口：失败 attempt 不定稿
                if isinstance(action, dict) and action.get("kind") == "retry":
                    continue
                raise
            if self._cancelled:
                # 非桥接适配器提前 break 的取消出口
                self._finalize_interrupted_prefix(live)
                return []

            # finish 缺省 {kind: 'stop'}（上游 assembler.finish getter 缺省展开）
            finish = live.finish or {"kind": "stop"}
            if finish["kind"] == "max-tokens":
                self._turn_end = {"kind": "max-tokens"}   # max-tokens 粘滞
            # 对齐上游：finish {kind:'error'|'aborted', failure} 是带内失败路径，
            # 落 assistant/attempt（内嵌流）后走 request-error waterfall
            if finish["kind"] in ("error", "aborted"):
                failure = finish.get("failure") or {}
                exc = LlmFailure(
                    failure.get("code", "UNKNOWN"),
                    failure.get("message", "模型流在 finish 处失败"),
                    status=failure.get("status"),
                    provider_retry_after_ms=failure.get("providerRetryAfterMs"),
                    request_id=failure.get("requestId"),
                )
                if not settled:
                    live.settle("assistant/attempt",
                                lambda: self.session.append("assistant/attempt", {
                                    "turn": self._turn, "step": self._step,
                                    "stream": live.stream,
                                })["seq"])
                    settled = True
                action = await self.ctx.awaterfall("agent/request-error", {
                    "agent": self,
                    "turn": self._turn,
                    "step": self._step,
                    "provider": self.adapter.provider,
                    "failure": exc,
                    "retryPolicy": getattr(self.adapter, "retry_policy", None),
                    "signal": self._abort_proxy,
                }, this_arg=self._carrier)
                if self._cancelled:
                    return []   # 取消落在恢复窗口：失败 attempt 不定稿
                if isinstance(action, dict) and action.get("kind") == "retry":
                    continue
                raise exc

            # assistant/message 的 source 对齐上游 {kind:'model', provider, model}
            assistant_message = create_message("assistant", live.blocks(), {
                "kind": "model",
                "provider": self.adapter.provider,
                "model": getattr(self.adapter, "model", None),
            })
            message_data: dict[str, Any] = {
                "turn": self._turn, "step": self._step, "message": assistant_message,
                "stream": live.stream,
            }
            if live.usage is not None:
                message_data["usage"] = live.usage
            live.settle("assistant/message",
                        lambda: self.session.append("assistant/message", message_data,
                                                    surfaceOp="append")["seq"])

            return [
                b for b in assistant_message["content"] if b.get("type") == "tool-call"
            ]

    def _finalize_interrupted_prefix(self, live: AssistantStreamAttempt) -> None:
        """取消时定稿流前缀为 interrupted assistant/message（上游 agent.ts
        catch 路径：interruptedBlocks 非空才落 message，否则落
        assistant/attempt）。

        source 经消息工厂补 kind:'model'（与正常完成路径同形状）；
        surfaceOp append + 内嵌 stream（V2）。
        """
        if not self._cancelled:
            return
        content = live.interrupted_blocks()
        if not content:
            # 无可见内容：落 assistant/attempt（内嵌流，不留 surface message）
            live.settle("assistant/attempt",
                        lambda: self.session.append("assistant/attempt", {
                            "turn": self._turn, "step": self._step,
                            "stream": live.stream,
                        })["seq"])
            return
        message = create_message("assistant", content, {
            "kind": "model",
            "provider": self.adapter.provider,
            "model": getattr(self.adapter, "model", None),
        })
        message_data: dict[str, Any] = {
            "turn": self._turn, "step": self._step, "message": message,
            "interrupted": True, "stream": live.stream,
        }
        if live.usage is not None:
            message_data["usage"] = live.usage
        live.settle("assistant/message",
                    lambda: self.session.append("assistant/message", message_data,
                                                surfaceOp="append")["seq"])

    async def _execute_tools_async(self, tool_calls: list[dict]) -> bool:
        """并行调度器（exclusive 屏障 + 有界滚动池 + 模型序提交）。
        @returns 是否有工具结算即终结当前回合（concludes_turn）。"""
        if not tool_calls:
            return False
        self._step_signal = ToolExec(agent=self)
        try:
            concluded, _ = await schedule_tool_calls(
                self.session, self.ctx, self.tools, self._turn, self._step,
                tool_calls, self._step_signal,
                max_parallel=self.max_parallel_tool_calls,
                agent=self,
            )
            return concluded
        finally:
            self._step_signal = None

    def _tool_definitions(self) -> list[dict]:
        defs = []
        for name in self.tools.names():
            tool = self.tools.resolve(name)
            if tool:
                defs.append({
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                })
        return defs