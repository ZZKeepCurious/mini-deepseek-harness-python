"""v2→v3 相邻迁移（上游 session-format-v2-to-v3/src/{migration,references,payload}.ts
逐语义移植；整件函数载体，与 v0→v1/v1→v2 同构——流式 Stage 架构是上游性能机制，
mini 整件迁移已登记 verified-diffs §2.24 载体差异）。

五步：源 v2 精确校验 + 封闭词表（RELEASED_V2 dispositions + feedback 两事件；
未知含 ignorable 拒）→ 消息 id 观察（生成 id 冲突预查）→ system head 提升
（首个 step/start 后插空 head；逐 request/header 比对 header.system——变化点在
该 header 前插入 replace/append 事件，system 键一律剥离；合成 id
`v2-to-v3-system-<sha256>`）→ 密集重映射（五组同制品引用 + surface 信封）→
PTC 词汇改名（tool/code-dispatch{,-start}→tool/ptc-dispatch{,-start}、
plugin tools-code-mode→tools-ptc、preset `code`→`ptc`）→ canonical 信封
（replace 端点 start/end→startSeq/endSeq、空可选 header 键省略）→ 目标 v3
全量校验。

delivery guards：`session-log-deepseek/delivery-accepted` 声称 v3 → 拒（防升版
把 V2 水位提升为 V3 上传水位）；foreign marker（sessionId≠本会话）仅允许在
有 parentSession 的继承前缀内。预设改名覆盖 creation header 与**每个**
agent-preset/selected 选择事件（最近一次选择控制 resume、更早的选择控制历史
fork）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from . import dispositions as _disp
from .helpers import (
    count,
    fail,
    is_json_object,
    snapshot_json,
    unsupported,
)
from .validate_v2 import assert_released_v2_artifact, assert_released_v2_header
from .validate_v3 import (
    SURFACE_TYPES_V3,
    assert_released_v3_artifact,
    assert_v3_event,
)

__all__ = ["V2_TO_V3"]

_FEEDBACK_TYPES = frozenset({"feedback/message-put", "feedback/message-delete"})

_SYSTEM_PROMPT_PLUGIN = "@deepseek-ai/dsh-system-prompt"


def _migrate_header_v2_v3(header: dict) -> dict:
    assert_released_v2_header(header)
    out = {**header, "version": 3}
    if header.get("agentPreset") == "code":
        out["agentPreset"] = "ptc"
    return out


def _json_identity(parts: list[Any]) -> str:
    """JS JSON.stringify 紧凑形（无空格、非 ASCII 不转义）——合成 id 的哈希
    输入必须与上游逐字节一致。"""
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def _message_ids(event: dict) -> list[str]:
    """观察位置的消息 id（上游 observeMessageIds：五个 owned content 槽）。"""
    etype = event["type"]
    data = event["data"]
    if etype == "user/message":
        messages = [data]
    elif etype in ("assistant/message", "tool/result"):
        messages = [data.get("message") or {}]
    elif etype == "agent/inbox/spliced":
        messages = list(data.get("inserted") or [])
    elif etype == "session/title-llm-request":
        messages = list(data.get("messages") or [])
    else:
        return []
    ids = []
    for message in messages:
        if isinstance(message, dict) and isinstance(message.get("id"), str):
            ids.append(message["id"])
    return ids


class _Stage:
    """v2→v3 迁移舞台（上游 ReleasedV2ToV3Stage 的整件等价物）。"""

    def __init__(self, header: dict, source_inherited: int) -> None:
        self.header = header
        self.source_inherited = source_inherited
        self.mapping: list[int] = []
        self.original_ids: set[str] = set()
        self.generated_ids: set[str] = set()
        self.target_seq = 0
        is_seeded = header.get("isSeeded")
        self.source_cut: int | None = 0 if not is_seeded else None
        self.target_cut: int | None = 0 if not is_seeded else None
        self.last_foreign_delivery_seq: int | None = None
        self.step: dict | None = None
        self.head: int | None = None
        self.prompt = ""
        self.out_events: list[dict] = []

    # ---------- 事件处理 ----------

    def transform(self, event: dict) -> None:
        if event["seq"] != len(self.mapping):
            raise fail("format v2 source events must be dense")
        etype = event["type"]
        data = event["data"]
        for message_id in _message_ids(event):
            if message_id in self.generated_ids:
                raise unsupported(
                    "source message id collides with a generated system message id")
            self.original_ids.add(message_id)
        source_event = event
        if etype == "request/header":
            header_obj = dict(data.get("header") or {})
            system = header_obj.pop("system", None)
            prompt_now = system if isinstance(system, str) else ""
            if prompt_now != self.prompt:
                self.emit_system(prompt_now, event)
            source_event = {**event, "data": {**data, "header": header_obj}}
        if etype in SURFACE_TYPES_V3 and self.head is None:
            raise unsupported(
                "format v2 surface before first step cannot acquire a system head "
                "without changing chronology")
        if etype == "session/end-seed" and data.get("inherited") is True:
            if not self.header.get("isSeeded"):
                raise fail("format v2 unseeded Session contains an inherited end-seed marker")
            self.source_cut = event["seq"]
            self.target_cut = self.target_seq
        if etype == "session-log-deepseek/delivery-accepted":
            if data.get("sessionFormatVersion") == 3:
                raise fail("format v2 delivery marker claims target format v3")
            if data.get("sessionFormatVersion") == 2 \
                    and data.get("sessionId") != self.header["id"]:
                self.last_foreign_delivery_seq = event["seq"]
        target = _remap_v2_v3(source_event, self.target_seq, self.mapping)
        self.mapping.append(self.target_seq)
        self.target_seq += 1
        self.out_events.append(_canonicalize_transformed(_rename_ptc(target)))
        if etype == "step/start":
            self.step = {"turn": data["turn"], "step": data["step"]}
            if self.head is None:
                self.emit_system("", event)
        elif etype in ("step/end", "turn/end"):
            self.step = None

    def finish(self) -> int:
        if self.header.get("isSeeded") and self.source_cut is None:
            raise fail("format v2 inherited end-seed marker is missing")
        cut = count(self.source_cut, "format v2 inherited end-seed marker")
        if self.source_inherited != cut:
            raise fail("format v2 inherited end-seed marker disagrees with its source cut")
        if self.last_foreign_delivery_seq is not None \
                and (self.header.get("parentSession") is None
                     or self.last_foreign_delivery_seq >= cut):
            raise fail("current-generation delivery marker names the wrong Session")
        if self.target_cut is None:
            raise fail("format v3 inherited event count is missing")
        return self.target_cut

    # ---------- system head 提升 ----------

    def emit_system(self, prompt: str, anchor: dict) -> None:
        if self.step is None:
            raise unsupported(
                "format v2 changed request prompt outside an open step cannot retain "
                "source chronology")
        identity = _json_identity(
            ["session-format-v2-to-v3", self.header["id"], anchor["seq"], anchor["type"]])
        system_id = "v2-to-v3-system-" + hashlib.sha256(
            identity.encode("utf-8")).hexdigest()
        if system_id in self.original_ids or system_id in self.generated_ids:
            raise unsupported(
                "generated system message id collides with an existing message id")
        self.generated_ids.add(system_id)
        seq = self.target_seq
        self.target_seq += 1
        event: dict[str, Any] = {
            "type": "system/message", "seq": seq, "time": anchor["time"],
            "data": {
                **self.step,
                "message": {
                    "id": system_id,
                    "role": "system",
                    "source": {"kind": "plugin", "plugin": _SYSTEM_PROMPT_PLUGIN},
                    "content": [] if prompt == "" else [{"type": "text", "text": prompt}],
                },
            },
        }
        if self.head is None:
            event["surfaceOp"] = "append"
        else:
            event["surfaceOp"] = {"op": "replace", "start": self.head, "end": self.head}
            event["sourceEventSeqs"] = [self.head]
        self.out_events.append(_canonicalize_transformed(event))
        self.head = seq
        self.prompt = prompt


def _remap_v2_v3(event: dict, seq: int, mapping: list[int]) -> dict:
    """显式局部坐标重映射（上游 references.ts remapEvent）：只重映射审计过的
    同制品引用——surface 信封 + command/done.sourceEventSeq + compaction
    shadowed* + title messageSeqs；captured/delivery 水位、turn/step、流块
    索引、数字 JSON 一律不重映射。"""
    def one(value: Any, label: str) -> int:
        source = count(value, "source event reference")
        target = mapping[source] if source < len(mapping) else None
        if source >= event["seq"] or target is None:
            raise fail("reference must name an earlier source event")
        return target

    data = event["data"]
    etype = event["type"]
    if etype == "command/done" and "sourceEventSeq" in data:
        data = {**data, "sourceEventSeq": one(data["sourceEventSeq"], "sourceEventSeq")}
    elif etype in ("compaction/summary", "compaction/prune"):
        shadowed_range = dict(data.get("shadowedRange") or {})
        shadowed_range["start"] = one(shadowed_range.get("start"), "shadowedRange start")
        shadowed_range["end"] = one(shadowed_range.get("end"), "shadowedRange end")
        updates: dict[str, Any] = {"shadowedRange": shadowed_range}
        if "shadowedSeqs" in data:
            updates["shadowedSeqs"] = [one(member, "shadowedSeqs member")
                                       for member in data["shadowedSeqs"]]
        data = {**data, **updates}
    elif etype in ("session/title", "session/title-llm-request"):
        data = {**data, "messageSeqs": [one(member, "messageSeqs member")
                                        for member in data.get("messageSeqs") or []]}
    out = {**event, "seq": seq, "data": data}
    if "sourceEventSeqs" in event:
        out["sourceEventSeqs"] = [one(member, "sourceEventSeqs member")
                                  for member in event["sourceEventSeqs"]]
    surface_op = event.get("surfaceOp")
    if surface_op is not None and surface_op != "append":
        if not is_json_object(surface_op):
            raise fail("sequence range must be an object")
        out["surfaceOp"] = {"op": "replace",
                            "start": one(surface_op.get("start"), "surface start"),
                            "end": one(surface_op.get("end"), "surface end")}
    return out


def _rename_ptc(event: dict) -> dict:
    """PTC 词汇改名（上游 renamePtcEvent）：源准入先于改名，payload 字段
    精确审计过。明确不改：run_code 工具名、含 :code: 的历史 id、相似插件名、
    任意文本/嵌套 JSON。"""
    etype = event["type"]
    data = event["data"]
    if etype == "agent-preset/selected":
        if data.get("agentPreset") == "code":
            return {**event, "data": {**data, "agentPreset": "ptc"}}
        return event
    if etype == "tool/code-dispatch-start":
        return {**event, "type": "tool/ptc-dispatch-start"}
    if etype == "tool/code-dispatch":
        return {**event, "type": "tool/ptc-dispatch"}

    def rename_message_source(message: Any) -> Any:
        source = message.get("source") if isinstance(message, dict) else None
        if not isinstance(source, dict) or source.get("kind") != "plugin" \
                or source.get("plugin") != "tools-code-mode":
            return message
        return {**message, "source": {**source, "plugin": "tools-ptc"}}

    if etype == "user/message":
        renamed = rename_message_source(data)
        return event if renamed is data else {**event, "data": renamed}
    if etype in ("agent/inbox/spliced", "session/title-llm-request"):
        key = "inserted" if etype == "agent/inbox/spliced" else "messages"
        messages = list(data.get(key) or [])
        renamed_list = [rename_message_source(member) for member in messages]
        if all(new is old for new, old in zip(renamed_list, messages)):
            return event
        return {**event, "data": {**data, key: renamed_list}}
    return event


def _canonicalize_transformed(event: dict) -> dict:
    """结构变换后的 canonical 化（上游 canonicalizeTransformedEvent）：replace
    端点名 start/end→startSeq/endSeq（V2 源名，count 校验）、request/header
    空可选键省略，随后全量 V3 信封校验。"""
    target = event
    operation = event.get("surfaceOp")
    if operation is not None and operation != "append":
        if not is_json_object(operation) or len(operation) != 3 \
                or operation.get("op") != "replace" \
                or "start" not in operation or "end" not in operation:
            raise fail(
                f"format v2 {event['type']} at seq {event['seq']} requires exact "
                "replace fields op/start/end")
        target = {**event, "surfaceOp": {
            "op": "replace",
            "startSeq": count(operation["start"],
                              f"format v2 {event['type']} at seq {event['seq']} replace start"),
            "endSeq": count(operation["end"],
                            f"format v2 {event['type']} at seq {event['seq']} replace end"),
        }}
    if target.get("type") == "request/header":
        data = target["data"]
        header = dict(data.get("header") or {})
        empty = [key for key in ("tools", "adapterDefaults")
                 if (isinstance(header.get(key), list) and len(header[key]) == 0)
                 or (isinstance(header.get(key), dict) and len(header[key]) == 0)]
        if empty:
            canonical = {k: v for k, v in header.items() if k not in empty}
            target = {**target, "data": {**data, "header": canonical}}
    assert_v3_event(target)
    return target


def _migrate_v2_v3(artifact: dict) -> dict:
    """v2 → v3：system head 提升 + 密集重映射 + PTC 词汇改名 + canonical 信封。"""
    header = artifact["header"]
    events = artifact["events"]
    source_inherited = artifact["inherited_event_count"]
    # ① 源 v2 精确校验（source audit 的语义面：payload/流/关系全量）
    assert_released_v2_artifact(artifact)
    # ② 封闭词表（上游 assertEvent(v2)：RELEASED_V2 dispositions + feedback；
    # 未知事件**含 ignorable** 一律拒）
    for event in events:
        etype = event["type"]
        if etype not in _disp.RELEASED_V2_EVENT_DISPOSITIONS and etype not in _FEEDBACK_TYPES:
            raise unsupported(
                "format v2 to v3 cannot safely transform unclassified event " + etype)
    # ③ 逐事件 stage（system head 提升 + 重映射 + 改名 + canonical 化）
    stage = _Stage(header, source_inherited)
    for event in events:
        stage.transform(event)
    # ④ finish（cut 对齐 + delivery guards）
    target_cut = stage.finish()
    target = {"header": _migrate_header_v2_v3(header),
              "inherited_event_count": target_cut,
              "events": stage.out_events}
    # ⑤ 目标快照 + v3 全量校验
    target = snapshot_json(target, "released v2-to-v3 target")
    assert_released_v3_artifact(target)
    return target


V2_TO_V3 = {
    "name": "@deepseek-ai/dsh-session-format-v2-to-v3",
    "from_version": 2,
    "to_version": 3,
    "migrate_header": _migrate_header_v2_v3,
    "migrate": _migrate_v2_v3,
}
