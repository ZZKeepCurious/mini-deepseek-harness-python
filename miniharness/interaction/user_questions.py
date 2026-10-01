"""第 9 章：用户提问 —— UserQuestionService 能力 seam + 稳定错误分类。

对应 dsh 真实源码：packages/interaction/user-questions（src/index.ts +
src/types.ts）；模型面工具独立在 tool-ask-user（interaction/tool_ask_user.py）。

上游语义（已核实，index.ts:86-151）：
  * `ctx.userQuestions` = 校验 + 作用域化 answerer 瀑布。ask() 校验序：
      signal aborted            → ASK_ABORTED
      questions 为空            → EMPTY_QUESTIONS
      agent 管线校验            → CALLER_NOT_LIVE（非 registry 精确实例）
                                 / DELEGATED_CALLER（live 但被其它 live
                                 agent 拥有——runtime ownership 决定边界，
                                 不是 durable session lineage）
      intent 校验               → BAD_INTENT（approve 标签须是自身选项之一；
                                 plan-review 必须带 detail）
      瀑布派发 'user-questions/request'（无 agent = 根瀑布；有 agent = 载波
        作用域瀑布，payload 深带 agent）→ 链端无应答者 → NO_PROVIDER
      传输恢复：带 name/message/code 的形状还原为 UserQuestionError；
        恢复后仍非域错误且 signal 已中止 → ASK_ABORTED。
  * 事件修正（已核实，账本须注明）：上游**没有** user-questions/asked|answered
    事件；只有 user-questions/request（mode:'waterfall'，api/remotes/
    remote-events.ts:37 同文件 approval/request）。
  * 挂载事实（bundle/preset，已核实）：base bundle 只挂 id:user-questions
    service seam（无 UI 应答者）；web-app 挂 ui-user-questions 应答者；
    工具 ask_user_question 仅经 agent presets 挂载；headless rides over
    dsh-base → 无工具无应答者，只经 base 有 service seam。

dsh-v0.2.0-rc.2（index.ts + projection.ts + timed-wait.ts）：
  * 新投影 `userQuestions`（stateVersion 2，projection.ts:316）：fold 会话日志的
    `request/header`（据已记录的 `ask_user_question` schema 是否声明 `timeout`
    参数判读 timed/legacy）、`tool/call`、`tool/result`、`tool/ptc-dispatch`、
    `user/message`（source.kind==='user-question-reply'）。**legacy 阻塞 schema
    下的调用永不被追踪**。pending 结果或 resume 修复的 `TOOL_OUTCOME_UNKNOWN`
    结果让调用保持可答（continued）。视图 `{active, settled}`。
  * 新服务方法：`answer(agent, callId, answer)`（steer 一条 `user-question-reply`
    来源的 user 消息并关闭投影；错误 BAD_ANSWER/REPLY_QUEUED）、
    `attach_wait(agent, callId, signal)`（产出 `{remainingMs}` 帧）、
    `ask_timed(request&{agent}, callId, timeoutMs)`（ASK_TIMED_OUT → `{pending,
    callId}`；错误 BAD_TIMEOUT/DUPLICATE_WAIT）。
  * `TimedQuestionWait`：Host 截止只在无客户端持有时计时；客户端 attach 挂起计时，
    释放后重排。

载体差异（已核实，见 verified-diffs §3）：
  * 上游是 async waterfall + AbortSignal；mini 的 Context.awaterfall 本模块
    引入可选 base 参数（对齐 sync waterfall 的链端回调）。
  * signal 鸭子类型：AbortSignal 形状读 .aborted；mini 的 threading.Event /
    FusedSignal（tools.py）读 is_set()。
  * UserQuestionError 独立于 LlmFailure：上游继承 HarnessError；mini 的
    llm 包无可子类 HarnessError（只导出 LlmFailure，具体于 LLM 失败），
    故独立 Exception 类、携带同形 wire {name, message, code}（.name 类
    属性、.code、.cause）。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Mapping

from ..core.dsh_scope import scope_target
from ..core.scope import Context, Service
from ..core.session.message import create_message, text_block
from ..core.session.types import TOOL_OUTCOME_UNKNOWN
from ..session_projection import ProjectionDefinition

__all__ = [
    "ASK_ABORTED",
    "ASK_CANCELLED",
    "ASK_TIMED_OUT",
    "BAD_ANSWER",
    "BAD_INTENT",
    "BAD_TIMEOUT",
    "CALLER_NOT_LIVE",
    "DELEGATED_CALLER",
    "DUPLICATE_WAIT",
    "EMPTY_QUESTIONS",
    "NO_PROVIDER",
    "REPLY_QUEUED",
    "TIMED_WAIT_PARAMETER",
    "USER_QUESTION_PROJECTION_KEY",
    "USER_QUESTION_STATE_VERSION",
    "UserQuestionError",
    "UserQuestionService",
    "TimedQuestionWait",
    "aborted_question",
    "apply_user_question_event",
    "fold_user_questions",
    "install_user_questions",
    "is_timed_ask_user_question_schema",
    "register_user_question_projection",
    "restore_user_question_error",
    "user_question_projection_definition",
]

#: 稳定错误分类（上游 UserQuestionError 的 code 集合，index.ts:33-47；
#: ASK_CANCELLED 由应答者侧产出，经传输恢复转送回服务面）。
ASK_ABORTED = "ASK_ABORTED"
ASK_CANCELLED = "ASK_CANCELLED"
ASK_TIMED_OUT = "ASK_TIMED_OUT"
BAD_ANSWER = "BAD_ANSWER"
BAD_INTENT = "BAD_INTENT"
BAD_TIMEOUT = "BAD_TIMEOUT"
CALLER_NOT_LIVE = "CALLER_NOT_LIVE"
DELEGATED_CALLER = "DELEGATED_CALLER"
DUPLICATE_WAIT = "DUPLICATE_WAIT"
EMPTY_QUESTIONS = "EMPTY_QUESTIONS"
NO_PROVIDER = "NO_PROVIDER"
REPLY_QUEUED = "REPLY_QUEUED"

#: 工具名（上游 projection.ts:28 ASK_USER_QUESTION_TOOL）。
ASK_USER_QUESTION_TOOL = "ask_user_question"
#: 只有 timed schema 声明的模型参数（projection.ts:36 TIMED_WAIT_PARAMETER）。
TIMED_WAIT_PARAMETER = "timeout"
#: 投影键与 stateVersion（projection.ts:317-330，stateVersion 2）。
USER_QUESTION_PROJECTION_KEY = "userQuestions"
USER_QUESTION_STATE_VERSION = 2

#: 空视图（projection.ts:113：无 timed 提问的会话共享同一引用）。
_EMPTY_VIEW: dict = {"active": [], "settled": []}


def is_timed_ask_user_question_schema(tool: Any) -> bool:
    """一个已记录的工具 schema 是否为 timed `ask_user_question`（projection.ts:126）。

    仅当 name 是 `ask_user_question` 且 parameters.properties 声明 `timeout`。
    """
    if not isinstance(tool, Mapping) or tool.get("name") != ASK_USER_QUESTION_TOOL:
        return False
    parameters = tool.get("parameters")
    properties = parameters.get("properties") if isinstance(parameters, Mapping) else None
    return isinstance(properties, Mapping) and TIMED_WAIT_PARAMETER in properties


def _questions_of(arguments_text: Any) -> list | None:
    """从已记录的 `ask_user_question` 参数读出问题批（projection.ts:137）。"""
    if not isinstance(arguments_text, str):
        return None
    try:
        parsed = json.loads(arguments_text)
    except ValueError:
        return None
    if not isinstance(parsed, Mapping):
        return None
    questions = parsed.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    projected = []
    for question in questions:
        if (not isinstance(question, Mapping)
                or not isinstance(question.get("id"), str)
                or not isinstance(question.get("question"), str)):
            return None
        item: dict[str, Any] = {"id": question["id"], "question": question["question"]}
        if question.get("header") is not None:
            item["header"] = question["header"]
        if question.get("options") is not None:
            options = []
            for option in question["options"]:
                if not isinstance(option, Mapping) or not isinstance(option.get("label"), str):
                    return None
                entry = {"label": option["label"]}
                if option.get("description") is not None:
                    entry["description"] = option["description"]
                options.append(entry)
            item["options"] = options
        if question.get("multi_select") is not None:
            item["multiSelect"] = question["multi_select"]
        elif question.get("multiSelect") is not None:
            item["multiSelect"] = question["multiSelect"]
        projected.append(item)
    return projected


def _text_of(content: Any) -> str | None:
    if not isinstance(content, (list, tuple)):
        return None
    for block in content:
        if isinstance(block, Mapping) and block.get("type") == "text" \
                and isinstance(block.get("text"), str):
            return block["text"]
    return None


def _is_pending_result(content: Any) -> bool:
    """结果文本是否是 timed 的 pending 载荷（projection.ts:164）。"""
    text = _text_of(content)
    if text is None:
        return False
    try:
        parsed = json.loads(text)
    except ValueError:
        return False
    return isinstance(parsed, Mapping) and parsed.get("pending") is True


def _answer_batch_of(content: Any) -> list | None:
    """从一段已记录文本读出答案批（工具结果或迟到回复，projection.ts:182）。"""
    text = _text_of(content)
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, Mapping) or not isinstance(parsed.get("answers"), list):
        return None
    answers = []
    for answer in parsed["answers"]:
        if not isinstance(answer, Mapping) or not isinstance(answer.get("id"), str):
            return None
        selected = answer.get("selected")
        if not isinstance(selected, list):
            return None
        entry: dict[str, Any] = {"id": answer["id"], "selected": list(selected)}
        if answer.get("custom") is not None:
            entry["custom"] = answer["custom"]
        answers.append(entry)
    return answers


def _settle_question(view: dict, call_id: str, answers: list) -> dict:
    """关闭一个可答提问并保留其结算答案（projection.ts:205）。"""
    question = next((item for item in view["active"] if item["callId"] == call_id), None)
    if question is None:
        return view
    return {
        "active": [item for item in view["active"] if item["callId"] != call_id],
        "settled": [*view["settled"], {"callId": question["callId"], "answers": answers}],
    }


def apply_user_question_event(state: dict, event: Mapping) -> dict:
    """把一条会话事件应用到 timed 提问折叠（projection.ts:235 applyUserQuestionEvent）。

    返回同一 state 引用表示未变；否则新 dict（whole-value 规则）。
    """
    if event.get("seq", 0) < state.get("inheritedEventCount", 0):
        return state
    event_type = event.get("type")
    data = event.get("data") or {}
    view = state["questions"]
    if event_type == "request/header":
        header = data.get("header") or {}
        tools = header.get("tools")
        timed = isinstance(tools, (list, tuple)) and any(
            is_timed_ask_user_question_schema(tool) for tool in tools)
        if timed == state["timed"]:
            return state
        return {**state, "timed": timed}
    if event_type == "tool/call":
        if not state["timed"] or data.get("name") != ASK_USER_QUESTION_TOOL:
            return state
        questions = _questions_of(data.get("arguments"))
        if questions is None:
            return state
        call_id = data.get("callId")
        active = [item for item in view["active"] if item["callId"] != call_id]
        active.append({"callId": call_id, "questions": questions, "state": "open"})
        return {**state, "questions": {**view, "active": active}}
    if event_type == "tool/result":
        message = data.get("message") or {}
        call_id = message.get("toolCallId")
        if not any(item["callId"] == call_id for item in view["active"]):
            return state
        if (_is_pending_result(message.get("content"))
                or (data.get("error") or {}).get("code") == TOOL_OUTCOME_UNKNOWN):
            active = [{**item, "state": "continued"} if item["callId"] == call_id else item
                      for item in view["active"]]
            return {**state, "questions": {**view, "active": active}}
        answers = (None if data.get("error") is not None or message.get("isError") is True
                   else _answer_batch_of(message.get("content")))
        if answers is None:
            active = [item for item in view["active"] if item["callId"] != call_id]
            return {**state, "questions": {**view, "active": active}}
        return {**state, "questions": _settle_question(view, call_id, answers)}
    if event_type == "tool/ptc-dispatch":
        if (data.get("name") != ASK_USER_QUESTION_TOOL or data.get("isError")
                or not _is_pending_result(data.get("content"))):
            return state
        # 上游 projection.ts:284：ptc-dispatch 携带的 arguments 是已解析对象，
        # 先序列化再交同一 JSON 解析（tool/call 分支的 arguments 本就是 JSON 文本）。
        questions = _questions_of(json.dumps(
            data.get("arguments"), ensure_ascii=False, separators=(",", ":")))
        if questions is None:
            return state
        call_id = data.get("subCallId")
        active = [item for item in view["active"] if item["callId"] != call_id]
        active.append({"callId": call_id, "questions": questions, "state": "continued"})
        return {**state, "questions": {**view, "active": active}}
    if event_type == "user/message":
        source = data.get("source") or {}
        if source.get("kind") != "user-question-reply":
            return state
        questions = _settle_question(view, source.get("callId"),
                                     _answer_batch_of(data.get("content")) or [])
        if questions is view:
            return state
        return {**state, "questions": questions}
    return state


def fold_user_questions(events) -> dict:
    """把一个完整日志折成提问视图（projection.ts:311 foldUserQuestions）。"""
    state = {"inheritedEventCount": 0, "timed": False, "questions": _EMPTY_VIEW}
    for event in events:
        state = apply_user_question_event(state, event)
    return state["questions"]


def user_question_projection_definition() -> ProjectionDefinition:
    """`userQuestions` 会话投影（projection.ts:316 userQuestionProjectionDefinition）。"""
    return ProjectionDefinition(
        key=USER_QUESTION_PROJECTION_KEY,
        init=lambda _header, inherited: {
            "inheritedEventCount": inherited, "timed": False, "questions": _EMPTY_VIEW},
        apply=apply_user_question_event,
        state_version=USER_QUESTION_STATE_VERSION,
        view=lambda state: state["questions"],
    )


def register_user_question_projection(registry) -> Callable[[], None]:
    """向 `ctx.sessionProjections` 注册 `userQuestions` 单元（幂等由注册表护）。"""
    return registry.register(user_question_projection_definition())


class UserQuestionError(Exception):
    """用户提问失败分类（name/code/cause；wire 形状 {name, message, code}）。"""

    name = "UserQuestionError"

    def __init__(self, message: str, code: str, cause: Any = None):
        super().__init__(message)
        self.code = code
        self.cause = cause


def aborted_question(cause: Any = None) -> UserQuestionError:
    """user-questions 中止错误（上游 abortedQuestion，index.ts:41-47）。"""
    return UserQuestionError(
        "ask_user_question was aborted before the user answered",
        ASK_ABORTED,
        cause=cause,
    )


def _is_record(value: Any) -> bool:
    """对象且非标量/数组（对齐上游 isRecord：`typeof === 'object'`，非数组）。

    排除 None / str / bytes / list / tuple / set / type / 数值 / 布尔；其余
    （dict、BaseException、普通对象）视为记录，字段经 _field 的 `.get` 或
    属性访问读取——与上游「对象即可」的判定面一致。
    """
    return (value is not None
            and not isinstance(value, (str, bytes, bytearray, list, tuple, set,
                                       frozenset, type, int, float, complex, bool)))


def _field(record: Any, key: str) -> Any:
    get = getattr(record, "get", None)
    if callable(get):
        return get(key)
    return getattr(record, key, None)


def restore_user_question_error(reason: Any) -> Any:
    """传输恢复：{name, message, code} 形状还原为 UserQuestionError，其余原样。"""
    if isinstance(reason, UserQuestionError):
        return reason
    if (_is_record(reason)
            and _field(reason, "name") == "UserQuestionError"
            and isinstance(_field(reason, "message"), str)
            and isinstance(_field(reason, "code"), str)):
        return UserQuestionError(_field(reason, "message"), _field(reason, "code"),
                                 cause=reason)
    return reason


def _signal_aborted(signal: Any) -> bool:
    """signal 中止判定：AbortSignal 形状读 .aborted；事件/熔合信号读 is_set()。"""
    if signal is None:
        return False
    aborted = getattr(signal, "aborted", None)
    if aborted is not None:
        return bool(aborted)
    is_set = getattr(signal, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


class _WaitSignal:
    """TimedQuestionWait 的取消句柄（AbortSignal 形状：`.aborted`/`.reason`）。"""

    def __init__(self, wait: "TimedQuestionWait"):
        self._wait = wait

    @property
    def aborted(self) -> bool:
        return self._wait.closed

    @property
    def reason(self) -> Any:
        return self._wait.reason

    def is_set(self) -> bool:
        return self._wait.closed


class TimedQuestionWait:
    """一条前台提问的生命周期与客户端持有（timed-wait.ts TimedQuestionWait）。

    Host 截止时间只在没有客户端持有时计时；客户端 attach 会挂起计时器，
    释放后按剩余时间重排。`close(reason)` 取消计时、唤醒全部 attach 与 done。
    """

    def __init__(self, deadline_ms: float, parent: Any, timeout_error: UserQuestionError):
        self.deadline = deadline_ms
        self._parent = parent
        self._timeout = timeout_error
        self._closed = False
        self._reason: Any = None
        self._claims: set = set()
        self._timer = None
        self._watcher = None
        self._closed_event = asyncio.Event()
        self.signal = _WaitSignal(self)
        if parent is not None and _signal_aborted(parent):
            self.close(_parent_reason(parent))
            return
        self._schedule()
        if parent is not None:
            self._watcher = asyncio.ensure_future(self._watch_parent())

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def reason(self) -> Any:
        return self._reason

    async def wait_done(self) -> None:
        await self._closed_event.wait()

    async def _watch_parent(self) -> None:
        while not self._closed:
            if _signal_aborted(self._parent):
                self.close(_parent_reason(self._parent))
                return
            await asyncio.sleep(0.02)

    def _schedule(self) -> None:
        self._clear_timer()
        if self._closed or self._claims:
            return
        loop = asyncio.get_running_loop()
        delay = max(0.0, (self.deadline - time.time() * 1000) / 1000.0)
        self._timer = loop.call_later(delay, lambda: self.close(self._timeout))

    def _clear_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    async def attach(self, signal: Any):
        """持有一条业务流；产出一帧剩余时长，流关闭或结算时结束（timed-wait.ts:45）。"""
        if self._closed or _signal_aborted(signal):
            return
        if not self._claims and time.time() * 1000 >= self.deadline:
            self.close(self._timeout)
            return
        released = asyncio.Event()

        def release() -> None:
            if released in self._claims:
                self._claims.discard(released)
                released.set()
                self._schedule()

        self._claims.add(released)
        self._schedule()
        try:
            yield {"remainingMs": max(0, int(self.deadline - time.time() * 1000))}
            rel = asyncio.ensure_future(released.wait())
            clo = asyncio.ensure_future(self._closed_event.wait())
            try:
                await asyncio.wait({rel, clo}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (rel, clo):
                    if not task.done():
                        task.cancel()
        finally:
            release()

    def close(self, reason: Any) -> None:
        """释放计时、父取消与全部客户端持有（timed-wait.ts:75）。"""
        if self._closed:
            return
        self._closed = True
        self._reason = reason
        self._clear_timer()
        if self._watcher is not None:
            self._watcher.cancel()
            self._watcher = None
        self._closed_event.set()


def _parent_reason(parent: Any) -> Any:
    return getattr(parent, "reason", None)


class UserQuestionService(Service):
    """`ctx.userQuestions`：校验 + 作用域化 answerer 瀑布（对齐 UserQuestionService）。"""

    def __init__(self, ctx: Context):
        super().__init__(ctx, "userQuestions")
        #: 前台 timed 等待：Agent → {callId: TimedQuestionWait}（index.ts:81）。
        self._waits: dict[Any, dict[str, TimedQuestionWait]] = {}
        #: 已入队但尚未被 agent 承认的迟到回复：sessionId → {callId: messageId}。
        self._queued_replies: dict[str, dict[str, str]] = {}
        ctx.on("session/event", self._on_session_event, global_=True)

    async def ask(self, request: dict) -> dict:
        """向作用域 answerer 瀑布提问并等答案（上游 UserQuestionService.ask）。

        request：{questions: [...], agent?: Agent, signal?: ...}。校验序与
        错误码逐字对齐上游（index.ts:86-151）；详情见模块 docstring。
        """
        signal = request.get("signal")
        if _signal_aborted(signal):
            raise aborted_question()
        questions = request.get("questions") or []
        if len(questions) == 0:
            raise UserQuestionError(
                "ask_user_question requires at least one question", EMPTY_QUESTIONS)
        agent = request.get("agent")
        if agent is not None:
            self._assert_live_root(agent)
        for question in questions:
            intent = question.get("intent")
            if intent is None:
                continue
            options = question.get("options") or []
            if not any(option.get("label") == intent.get("approve") for option in options):
                raise UserQuestionError(
                    f'question {question.get("id")} declares intent {intent.get("kind")} '
                    f'whose approve label {json.dumps(intent.get("approve"))} '
                    "names none of its options",
                    BAD_INTENT)
            if question.get("detail") is None:
                raise UserQuestionError(
                    f'question {question.get("id")} declares intent {intent.get("kind")} '
                    "without the detail it reviews",
                    BAD_INTENT)

        def no_answerer(_cur: Any = None) -> None:
            raise UserQuestionError(
                "no user-questions answerer accepted the request", NO_PROVIDER)

        try:
            if agent is None:
                return await self.ctx.awaterfall(
                    "user-questions/request", request, base=no_answerer)
            payload = dict(request)
            payload["agent"] = agent
            return await self.ctx.awaterfall(
                "user-questions/request", payload,
                this_arg=scope_target(agent, agent.scope.scope_key),
                base=no_answerer)
        except Exception as error:
            restored = restore_user_question_error(error)
            if isinstance(restored, UserQuestionError):
                raise restored
            if _signal_aborted(signal):
                raise aborted_question(error)
            raise restored

    # ---------- timed 提问（dsh-v0.2.0-rc.2） ----------

    def _assert_live_root(self, agent: Any) -> None:
        """human interaction 只对精确 live runtime root 有效（index.ts:131）。"""
        agents = self.ctx.get("agents")
        if agents is None or agents.get(agent.id) is not agent:
            raise UserQuestionError(
                "human interaction requires the exact live calling agent "
                "when an agent is supplied",
                CALLER_NOT_LIVE)
        if agent not in agents.roots():
            raise UserQuestionError(
                "human interaction is unavailable while the calling agent is "
                "owned by another live agent; include the unresolved question "
                "or decision in the child agent's final result",
                DELEGATED_CALLER)

    def _continued(self, agent: Any) -> list:
        """仍可被回答的 `continued` timed 提问（index.ts:146）。"""
        registry = self.ctx.get("sessionProjections")
        if registry is None:
            return []
        state = registry.state_of(agent.session, USER_QUESTION_PROJECTION_KEY)
        if not state:
            return []
        return [item for item in state["questions"]["active"]
                if item["state"] == "continued"]

    def _on_session_event(self, payload: Any) -> None:
        event = payload.get("event") if isinstance(payload, dict) else None
        session = payload.get("session") if isinstance(payload, dict) else None
        if event is None or session is None:
            return
        if event.get("type") != "user/message":
            return
        data = event.get("data") or {}
        source = data.get("source") or {}
        if source.get("kind") != "user-question-reply":
            return
        self._release_reply(session.session_id, source.get("callId"), data.get("id"))

    def _release_reply(self, session_id: str, call_id: Any, message_id: Any) -> None:
        replies = self._queued_replies.get(session_id)
        if replies is None or replies.get(call_id) != message_id:
            return
        replies.pop(call_id, None)
        if not replies:
            self._queued_replies.pop(session_id, None)

    def answer(self, agent: Any, call_id: str, answer: dict) -> bool:
        """回答一个 `continued` timed 提问（上游 index.ts:165 answer）。

        把回复以 `user-question-reply` 来源的 user 消息 steer 进 agent；该消息
        也是投影中关闭该提问的记录。返回是否仍为 continued；被接受的回复在
        agent 承认其 user 消息前保持排队。
        """
        self._assert_live_root(agent)
        question = next((item for item in self._continued(agent)
                         if item["callId"] == call_id), None)
        if question is None:
            return False
        session_id = agent.session.session_id
        replies = self._queued_replies.get(session_id) or {}
        if call_id in replies:
            raise UserQuestionError(
                "a reply is already queued for this question", REPLY_QUEUED)

        def matches(message: Any) -> bool:
            source = message.get("source") or {}
            return source.get("kind") == "user-question-reply" and source.get("callId") == call_id

        if (any(matches(message) for message in agent.inbox.next_turn)
                or any(matches(message) for message in agent.inbox.next_step)):
            raise UserQuestionError(
                "a reply is already queued for this question", REPLY_QUEUED)
        answers = answer.get("answers") if isinstance(answer, Mapping) else None
        if not isinstance(answers, list):
            raise UserQuestionError(
                f"the answer batch for {call_id} must name each of its "
                f"{len(question['questions'])} questions exactly once", BAD_ANSWER)
        answered = {item.get("id") if isinstance(item, Mapping) else None
                    for item in answers}
        if (len(answered) != len(answers)
                or len(question["questions"]) != len(answers)
                or not all(item["id"] in answered for item in question["questions"])):
            raise UserQuestionError(
                f"the answer batch for {call_id} must name each of its "
                f"{len(question['questions'])} questions exactly once", BAD_ANSWER)
        message = create_message("user", [text_block(json.dumps({
            "kind": "answer_to_pending_question", "tool": ASK_USER_QUESTION_TOOL,
            "callId": call_id, "questions": question["questions"], "answers": answers,
        }, separators=(",", ":"), ensure_ascii=False))], {
            "kind": "user-question-reply", "callId": call_id, "outcome": "answered",
        })
        self._queued_replies.setdefault(session_id, {})[call_id] = message["id"]
        try:
            agent.steer(message)
        except Exception:
            self._release_reply(session_id, call_id, message["id"])
            raise
        return True

    async def attach_wait(self, agent: Any, call_id: str, signal: Any):
        """一个答案 UI 持有一条 live timed 等待（上游 index.ts:216 attachWait）。"""
        self._assert_live_root(agent)
        wait = self._waits.get(agent, {}).get(call_id)
        if wait is None:
            return
        async for frame in wait.attach(signal):
            yield frame

    async def ask_timed(self, request: dict, call_id: str, timeout_ms: int) -> dict:
        """前台等待，首个结算由客户端决定（上游 index.ts:234 askTimed）。

        客户端倒计时结束时拒绝以 `ASK_TIMED_OUT`，本方法映射为 pending 结果；
        未在窗口内到达答案（含无客户端认领）同样返回 `{pending, callId}`。
        """
        if (isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int)
                or timeout_ms < 1 or timeout_ms > 2_147_483_647):
            raise UserQuestionError(
                "timeout must fit a positive platform timer", BAD_TIMEOUT)
        agent = request["agent"]
        self._assert_live_root(agent)
        calls = self._waits.setdefault(agent, {})
        if call_id in calls:
            raise UserQuestionError(
                "the question call already has a foreground wait", DUPLICATE_WAIT)
        wait = TimedQuestionWait(
            time.time() * 1000 + timeout_ms, request.get("signal"),
            UserQuestionError(
                "ask_user_question timed out before the user answered", ASK_TIMED_OUT))
        calls[call_id] = wait
        try:
            try:
                return await self.ask({**request, "signal": wait.signal,
                                       "wait": {"callId": call_id, "timed": True}})
            except UserQuestionError as error:
                if wait.signal.aborted:
                    raise wait.signal.reason
                if error.code == NO_PROVIDER:
                    await wait.wait_done()
                    raise wait.signal.reason
                raise
        except UserQuestionError as error:
            if error.code == ASK_TIMED_OUT:
                return {"pending": True, "callId": call_id}
            if wait.signal.aborted:
                raise aborted_question(error)
            raise
        finally:
            wait.close(aborted_question())
            calls.pop(call_id, None)
            if not calls:
                self._waits.pop(agent, None)


def install_user_questions(ctx: Context) -> UserQuestionService:
    """幂等装配：创建 ctx.userQuestions 服务（镜像 install_agents/install_jobs）。

    已存在 ctx.userQuestions 时收养并直接返回（首个调用生效）。仅装 service
    seam——UI 应答者由 web 组合（web/questions.py）或宿主显式挂载。若
    `ctx.sessionProjections` 已装配，同时注册 `userQuestions` 投影单元。
    """
    service = ctx.get("userQuestions")
    if service is None:
        service = UserQuestionService(ctx)
    registry = ctx.get("sessionProjections")
    if registry is not None and not getattr(service, "_projection_registered", False):
        register_user_question_projection(registry)
        service._projection_registered = True
    return service