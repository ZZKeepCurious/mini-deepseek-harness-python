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
from .user_questions import UserQuestionError

__all__ = ["ASK_USER_QUESTION", "register_ask_user_question"]

#: 工具名（上游 tool-ask-user 唯一注册名）。
ASK_USER_QUESTION = "ask_user_question"

DESCRIPTION = (
    "Ask the user a concise question when you need confirmation, a choice, or "
    "missing information before proceeding. Send one or more questions, each "
    "with a stable id that will be echoed in the answer."
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


def register_ask_user_question(reg: Any, ctx: Any) -> Any:
    """把 ask_user_question 注册进现有 ToolRegistry（装配点显式调用）。

    仅当 ctx.userQuestions 服务已装配时注册（对齐上游 inject 的
    ['tools','userQuestions'] 依赖）；缺失时跳过并返回 None。返回工具注册的
    disposer（fiber 拆解自动注销）。
    """
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
        out_answers = []
        for item in answer.get("answers", []):
            entry: dict = {"id": item["id"], "selected": list(item.get("selected", []))}
            if item.get("custom") is not None:
                entry["custom"] = item["custom"]
            out_answers.append(entry)
        return {"answers": out_answers}

    return reg.register(Tool(
        name=ASK_USER_QUESTION,
        description=DESCRIPTION,
        parameters=_PARAMETERS,
        output=_OUTPUT_SCHEMA,
        render=_render,
        execute=execute,
    ))