"""v3→v4 相邻迁移（上游 session-format-v3-to-v4/src/{migration,tool-role,sources,
content,extension-identities,references}.ts 逐语义移植；整件函数载体，与 v0→v1/
v1→v2/v2→v3 同构——流式 Stage 架构是上游性能机制，mini 整件迁移已登记
verified-diffs §2.24 载体差异）。

五步：源 v3 精确校验（target 全量）+ 封闭词表（RELEASED_V3 dispositions；
未知含 ignorable 拒）→ 修复证据（开放 turn 无开步且下一条编号 turn/start
紧跟非空 next-turn agent/inbox/spliced 时，在该 start 前插 reason
`interrupted` 的 turn/end）→ 密集重映射（六组同制品引用 + surface 信封）→
tool/result 提升（role user + tool-result 包裹 → role tool 平铺 content +
顶层 toolCallId/isError；扩展字段前缀 `plugin:message:`/`plugin:result:`）→
消息 source 改名（plugin 包裹 → 命名 kind）→ 内容标签前缀（未知块标签
`plugin:<type>`，含内嵌流 block-start/block-end）→ 目标 v4 全量校验。

delivery guards：`session-log-deepseek/delivery-accepted` 声称 v4 → 拒（防升版
把 v3 水位提升为 v4 上传水位）；foreign marker 仅允许在有 parentSession 的
继承前缀内。

载体差异（有意保留，须登记）：mini 不持久化 `subagent/catalog` 事件，故上游
「补写缺失 parent catalog facts」整段不实现（上游 facts.ts / migration.finish
的 candidates 分支）；`deliverables/presented` 亦不在 mini 词表。父目录事实
既不收集也不补写。
"""
from __future__ import annotations

from typing import Any

from . import dispositions as _disp
from .helpers import (
    count,
    fail,
    is_json_object,
    snapshot_json,
    unsupported,
)
from .validate_v3 import assert_released_v3_artifact, assert_released_v3_header
from .validate_v4 import assert_released_v4_artifact

__all__ = ["V3_TO_V4"]

#: released v3 插件名 → 当前生产者 kind（上游 sources.ts RENAMED_PRODUCERS）。
_RENAMED_PRODUCERS = {
    "compact": "compact-checkpoint",
    "tools-code-mode": "ptc-mode",
    "tools-ptc": "ptc-mode",
    "dsh-compaction-basic": "compact-basic",
    "@deepseek-ai/dsh-system-prompt": "runtime-context",
}

#: 保留同名 kind 的第一方生产者（上游 sources.ts RELEASED_SAME_NAME_PRODUCERS）。
_SAME_NAME_PRODUCERS = frozenset({
    "agent-instructions", "session-reference", "team-message", "goal",
    "skill-invocation", "skill-catalog", "coordinator", "subagent-report",
    "subagent-settled", "webhook", "agent-message", "model-selection",
    "plan-mode", "time-context", "tmux-context", "user-approval",
    "repeat-tool-reminder", "tool-cordis", "cordis-host-runner", "tool-goal",
    "tool-jobs", "hooks-codex", "hooks-claude-code", "schedule",
    "dsh-session-title-llm",
})

#: released v3 已知内容块标签（上游 content.ts V3_BLOCK_TYPES）。
_V3_BLOCK_TYPES = frozenset({"text", "reasoning", "image", "file", "tool-call", "tool-result"})


def _migrate_header_v3_v4(header: dict) -> dict:
    assert_released_v3_header(header)
    return {**header, "version": 4}


# ---------- tool/result 提升 ----------

_WRAPPER_FIELDS = frozenset({"type", "toolCallId", "content", "isError"})
_MESSAGE_FIELDS = frozenset({"id", "role", "source", "content"})


def _extension_fields(value: dict, fields: frozenset, owner: str) -> dict:
    """保留未知字段，且不合并原始 message/result 归属（上游 extensionFields）。"""
    return {f"plugin:{owner}:{key}": item for key, item in value.items()
            if key not in fields}


def _result_content(value: Any, subject: str) -> list:
    if not isinstance(value, list):
        raise fail(f"{subject} tool-result content must be an array")
    if any(isinstance(block, dict) and block.get("type") == "tool-result" for block in value):
        raise unsupported(
            f"{subject} contains a nested tool-result unsupported by this converter")
    return value


def _lift_tool_result(event: dict) -> dict:
    """把一个 released v3 包裹形 tool/result 提升为一等 v4 tool 角色消息
    （上游 liftToolResult）。非包裹形原样返回。"""
    if event.get("type") != "tool/result" or not is_json_object(event.get("data")):
        return event
    data = event["data"]
    message = data.get("message")
    if not is_json_object(message) or message.get("role") != "user":
        return event
    source = message.get("source")
    call_id = source.get("callId") if is_json_object(source) else None
    content = message.get("content")
    block = content[0] if isinstance(content, list) and len(content) == 1 else None
    wrapper = block if is_json_object(block) else None
    id_ = message.get("id")
    if not isinstance(id_, str) or len(id_) == 0 \
            or not is_json_object(source) or source.get("kind") != "tool" \
            or not isinstance(call_id, str) or len(call_id) == 0 \
            or wrapper is None or wrapper.get("type") != "tool-result" \
            or wrapper.get("toolCallId") != call_id:
        raise fail(
            f"format v3 {event.get('type')} at seq {event.get('seq')} requires exactly "
            "one tool-result wrapper matching its tool source")
    is_error = wrapper.get("isError")
    if is_error is not None and not isinstance(is_error, bool):
        raise fail(
            f"format v3 {event.get('type')} at seq {event.get('seq')} "
            "tool-result isError must be boolean")
    target_message: dict[str, Any] = {
        "role": "tool",
        "source": source,
        "toolCallId": call_id,
        "content": _result_content(wrapper.get("content"),
                                   f"format v3 {event.get('type')} at seq {event.get('seq')}"),
    }
    if is_error is not None:
        target_message["isError"] = is_error
    target_message["id"] = id_
    target_message.update(_extension_fields(message, _MESSAGE_FIELDS, "message"))
    target_message.update(_extension_fields(wrapper, _WRAPPER_FIELDS, "result"))
    return {**event, "data": {**data, "message": target_message}}


# ---------- 消息 source 改名 ----------

def _producer_kind(plugin: str, role: Any) -> str:
    if plugin == "@deepseek-ai/dsh-system-prompt" and role == "system":
        return "system-prompt"
    if plugin in _RENAMED_PRODUCERS:
        return _RENAMED_PRODUCERS[plugin]
    if plugin in _SAME_NAME_PRODUCERS:
        return plugin
    return f"plugin:{plugin}"


def _rewrite_plugin_source(source: dict, seq: int, role: Any) -> dict:
    plugin = source.get("plugin")
    if not isinstance(plugin, str):
        raise fail(f"plugin source at seq {seq} is not canonical: plugin requires a string")
    kind = _producer_kind(plugin, role)
    out: dict[str, Any] = {}
    for key, item in source.items():
        if key == "plugin":
            continue
        out[key] = kind if key == "kind" else item
    return out


def _rewrite_message_source(source: dict, seq: int, role: Any) -> dict:
    kind = source.get("kind")
    if not isinstance(kind, str) or len(kind) == 0:
        raise fail(f"message source at seq {seq} requires a nonempty kind")
    if kind == "plugin":
        return _rewrite_plugin_source(source, seq, role)
    return source


def _map_event_messages(event: dict, transform) -> dict:
    """只访问第一方事件载荷声明的消息位置（上游 mapEventMessages）。"""
    data = event.get("data")
    if not is_json_object(data):
        return event
    if event.get("type") == "user/message":
        message = transform(data)
        return event if message is data else {**event, "data": message}
    if event.get("type") in ("developer/message", "system/message",
                             "assistant/message", "tool/result"):
        if not is_json_object(data.get("message")):
            raise fail(f"{event.get('type')} requires a message")
        message = transform(data["message"])
        return event if message is data["message"] \
            else {**event, "data": {**data, "message": message}}
    key = "inserted" if event.get("type") == "agent/inbox/spliced" \
        else "messages" if event.get("type") == "session/title-llm-request" else None
    if key is None:
        return event
    messages = data.get(key)
    if not isinstance(messages, list):
        raise fail(f"{event.get('type')} requires message array")
    mapped = [transform(message) if is_json_object(message)
              else _raise_message_object(event) for message in messages]
    if all(new is old for new, old in zip(mapped, messages)):
        return event
    return {**event, "data": {**data, key: mapped}}


def _raise_message_object(event: dict) -> dict:
    raise fail(f"{event.get('type')} requires message objects")


def _rewrite_event_sources(event: dict) -> dict:
    seq = event.get("seq")

    def transform(message: dict) -> dict:
        source = message.get("source")
        if not is_json_object(source):
            return message
        converted = _rewrite_message_source(source, seq, message.get("role"))
        return message if converted is source else {**message, "source": converted}

    return _map_event_messages(event, transform)


# ---------- 内容标签迁移 ----------

def _migrate_block(value: Any, subject: str) -> Any:
    if not is_json_object(value) or not isinstance(value.get("type"), str):
        raise fail(f"{subject} requires content blocks with string type tags")
    block_type = value["type"]
    if block_type in _V3_BLOCK_TYPES:
        return value
    return {**value, "type": f"plugin:{block_type}"}


def _migrate_content(value: Any, subject: str) -> list:
    if not isinstance(value, list):
        raise fail(f"{subject} content must be an array")
    mapped = [_migrate_block(block, f"{subject}[{index}]")
              for index, block in enumerate(value)]
    if all(new is old for new, old in zip(mapped, value)):
        return value
    return mapped


def _migrate_message(message: dict, subject: str) -> dict:
    content = _migrate_content(message.get("content"), subject)
    return message if content is message.get("content") else {**message, "content": content}


def _migrate_chunk(value: Any, subject: str) -> Any:
    if not is_json_object(value):
        return value
    if value.get("type") == "block-end":
        block = _migrate_block(value.get("block"), f"{subject}.block")
        return value if block is value.get("block") else {**value, "block": block}
    if value.get("type") != "block-start":
        return value
    original = value.get("blockType")
    if not isinstance(original, str):
        raise fail(f"{subject} blockType must be a string")
    block_type = original if original in _V3_BLOCK_TYPES else f"plugin:{original}"
    return value if block_type == original else {**value, "blockType": block_type}


def _migrate_event_content(event: dict) -> dict:
    """转换声明的内容标签与内嵌流标签（上游 migrateV3EventContent）。"""
    subject = f"format v3 {event.get('type')} at seq {event.get('seq')}"
    mapped = _map_event_messages(event, lambda message: _migrate_message(message, subject))
    if not is_json_object(mapped.get("data")):
        return mapped
    data = mapped["data"]

    def content_field(key: str) -> None:
        nonlocal data
        content = _migrate_content(data.get(key), f"{subject}.{key}")
        if content is not data.get(key):
            data = {**data, key: content}

    etype = event.get("type")
    if etype == "compaction/summary":
        content_field("summary")
        if data.get("rawOutput") is not None:
            content_field("rawOutput")
    elif etype == "tool/ptc-dispatch":
        content_field("content")
    elif etype == "team/message/queued" and is_json_object(data.get("message")):
        message = _migrate_message(data["message"], subject)
        if message is not data["message"]:
            data = {**data, "message": message}
    if etype == "request/header" and is_json_object(data.get("header")):
        tools = data["header"].get("tools")
        if isinstance(tools, list):
            for index, tool in enumerate(tools):
                if is_json_object(tool) and "deferLoading" in tool:
                    raise unsupported(
                        f"{subject}.header.tools[{index}] contains deferLoading, "
                        "which is only defined in V4")
    if etype in ("assistant/message", "assistant/attempt") and isinstance(data.get("stream"), list):
        stream = data["stream"]
        converted = []
        for index, entry in enumerate(stream):
            if not is_json_object(entry) or entry.get("type") != "chunk":
                converted.append(entry)
                continue
            chunk = _migrate_chunk(entry.get("chunk"), f"{subject}.stream[{index}]")
            converted.append(entry if chunk is entry.get("chunk") else {**entry, "chunk": chunk})
        if any(new is not old for new, old in zip(converted, stream)):
            data = {**data, "stream": converted}
    if data is not mapped["data"]:
        mapped = {**mapped, "data": data}
    return mapped


# ---------- 引用重映射 ----------

def _remap_v3_references(event: dict, seq: int, mapping: list[int]) -> dict:
    """显式局部坐标重映射（上游 references.ts remapV3References）：只重映射审计
    过的同制品引用——surface 信封 + command/done.sourceEventSeq + compaction
    shadowed* + title messageSeqs + image/offload target seq；captured/delivery
    水位、turn/step、流块索引、数字 JSON 一律不重映射。"""
    if seq == event.get("seq"):
        return event

    def reference(value: Any) -> int:
        source = count(value, "V3 source event reference")
        target = mapping[source] if source < len(mapping) else None
        if source >= event.get("seq") or target is None:
            raise fail("V3 reference must name an earlier source event")
        return target

    def references(value: Any) -> list[int]:
        if not isinstance(value, list):
            raise fail("V3 event references must be an array")
        return [reference(member) for member in value]

    data = event.get("data")
    etype = event.get("type")
    if etype == "command/done" and is_json_object(data) and data.get("sourceEventSeq") is not None:
        data = {**data, "sourceEventSeq": reference(data["sourceEventSeq"])}
    elif etype in ("compaction/summary", "compaction/prune") and is_json_object(data):
        range_ = data.get("shadowedRange")
        if not is_json_object(range_):
            raise fail("V3 event reference container must be an object")
        data = {**data,
                "shadowedRange": {**range_,
                                  "start": reference(range_.get("start")),
                                  "end": reference(range_.get("end"))},
                "shadowedSeqs": references(data.get("shadowedSeqs"))}
    elif etype in ("session/title", "session/title-llm-request") and is_json_object(data):
        data = {**data, "messageSeqs": references(data.get("messageSeqs"))}
    elif etype == "image/offload" and is_json_object(data):
        targets = data.get("targets")
        if not isinstance(targets, list):
            raise fail("V3 image offload targets must be an array")
        remapped = []
        for value in targets:
            if not is_json_object(value):
                raise fail("V3 event reference container must be an object")
            remapped.append({**value, "seq": reference(value.get("seq"))})
        data = {**data, "targets": remapped}
    surface = event.get("surfaceOp")
    range_op = None if surface is None or surface == "append" else surface
    if range_op is not None and not is_json_object(range_op):
        raise fail("V3 event reference container must be an object")
    out: dict[str, Any] = {**event, "seq": seq, "data": data}
    if event.get("sourceEventSeqs") is not None:
        out["sourceEventSeqs"] = references(event["sourceEventSeqs"])
    if range_op is not None:
        out["surfaceOp"] = {**range_op,
                            "startSeq": reference(range_op.get("startSeq")),
                            "endSeq": reference(range_op.get("endSeq"))}
    return out


class _Stage:
    """v3→v4 迁移舞台（上游 ReleasedV3ToV4Stage 的整件等价物）。"""

    def __init__(self, header: dict, source_inherited: int | None) -> None:
        self.header = header
        self.source_inherited = source_inherited
        self.mapping: list[int] = []
        self.next_seq = 0
        self.time = header.get("createdAt")
        is_seeded = header.get("isSeeded")
        self.source_cut: int | None = 0 if not is_seeded else None
        self.cut: int | None = 0 if not is_seeded else None
        self.turn: int | None = None
        self.step_open = False
        self.next_turn_spliced = False
        self.foreign_delivery_seq: int | None = None
        self.out_events: list[dict] = []

    def transform(self, event: dict) -> None:
        if event.get("seq") != len(self.mapping):
            raise fail("format v3 source events must be dense")
        interrupted = self._observe_restart(event)
        if interrupted is not None:
            self.out_events.append({
                "type": "turn/end", "seq": self.next_seq,
                "time": event.get("time"),
                "data": {"turn": interrupted, "reason": {"kind": "interrupted"}},
            })
            self.next_seq += 1
        target_seq = self.next_seq
        self.next_seq += 1
        self.time = event.get("time")
        etype = event.get("type")
        data = event.get("data")
        if etype == "session/end-seed" and is_json_object(data) and data.get("inherited") is True:
            if not self.header.get("isSeeded"):
                raise fail("format v3 unseeded Session contains an inherited end-seed marker")
            self.source_cut = event.get("seq")
            self.cut = target_seq
        delivery_id = self._validate_delivery(event)
        if etype == "session-log-deepseek/delivery-accepted":
            if is_json_object(data) and data.get("sessionFormatVersion") == 4:
                raise unsupported("format v3 delivery marker claims target format v4")
            if delivery_id is not None and delivery_id != self.header.get("id"):
                self.foreign_delivery_seq = event.get("seq")
        opaque = self._namespace_opaque(event)
        if opaque is not event:
            self.mapping.append(target_seq)
            self.out_events.append(
                opaque if opaque.get("seq") == target_seq else {**opaque, "seq": target_seq})
            return
        if etype not in _disp.RELEASED_V3_EVENT_DISPOSITIONS:
            raise unsupported(
                f"format v3 contains unknown event type {etype!r} at seq {event.get('seq')}")
        remapped = _remap_v3_references(event, target_seq, self.mapping)
        self.mapping.append(target_seq)
        rewritten = _rewrite_event_sources(remapped)
        self.out_events.append(_migrate_event_content(_lift_tool_result(rewritten)))

    def finish(self) -> int:
        cut = count(self.cut, "V3 inherited event count")
        source_cut = count(self.source_cut, "V3 source inherited event count")
        if self.source_inherited is not None and source_cut != self.source_inherited:
            raise fail("format v3 inherited cut disagrees with its source marker")
        if self.foreign_delivery_seq is not None \
                and (self.header.get("parentSession") is None
                     or self.foreign_delivery_seq >= source_cut):
            raise fail("current-generation delivery marker names the wrong Session")
        return cut

    # ---------- 内部 ----------

    def _observe_restart(self, event: dict) -> int | None:
        data = event.get("data")
        interrupted = event.get("type") == "turn/start" and self.turn is not None \
            and not self.step_open and self.next_turn_spliced \
            and is_json_object(data) and data.get("turn") == self.turn + 1
        interrupted_turn = self.turn if interrupted else None
        self.next_turn_spliced = event.get("type") == "agent/inbox/spliced" \
            and is_json_object(data) and data.get("target") == "next-turn" \
            and isinstance(data.get("inserted"), list) and len(data["inserted"]) > 0
        if event.get("type") == "turn/start" and is_json_object(data) \
                and isinstance(data.get("turn"), int) and not isinstance(data.get("turn"), bool):
            self.turn = data["turn"]
        elif event.get("type") == "turn/end":
            self.turn = None
        elif event.get("type") == "step/start":
            self.step_open = True
        elif event.get("type") == "step/end":
            self.step_open = False
        return interrupted_turn

    def _validate_delivery(self, event: dict) -> str | None:
        if event.get("type") != "session-log-deepseek/delivery-accepted":
            return None
        data = event.get("data")
        if not is_json_object(data):
            raise fail("delivery-accepted data must be an object")
        version = count(0 if data.get("sessionFormatVersion") is None
                        else data["sessionFormatVersion"], "delivery sessionFormatVersion")
        if version != 3:
            return None
        through_seq = count(data.get("throughSeq"), "delivery throughSeq")
        if through_seq >= event.get("seq"):
            raise fail("delivery throughSeq must precede its marker")
        id_ = data.get("sessionId")
        if not isinstance(id_, str) or len(id_) == 0:
            raise fail("delivery requires a nonempty Session id")
        return id_

    def _namespace_opaque(self, event: dict) -> dict:
        if event.get("ignorable") is True \
                and event.get("type") not in _disp.RELEASED_V3_EVENT_DISPOSITIONS:
            return {**event, "type": f"plugin:{event.get('type')}", "ignorable": True}
        return event


def _migrate_v3_v4(artifact: dict) -> dict:
    """v3 → v4：证据修复 + 密集重映射 + tool/result 提升 + source 改名 +
    内容标签前缀 + canonical 信封。"""
    header = artifact["header"]
    events = artifact["events"]
    source_inherited = artifact.get("inherited_event_count")
    # ① 源 v3 精确校验（source audit 的语义面：payload/流/关系全量）
    assert_released_v3_artifact(artifact)
    # ② 封闭词表（上游 assertEvent(v3)：RELEASED_V3 dispositions；未知含
    # ignorable 一律拒——ignorable 未知由 ③ 的 namespace 分支收留）
    stage = _Stage(header, source_inherited)
    for event in events:
        stage.transform(event)
    target_cut = stage.finish()
    target = {"header": _migrate_header_v3_v4(header),
              "inherited_event_count": target_cut,
              "events": stage.out_events}
    # ③ 目标快照 + v4 全量校验
    target = snapshot_json(target, "released v3-to-v4 target")
    assert_released_v4_artifact(target)
    return target


V3_TO_V4 = {
    "name": "@deepseek-ai/dsh-session-format-v3-to-v4",
    "from_version": 3,
    "to_version": 4,
    "migrate_header": _migrate_header_v3_v4,
    "migrate": _migrate_v3_v4,
}
