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

import json
from typing import Any, Callable

from ..core.dsh_scope import scope_target
from ..core.scope import Context, Service

__all__ = [
    "ASK_ABORTED",
    "ASK_CANCELLED",
    "BAD_INTENT",
    "CALLER_NOT_LIVE",
    "DELEGATED_CALLER",
    "EMPTY_QUESTIONS",
    "NO_PROVIDER",
    "UserQuestionError",
    "UserQuestionService",
    "aborted_question",
    "install_user_questions",
    "restore_user_question_error",
]

#: 稳定错误分类（上游 UserQuestionError 的 code 集合，index.ts:33-47；
#: ASK_CANCELLED 由应答者侧产出，经传输恢复转送回服务面）。
ASK_ABORTED = "ASK_ABORTED"
ASK_CANCELLED = "ASK_CANCELLED"
BAD_INTENT = "BAD_INTENT"
CALLER_NOT_LIVE = "CALLER_NOT_LIVE"
DELEGATED_CALLER = "DELEGATED_CALLER"
EMPTY_QUESTIONS = "EMPTY_QUESTIONS"
NO_PROVIDER = "NO_PROVIDER"


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


class UserQuestionService(Service):
    """`ctx.userQuestions`：校验 + 作用域化 answerer 瀑布（对齐 UserQuestionService）。"""

    def __init__(self, ctx: Context):
        super().__init__(ctx, "userQuestions")

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


def install_user_questions(ctx: Context) -> UserQuestionService:
    """幂等装配：创建 ctx.userQuestions 服务（镜像 install_agents/install_jobs）。

    已存在 ctx.userQuestions 时收养并直接返回（首个调用生效）。仅装 service
    seam——UI 应答者由 web 组合（web/questions.py）或宿主显式挂载。
    """
    service = ctx.get("userQuestions")
    if service is None:
        service = UserQuestionService(ctx)
    return service