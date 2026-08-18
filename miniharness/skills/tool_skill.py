"""Durable 会话 skill 目录 + 模型侧 `skill` 加载工具。

上游对照：packages/skill/tool-skill/src/index.ts（skill 工具 + 手势注入 +
catalog 注入 listener + 目录渲染/digest）。

契约（与上游逐条一致）：
  * `skill` 工具：三态错误 `invalid skill name "<name>"` / `skill "<name>"
    is unknown or no longer available` / `skill "<name>" is not available
    for model invocation`；成功返回 {name, provider, resourceBase?, content}；
    lookup 用调用方 agent（mini 传 agent.ctx 作 scope + agent.cwd）
  * `/name` 手势：只扫 source.kind=='user' 的 text 块；SKILL_GESTURE
    (^|\\s)/kebab(?=\\s|$)；去重保序；未知名/非用户可调用名保持普通散文；
    注入消息 source {kind:'skill-invocation', name, form:'instructions'}
  * catalog 注入：仅当 agent 解析到本插件的精确 `skill` 工具注册（scoped
    shadow 或 restrict 会同时移除 schema 与注入指引）；快照不完整（revision
    抖动中）跳过；只列 modelInvocable；同 digest 幂等（可见 digest 已是最新
    则移除内存中残留的过期目录、消息内同 digest 则 no-op）；未发布过且空目录
    不注入；发布过则渲染 replacement update（含空目录退役语）
  * 目录是 `catalog`-form context：entries 记录在 source（非解析渲染散文），
    渲染层 escapeText 只发生在帧层不落库

mini 简化（有意保留，须在文档标注）：
  * execute 直接返回渲染文本 render_skill_content(skill)（上游 canonical
    value + output.render 分离未复现，与 jobs 同款简化）
  * 工具错误经管线捕获后带 Python 类型名前缀 `ValueError: `（上游为
    `Error: `）；三态错误消息体本身逐字一致
  * lookup.cwd 取 getattr(agent, 'cwd', None)（mini Session 无 header，
    AgentLoop 无 cwd 属性，缺省 None → 不扫描项目根）
  * signal 为 _AbortProxy / threading.Event：仅检查标记不中断执行
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..core.scope import Context, _maybe_await
from ..core.session import create_message, text_block
from ..core.tools import Tool, ToolExec
from .registry import (
    digest_catalog_entries,
    escape_text,
    is_model_invocable,
    is_skill_name,
    is_user_invocable,
    render_skill_content,
)

__all__ = ["SKILL_GESTURE", "SkillTool", "install_skill_tool"]

logger = logging.getLogger("miniharness.skills")

DEFAULT_CATALOG_DESCRIPTION_MAX_LENGTH = 500

#: 空白界的 `/name` 记号（对齐上游 SKILL_GESTURE：第二个 `/` 或非边界字符
#: 即断开匹配，避免 /usr/bin、5/8 误入）。
SKILL_GESTURE = re.compile(r"(^|\s)/([a-z0-9]+(?:-[a-z0-9]+)*)(?=\s|$)")

#: 与上游逐字一致的 skill 工具 description（模型面契约）。
SKILL_TOOL_DESCRIPTION = (
    "Load the full instructions for an available skill. Call this with the exact "
    "skill name from the session skill catalog before acting on a task that names "
    "or clearly matches that skill."
)


def catalog_description(value: str, max_length: int) -> str:
    """归一化空白 + 截断的目录描述（上游 catalogDescription 同款，未转义）。"""
    normalized = re.sub(r"\s+", " ", value).strip()
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 3]}..."


def catalog_source_entries(skills: list[dict], max_length: int) -> list[dict]:
    """目录 source 记录的条目（渲染行之外的 durable 镜像）。"""
    return [
        {"name": skill["name"], "description": catalog_description(skill["description"], max_length)}
        for skill in skills
    ]


def _read_catalog_entries(source: Any) -> list[dict] | None:
    """安全读取一条 catalog 消息记录的条目；不可读视为"不是本插件目录"。

    会话日志里的记录经 deep_freeze 冻结为 mappingproxy/tuple，故用
    Mapping/Sequence 判断（上游为结构化对象，等价）。
    """
    if not isinstance(source, Mapping):
        return None
    entries = source.get("entries")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return None
    readable: list[dict] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            return None
        name = entry.get("name")
        description = entry.get("description")
        if not isinstance(name, str) or name == "" or not isinstance(description, str):
            return None
        readable.append({"name": name, "description": description})
    return readable


def invoked_skill_names(messages: list[dict]) -> list[str]:
    """claimed 用户消息中的 `/name` 手势，去重保序（未对照注册表校验）。"""
    names: list[str] = []
    for message in messages:
        source = message.get("source")
        if not isinstance(source, dict) or source.get("kind") != "user":
            continue
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text") or ""
            for match in SKILL_GESTURE.finditer(text):
                name = match.group(2)
                if name and name not in names:
                    names.append(name)
    return names


def _render_catalog_lines(entries: list[dict]) -> list[str]:
    return [f"- `{entry['name']}`: {escape_text(entry['description'])}" for entry in entries]


def _render_catalog_message(entries: list[dict]) -> dict:
    text = "\n".join([
        "<system-reminder>",
        "A skill is a reusable set of task-specific instructions. The following skills are available in this session:",
        "",
        "<available_skills>",
        *_render_catalog_lines(entries),
        "</available_skills>",
        "",
        "If the user names a skill, or the task clearly matches a skill's description, call the `skill` tool with the exact skill name before taking task actions. Load all applicable skills, then follow their full instructions. This catalog contains summaries only; do not infer or follow a skill's instructions until it has been loaded.",
        "A user may also invoke a skill directly; its <skill_content> block then appears in this conversation. Follow it, and do not call the `skill` tool again for that skill.",
        "</system-reminder>",
    ])
    return create_message("user", [text_block(text)], {
        "kind": "skill-catalog", "form": "catalog", "entries": entries,
    })


def _render_catalog_update(entries: list[dict]) -> dict:
    availability = [
        "No skills are currently available through the `skill` tool. Do not use names from earlier skill catalogs.",
        "A user may still invoke a skill directly; its <skill_content> block then appears in this conversation. Follow it, and do not call the `skill` tool for it.",
    ] if len(entries) == 0 else [
        "Use only names in this replacement catalog. If the user names a listed skill, or the task clearly matches its description, call the `skill` tool with the exact name before acting.",
        "A user may also invoke a skill directly; its <skill_content> block then appears in this conversation. Follow it, and do not call the `skill` tool again for that skill.",
    ]
    text = "\n".join([
        "<system-reminder>",
        "The available skill catalog changed. This complete catalog replaces every earlier available-skills list in this session:",
        "",
        "<available_skills>",
        *_render_catalog_lines(entries),
        "</available_skills>",
        "",
        *availability,
        "</system-reminder>",
    ])
    return create_message("user", [text_block(text)], {
        "kind": "skill-catalog", "form": "catalog", "update": True, "entries": entries,
    })


def _lookup_for(agent: Any) -> dict:
    return {
        "cwd": getattr(agent, "cwd", None),
        "scope": getattr(agent, "ctx", None),
    }


def _signalled(signal: Any) -> bool:
    if signal is None:
        return False
    aborted = getattr(signal, "aborted", None)
    if isinstance(aborted, bool):
        return aborted
    is_set = getattr(signal, "is_set", None)
    return bool(is_set() if callable(is_set) else False)


class SkillTool:
    """skill 工具 + 两个 pre-step listener（手势先、catalog 后）。"""

    def __init__(self, ctx: Context, registry: Any, config: dict | None = None):
        self._ctx = ctx
        self._registry = registry
        config = config or {}
        max_len = config.get("catalogDescriptionMaxLength", DEFAULT_CATALOG_DESCRIPTION_MAX_LENGTH)
        if not isinstance(max_len, int) or isinstance(max_len, bool) or max_len < 3:
            raise ValueError(
                f"tool-skill: catalogDescriptionMaxLength must be an integer greater than or equal to 3"
            )
        self.catalog_description_max_length = max_len
        self.skill_tool = Tool(
            name="skill",
            description=SKILL_TOOL_DESCRIPTION,
            execute=self._execute,
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The exact skill name from the available skills list.",
                    },
                },
                "required": ["name"],
            },
            output={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "provider": {"type": "string"},
                    "resourceBase": {"oneOf": [
                        {"type": "object", "properties": {"kind": {"const": "directory"}, "path": {"type": "string"}}, "required": ["kind", "path"]},
                        {"type": "object", "properties": {"kind": {"const": "url"}, "url": {"type": "string"}}, "required": ["kind", "url"]},
                        {"type": "object", "properties": {"kind": {"const": "opaque"}, "description": {"type": "string"}}, "required": ["kind", "description"]},
                    ]},
                    "content": {"type": "string"},
                },
                "required": ["name", "provider", "content"],
            },
            present_call=self._present_call,
        )
        # 手势先、catalog 后：waterfall 顺序保证 catalog 消息先注入，
        # 手势内容作为"离答案最近"的最后注入（对齐上游注册序注释）。
        ctx.on("agent/pre-step", self._on_gesture)
        ctx.on("agent/pre-step", self._on_catalog)

    # ---------- skill 工具 ----------

    async def _execute(self, args: dict, exec_: ToolExec) -> Any:
        name = args.get("name")
        if not is_skill_name(name):
            raise ValueError(f'invalid skill name "{name}"')
        lookup = _lookup_for(exec_.agent)
        summary = next((s for s in self._registry.list(lookup) if s["name"] == name), None)
        if summary is None or not is_model_invocable(summary):
            if summary is None:
                raise ValueError(f'skill "{name}" is unknown or no longer available')
            raise ValueError(f'skill "{name}" is not available for model invocation')
        skill = self._registry.get(name, lookup)
        if skill is None:
            raise ValueError(f'skill "{name}" is unknown or no longer available')
        if not is_model_invocable(skill):
            raise ValueError(f'skill "{name}" is not available for model invocation')
        return render_skill_content(skill)

    @staticmethod
    def _present_call(args: dict) -> dict:
        return {"card": "generic", "title": f"Load skill {args['name']}", "kind": "read", "rawInput": args["name"]}

    # ---------- 手势 listener ----------

    async def _on_gesture(self, payload: dict, next_fn) -> dict:
        decision = await _maybe_await(next_fn())
        if isinstance(decision, dict) and decision.get("kind") == "reject":
            return decision
        agent = payload.get("agent")
        if agent is None:
            return decision
        messages = list(decision.get("messages") or []) if isinstance(decision, dict) else []
        names = invoked_skill_names(messages)
        if not names or _signalled(payload.get("signal")):
            return decision
        lookup = _lookup_for(agent)
        injections = []
        for name in names:
            skill = self._registry.get(name, lookup)
            # 未知名与用户禁用名保持普通散文：手势在这个边界从未成立
            if skill is None or not is_user_invocable(skill):
                continue
            source = {"kind": "skill-invocation", "name": name, "form": "instructions"}
            injections.append(create_message(
                "user", [text_block(render_skill_content(skill))], source,
            ))
        if not injections:
            return decision
        return {**decision, "kind": "enter", "messages": [*messages, *injections]}

    # ---------- catalog listener ----------

    async def _on_catalog(self, payload: dict, next_fn) -> dict:
        decision = await _maybe_await(next_fn())
        if isinstance(decision, dict) and decision.get("kind") == "reject":
            return decision
        agent = payload.get("agent")
        if agent is None or _signalled(payload.get("signal")):
            return decision
        tool_visible = False
        tools = getattr(agent, "tools", None)
        if tools is not None:
            tool_visible = tools.resolve(self.skill_tool.name) is self.skill_tool
        snapshot = self._registry.snapshot(_lookup_for(agent)) if tool_visible \
            else {"skills": [], "complete": True}
        if not snapshot["complete"]:
            return decision
        skills = [s for s in snapshot["skills"] if is_model_invocable(s)]
        entries = catalog_source_entries(skills, self.catalog_description_max_length)
        digest = digest_catalog_entries(entries)
        history = self._catalog_history(agent)
        existing = self._catalog_message(decision)
        messages = list(decision.get("messages") or []) if isinstance(decision, dict) else []
        if history.get("visibleDigest") == digest:
            if existing is None:
                return decision
            return {**decision, "messages": [
                m for m in messages if m["id"] != existing["message"]["id"]
            ]}
        if existing is not None and digest_catalog_entries(existing["entries"]) == digest:
            return decision
        if not history["published"] and len(skills) == 0:
            if existing is None:
                return decision
            return {**decision, "messages": [
                m for m in messages if m["id"] != existing["message"]["id"]
            ]}
        catalog = _render_catalog_update(entries) if history["published"] \
            else _render_catalog_message(entries)
        if existing is None:
            return {**decision, "kind": "enter", "messages": [*messages, catalog]}
        return {**decision, "kind": "enter", "messages": [
            catalog if m["id"] == existing["message"]["id"] else m for m in messages
        ]}

    # ---------- 内部 ----------

    def _catalog_history(self, agent: Any) -> dict:
        """会话日志中最近的可见目录 digest + 是否发布过（对齐 catalogHistory）。"""
        visible = {node["seq"] for node in agent.session.surface_nodes()}
        published = False
        for event in reversed(agent.session.events):
            if event["type"] != "user/message":
                continue
            data = event["data"]
            source = data.get("source") if isinstance(data, Mapping) else None
            if not isinstance(source, Mapping) or source.get("kind") != "skill-catalog":
                continue
            entries = _read_catalog_entries(source)
            if entries is None:
                continue
            published = True
            if event["seq"] in visible:
                return {"visibleDigest": digest_catalog_entries(entries), "published": True}
        return {"published": published}

    def _catalog_message(self, decision: dict) -> dict | None:
        """当前 step 消息列表中残留的目录消息（对齐 catalogMessage）。"""
        messages = decision.get("messages") or []
        for message in messages:
            source = message.get("source")
            if not isinstance(source, dict) or source.get("kind") != "skill-catalog":
                continue
            entries = _read_catalog_entries(source)
            if entries is not None:
                return {"message": message, "entries": entries}
        return None


def install_skill_tool(ctx: Context, registry: Any, config: dict | None = None) -> SkillTool:
    """装配 skill 工具 + 手势/catalog 两个 pre-step listener。"""
    return SkillTool(ctx, registry, config)
