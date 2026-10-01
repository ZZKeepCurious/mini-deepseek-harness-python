"""第 9 章：模型面工具 ask_user_question —— ctx.userQuestions 能力的 Consumer。

对应 dsh 真实源码：packages/interaction/tool-ask-user（src/index.ts）。

上游语义（已核实）：
  * 工具经插件 apply 挂到 tools 注册表（inject ['tools','userQuestions']）；
    mini 以显式独立注册 `register_ask_user_question(reg, ctx)`（仅当
    ctx.userQuestions 服务存在），只在 web 组合与 demo 调用——headless /
    sessions resume 的 default_tools **不挂**此工具（对齐上游 headless
    rides over dsh-base：无工具，只经 base 有 service seam）。
  * 参数投影：multi_select → multiSelect；execute 拼接 {questions, agent?,
    signal} 请求，answer 的 selected 拷贝一份，custom 只读存在时携带。
  * 结构错误经 error_info 携带 {name, code}（先例 tool_timeout_result，
    tools.py:29-42）；文本 `Error: <message>` 与上游 toolErrorResult 一致。
  * render 为 output.render 双参（args, value）：canonical 值 → 紧凑
    JSON 序列化（与 JSON.stringify 对齐：紧凑分隔符 + UTF-8 原样）。mini
    载体差异：emit_tool_result 对非 str content 做 str()（列表渲染会以
    repr 包一层），故 render 直接返回 JSON 字符串，模型可见输出即纯 JSON。

载体差异（已核实，见 verified-diffs）：
  * schema 载体：上游在 properties 内用 inline `required: true`（非标准 JSON
    Schema）；mini validate_schema 用 `required` 数组（items 对象级
    `["id","question"]` / options items 级 `["label"]`），语义等价。
"""
from __future__ import annotations

import json
from typing import Any

from ..core.tools import Tool, ToolResult
from .user_questions import (
    TIMED_WAIT_PARAMETER,
    UserQuestionError,
)

__all__ = [
    "ASK_USER_QUESTION",
    "PENDING_NOTICE",
    "register_ask_user_question",
    "register_timed_ask_user",
]

#: 工具名（上游 tool-ask-user 唯一注册名）。
ASK_USER_QUESTION = "ask_user_question"

DESCRIPTION = (
    "Ask the user a concise question when you need confirmation, a choice, or "
    "missing information before proceeding. Send one or more questions, each "
    "with a stable id that will be echoed in the answer."
)

#: timed 工具的描述（上游 timed.ts:27-30 逐字）。
TIMED_DESCRIPTION = (
    "Ask brief, direct, self-contained questions about missing information, "
    "preferences, or decisions. Use user-facing terms; assume no knowledge of "
    "background work or internal names. Use distinct stable question IDs. A "
    "submitted skipped question is an answer item with empty selected and no "
    "custom; pending instead means no answer batch arrived before the timeout "
    "and the user can still answer."
)

#: pending 结果 `message` 字段的指引（上游 timed.ts:39-42 逐字；投影据其判读
#: 调用仍可回答，客户端据其判读该结果文本是超时而非答案批）。
PENDING_NOTICE = (
    "No answer batch arrived before the timeout. This is pending, not a skipped "
    "answer. Continue useful independent work. The user can still answer; their "
    "reply will be a user message identified as answer_to_pending_question with "
    "this callId and the original questions. Do not treat this as permission."
)

#: 问题对象 schema（上游 tool-ask-user parameters.questions.items，逐字对齐：
#: `required: true` 在 mini 以 properties 内嵌套 required 数组表达）。
_ITEM_PROPERTIES = {
    "id": {"type": "string",
           "description": "Stable id for this question; echoed in the answer."},
    "question": {"type": "string",
                 "description": "The specific question to ask the user."},
    "header": {"type": "string",
               "description": 'Optional short heading for the question, such as "Confirm" or "Choose Mode".'},
    "options": {"type": "array",
                "description": 'Optional choices to show the user. If you recommend one, put it first and append "(Recommended)" to that label.',
                "items": {"type": "object", "additionalProperties": True,
                          "properties": {
                              "label": {"type": "string",
                                        "description": "Short user-facing option label."},
                              "description": {"type": "string",
                                              "description": "One sentence explaining the tradeoff or impact."},
                          },
                          "required": ["label"]}},
    "multi_select": {"type": "boolean",
                     "description": "Whether the user may select more than one option. Defaults to false."},
}

_PARAMETERS = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "description": "Questions to ask the user before continuing.",
            "items": {"type": "object", "additionalProperties": True,
                      "properties": _ITEM_PROPERTIES,
                      "required": ["id", "question"]},
        },
    },
    "required": ["questions"],
}

_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "answers": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {
                          "id": {"type": "string"},
                          "selected": {"type": "array",
                                       "items": {"type": "string"}},
                          "custom": {"type": "string"},
                      }},
        },
    },
}


#: timed schema 的 questions 段（与 legacy 同形，描述不同）。
_TIMED_QUESTIONS = {
    "type": "array",
    "description": "Questions to ask the user.",
    "items": {"type": "object", "additionalProperties": True,
              "properties": _ITEM_PROPERTIES,
              "required": ["id", "question"]},
}

def _timed_parameters(default_timeout: int) -> dict:
    """timed 工具参数 schema（上游 timed.ts:109-149；timeout 描述内插缺省值）。"""
    return {
        "type": "object",
        "properties": {
            "questions": _TIMED_QUESTIONS,
            # `userQuestions` 投影据本参数是否出现在已记录 request/header 中，
            # 判读一次 ask_user_question 调用是 timed 还是 legacy（timed.ts:143-148）。
            TIMED_WAIT_PARAMETER: {
                "type": "integer",
                "description": "Wait seconds for the entire batch "
                               f"(default {default_timeout}); omit unless the user "
                               "specifies a duration. Use -1 only when an answer is "
                               "required before proceeding.",
            },
        },
        "required": ["questions"],
    }

_TIMED_OUTPUT_SCHEMA = {
    "oneOf": [
        {
            "type": "object", "additionalProperties": False,
            "properties": {
                "pending": {"type": "boolean", "enum": [True]},
                "callId": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["pending", "callId", "message"],
        },
        {
            "type": "object", "additionalProperties": False,
            "properties": {
                "answers": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": False,
                              "properties": {
                                  "id": {"type": "string"},
                                  "selected": {"type": "array",
                                               "items": {"type": "string"}},
                                  "custom": {"type": "string"},
                              },
                              "required": ["id", "selected"]},
                },
            },
            "required": ["answers"],
        },
    ],
}


def _validate_timeout(timeout: Any, default: int) -> int:
    """timed 工具的 timeout 校验（上游 timed.ts:12-17）。

    -1 或正整数且 ≤ 2_147_483 秒；缺省取部署配置。
    """
    value = default if timeout is None else timeout
    if value != -1 and (isinstance(value, bool) or not isinstance(value, int)
                        or value < 1 or value > 2_147_483):
        raise ValueError("timeout must be -1 or a positive integer up to 2147483 seconds")
    return value


def _validate_question_ids(questions: list) -> None:
    """提问 id 必须在本次调用内唯一（上游 timed.ts:19-25）。"""
    seen = set()
    for question in questions:
        qid = question.get("id")
        if qid in seen:
            raise ValueError(
                f"question id {json.dumps(qid)} must be unique within this call")
        seen.add(qid)


def _answer_result(answer: dict) -> dict:
    """把服务答案拷成模型可见结果（上游 timed.ts:88-96 answerResult）。"""
    out = []
    for item in answer.get("answers", []):
        entry: dict[str, Any] = {"id": item["id"], "selected": list(item.get("selected", []))}
        if item.get("custom") is not None:
            entry["custom"] = item["custom"]
        out.append(entry)
    return {"answers": out}


def _project_questions(args: dict) -> list:
    """参数投影（上游 execute 的 questions.map）：仅存在字段携带。"""
    projected = []
    for question in args.get("questions", []):
        q: dict[str, Any] = {"id": question["id"], "question": question["question"]}
        if question.get("header") is not None:
            q["header"] = question["header"]
        if question.get("options") is not None:
            q["options"] = question["options"]
        if question.get("multi_select") is not None:
            q["multiSelect"] = question["multi_select"]
        projected.append(q)
    return projected


def _render(_args: dict, value: Any) -> str:
    """output.render（上游 (args, value) => 单 text 块 JSON 串）。

    与 JSON.stringify 对齐：紧凑分隔符 + 不转义非 ASCII。mini 的
    emit_tool_result 对非 str content 做 str() 包裹，返回 str 才让
    模型看到纯 JSON（上游该 render 返回 ContentBlock[]，语义等价）。"""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def register_ask_user_question(reg: Any, ctx: Any, *, mode: str = "legacy",
                               timeout: int = 120) -> Any:
    """把 ask_user_question 注册进现有 ToolRegistry（装配点显式调用）。

    仅当 ctx.userQuestions 服务已装配时注册（对齐上游 inject 的
    ['tools','userQuestions'] 依赖）；缺失时跳过并返回 None。返回工具注册的
    disposer（fiber 拆解自动注销）。

    dsh-v0.2.0-rc.2 新增 Config：`mode='legacy'|'timed'`（缺省 legacy），
    `timeout`（timed 缺省前台等待秒数，缺省 120，-1 = 必须回答）。
    """
    if mode == "timed":
        return register_timed_ask_user(reg, ctx, timeout)
    if mode != "legacy":
        raise ValueError("tool-ask-user: mode must be 'legacy' or 'timed'")
    service = ctx.get("userQuestions")
    if service is None:
        return None

    async def execute(args: dict, exec_: Any) -> Any:
        request: dict = {"questions": _project_questions(args),
                         "signal": exec_.signal}
        if getattr(exec_, "agent", None) is not None:
            request["agent"] = exec_.agent
        try:
            answer = await service.ask(request)
        except UserQuestionError as error:
            return ToolResult(
                ok=False, is_error=True, error=f"Error: {error}",
                error_info={"name": "UserQuestionError", "code": error.code})
        return _answer_result(answer)

    return reg.register(Tool(
        name=ASK_USER_QUESTION,
        description=DESCRIPTION,
        parameters=_PARAMETERS,
        output=_OUTPUT_SCHEMA,
        render=_render,
        execute=execute,
    ))


def register_timed_ask_user(reg: Any, ctx: Any, timeout: int = 120) -> Any:
    """注册 opt-in 的 timed `ask_user_question`（上游 timed.ts:103 registerTimedAskUser）。

    工具 schema 多一个 `timeout` 参数——`userQuestions` 投影据其出现在已记录
    request/header 中判读调用为 timed；输出 oneOf pending|answers。仅当
    ctx.userQuestions 已装配时注册；缺失返回 None。
    """
    default_timeout = _validate_timeout(timeout, 120)
    service = ctx.get("userQuestions")
    if service is None:
        return None

    async def execute(args: dict, exec_: Any) -> Any:
        resolved = _validate_timeout(args.get(TIMED_WAIT_PARAMETER), default_timeout)
        _validate_question_ids(args.get("questions", []))
        request: dict = {"questions": _project_questions(args),
                         "signal": exec_.signal}
        if getattr(exec_, "agent", None) is not None:
            request["agent"] = exec_.agent
        try:
            if resolved != -1:
                if getattr(exec_, "agent", None) is None:
                    raise RuntimeError("timed questions require a live agent")
                result = await service.ask_timed(
                    {**request, "agent": exec_.agent}, exec_.call_id, resolved * 1000)
                if "pending" in result:
                    return {**result, "message": PENDING_NOTICE}
                return _answer_result(result)
            # `wait` 无 `timed`：不定长形态仍以 callId 键控客户端卡片。
            answer = await service.ask(
                {**request, "wait": {"callId": exec_.call_id}})
            return _answer_result(answer)
        except UserQuestionError as error:
            return ToolResult(
                ok=False, is_error=True, error=f"Error: {error}",
                error_info={"name": "UserQuestionError", "code": error.code})

    return reg.register(Tool(
        name=ASK_USER_QUESTION,
        description=TIMED_DESCRIPTION,
        parameters=_timed_parameters(default_timeout),
        output=_TIMED_OUTPUT_SCHEMA,
        render=_render,
        execute=execute,
    ))