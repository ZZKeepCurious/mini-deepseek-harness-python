"""released 跨事件关系状态机（上游 session-format-v0-to-v1/src/relationships.ts 逐字移植）。

校验「足以安全重建一个当前 Session」的事件间关系：turn/step 开闭、surface 替换、
工具生命周期（advertise→started→settled / TOOL_NOT_STARTED 修复）、PTC 根系、
重试链、命令配对、compaction 事务、标题来源。`extensions` 复刻上游
`ReleasedRelationshipExtensions`（v2 复用本机时传 `stepEvents`/`preservedSourceTitleRequestText`）。
"""
from __future__ import annotations

from typing import Any

from . import dispositions as _disp
from .helpers import (
    fail,
    js_stringify,
    released_v0_record,
)

__all__ = ["assert_released_artifact_relationships"]

SURFACE_TYPES = frozenset({"user/message", "assistant/message", "tool/result"})


def assert_released_artifact_relationships(artifact: dict,
                                           extensions: dict | None = None) -> None:
    """跨事件关系（上游 assertReleasedArtifactRelationships）。"""
    extensions = extensions or {}
    step_events = extensions.get("stepEvents")
    preserved_title_text = extensions.get("preservedSourceTitleRequestText") is True
    events = artifact["events"]
    open_turn: int | None = None
    open_step: int | None = None
    open_step_provider: str | None = None
    next_turn = 1
    next_step = 1
    surface: list[int] = []
    open_compaction: dict | None = None
    stale_compaction_starts = _inherited_orphan_compaction_starts(events)
    retries: list[dict] = []
    retry_starts: set[str] = set()
    ptc_roots: dict[str, str] = {}
    ptc_starts: dict[str, dict] = {}
    tool_lifecycles: dict[str, dict] = {}
    command_runs: set[str] = set()

    for event in events:
        etype = event["type"]
        extension_step_event = isinstance(step_events, (set, frozenset)) \
            and etype in step_events
        if _disp.RELEASED_V0_EVENT_DISPOSITIONS.get(etype) is None \
                and not extension_step_event:
            continue
        data = released_v0_record(event.get("data"), f"{etype} {event.get('seq')} data")
        if etype in SURFACE_TYPES:
            surface = _apply_surface(surface, event)
        if etype in ("turn/start", "turn/end") and open_compaction is not None \
                and open_compaction["startSeq"] not in stale_compaction_starts:
            raise fail(f"{etype} crosses an open compaction")
        if extension_step_event:
            _require_open_step(etype, data, open_turn, open_step)
            continue

        if etype == "turn/start":
            if open_turn is not None or data.get("turn") != next_turn:
                raise fail(
                    f"turn/start {js_stringify(data.get('turn'))} does not open expected turn {next_turn}")
            open_turn = data.get("turn")
            open_step = None
            tool_lifecycles.clear()
            next_step = 1
        elif etype == "turn/end":
            if open_turn != data.get("turn"):
                raise fail(
                    f"turn/end {js_stringify(data.get('turn'))} has no matching open turn")
            _assert_no_unresolved_tools(tool_lifecycles, "turn/end")
            if open_step is not None:
                raise fail(f"turn/end {js_stringify(data.get('turn'))} crosses an open step")
            open_turn = None
            next_turn += 1
        elif etype == "step/start":
            if open_turn != data.get("turn") or open_step is not None \
                    or data.get("step") != next_step:
                raise fail(f"{etype} does not match the open turn and next step")
            open_step = data.get("step")
        elif etype == "step/end":
            _require_open_step(etype, data, open_turn, open_step)
            _assert_no_unresolved_tools(tool_lifecycles, "step/end")
            tool_lifecycles.clear()
            open_step = None
            next_step += 1
        elif etype == "assistant/chunk":
            _require_open_step(etype, data, open_turn, open_step)
        elif etype == "assistant/message":
            _require_open_step(etype, data, open_turn, open_step)
            message = released_v0_record(data.get("message"),
                                         f"assistant/message {event.get('seq')} message")
            for block in message.get("content"):
                if block.get("type") != "tool-call":
                    continue
                call_id = block.get("id")
                if call_id in tool_lifecycles:
                    raise fail(f"assistant/message repeats advertised tool call {call_id}")
                tool_lifecycles[call_id] = {
                    "name": block.get("name"),
                    "arguments": block.get("arguments"),
                    "state": "advertised",
                }
        elif etype == "tool/call":
            _require_open_step(etype, data, open_turn, open_step)
            call_id = data.get("callId")
            lifecycle = tool_lifecycles.get(call_id)
            if lifecycle is None or lifecycle["state"] != "advertised" \
                    or lifecycle["name"] != data.get("name") \
                    or lifecycle["arguments"] != data.get("arguments"):
                raise fail(f"tool/call {call_id} does not match one advertised tool call")
            lifecycle["state"] = "started"
        elif etype == "tool/result":
            if event.get("surfaceOp") == "append":
                _require_open_step(etype, data, open_turn, open_step)
                message = released_v0_record(data.get("message"),
                                             f"tool/result {event.get('seq')} message")
                source = released_v0_record(message.get("source"),
                                            f"tool/result {event.get('seq')} source")
                call_id = source.get("callId")
                content = message.get("content")
                error = None if data.get("error") is None \
                    else released_v0_record(data.get("error"),
                                            f"tool/result {event.get('seq')} error")
                lifecycle = tool_lifecycles.get(call_id)
                if lifecycle is None:
                    raise fail(f"tool/result {call_id} has no advertised tool lifecycle")
                if lifecycle["state"] == "advertised" \
                        and not _is_exact_tool_not_started_repair(event, content, error):
                    raise fail(f"tool/result {call_id} is not the exact TOOL_NOT_STARTED repair")
                del tool_lifecycles[call_id]
            elif open_turn is None:
                raise fail("tool/result replacement is outside an open turn")
        elif etype == "request/header":
            if open_turn is None:
                raise fail(f"{etype} is outside an open turn")
            open_step_provider = data["header"]["config"]["provider"]
        elif etype == "request/context":
            if open_turn is None:
                raise fail(f"{etype} is outside an open turn")
        elif etype in ("tool/code-dispatch-start", "tool/code-dispatch"):
            if open_turn is None:
                raise fail(f"{etype} is outside an open turn")
            root = data.get("rootCallId")
            parent = data.get("parentCallId")
            child = data.get("subCallId")
            known = ptc_roots.get(child)
            if known is not None and known != root:
                raise fail(f"{etype} changes its rootCallId")
            if parent != root and ptc_roots.get(parent) != root:
                raise fail(f"{etype} parentCallId does not belong to rootCallId")
            if etype == "tool/code-dispatch-start":
                if child in ptc_starts:
                    raise fail("tool/code-dispatch-start repeats subCallId")
                ptc_starts[child] = {
                    "root": root,
                    "parent": parent,
                    "name": data.get("name"),
                    "arguments": data.get("arguments"),
                    "settled": False,
                }
            else:
                start = ptc_starts.get(child)
                if start is None or start["settled"]:
                    raise fail("tool/code-dispatch has no unique start")
                if start["root"] != root or start["parent"] != parent \
                        or start["name"] != data.get("name") \
                        or not _deep_equal(start["arguments"], data.get("arguments")):
                    raise fail("tool/code-dispatch does not match its start")
                start["settled"] = True
            ptc_roots[child] = root
        elif etype == "llm/retry":
            _require_open_step(etype, data, open_turn, open_step)
            if data.get("provider") != open_step_provider:
                raise fail("llm/retry provider does not match the open request/header")
            _assert_retry_chain(retries, data)
            retries.append(event)
        elif etype == "llm/retry-started":
            scheduled = None
            for candidate in retries:
                prior = candidate.get("data")
                if prior.get("retryId") == data.get("retryId") \
                        and prior.get("retry") == data.get("retry"):
                    scheduled = candidate
            if scheduled is None:
                raise fail("llm/retry-started pairs no prior scheduled attempt")
            prior = scheduled.get("data")
            if prior.get("turn") != data.get("turn") or prior.get("step") != data.get("step"):
                raise fail("llm/retry-started does not match its scheduled turn and step")
            key = f"{js_stringify(data.get('retryId'))}\0{js_stringify(data.get('retry'))}"
            if key in retry_starts:
                raise fail("llm/retry-started repeats one scheduled attempt")
            retry_starts.add(key)
        elif etype in ("session/title", "session/title-llm-request"):
            _assert_title_sources(events, event, data, not preserved_title_text)
        elif etype == "command/run":
            cid = data.get("commandId")
            if cid in command_runs:
                raise fail(f"command/run repeats commandId {cid}")
            command_runs.add(cid)
        elif etype == "command/done":
            cid = data.get("commandId")
            if cid not in command_runs:
                raise fail(f"command/done {cid} has no prior command/run")
            source_seq = data.get("sourceEventSeq")
            if source_seq is not None:
                source = events[source_seq]
                if data.get("kind") != "success" \
                        or source.get("type") in ("command/run", "command/done"):
                    raise fail(f"command/done {cid} has invalid sourceEventSeq")
        elif etype == "session-log-deepseek/delivery-accepted":
            accepted = data.get("sessionFormatVersion", 0)
            if accepted == artifact["header"]["version"]:
                inherited = artifact["header"].get("parentSession") is not None \
                    and event.get("seq") < artifact.get("inherited_event_count", 0)
                if not inherited and data.get("sessionId") != artifact["header"].get("id"):
                    raise fail("current-generation delivery marker names the wrong Session")
        elif etype == "compaction/start":
            if open_compaction is not None:
                raise fail("compaction/start overlaps an open compaction")
            _assert_compaction_turn(data.get("turn"), open_turn, "compaction/start")
            entry: dict[str, Any] = {
                "id": data.get("compactionId"),
                "turn": data.get("turn"),
                "startSeq": event.get("seq"),
                "summarized": False,
            }
            if "sourceCommandId" in data:
                entry["sourceCommandId"] = data.get("sourceCommandId")
            open_compaction = entry
        elif etype == "compaction/summary":
            _assert_compaction_owner(open_compaction, data, "compaction/summary")
            _assert_compaction_turn(open_compaction["turn"], open_turn, "compaction/summary")
            if open_compaction["summarized"] is True:
                raise fail("compaction/summary repeats")
            _assert_current_surface_span(surface, data, "compaction/summary")
            open_compaction["summarized"] = True
        elif etype == "compaction/end":
            _assert_compaction_owner(open_compaction, data, "compaction/end")
            if data.get("turn") != open_compaction["turn"]:
                raise fail("compaction/end changes its owner turn")
            _assert_compaction_turn(open_compaction["turn"], open_turn, "compaction/end")
            if data.get("error") is None and open_compaction["summarized"] is not True:
                raise fail("successful compaction/end requires one summary")
            open_compaction = None
        elif etype == "compaction/prune":
            _assert_current_surface_span(surface, data, "compaction/prune")
        elif etype == "user/message":
            source = released_v0_record(data.get("source"),
                                        f"user/message {event.get('seq')} source")
            if event.get("surfaceOp") != "append" \
                    and source.get("kind") == "plugin" and source.get("plugin") == "compact":
                _assert_compaction_owner(open_compaction, source,
                                         f"compaction checkpoint at seq {event.get('seq')}")
        elif etype == "session/end-seed":
            open_compaction = None


def _inherited_orphan_compaction_starts(events: list[dict]) -> set[int]:
    stale: set[int] = set()
    open_: int | None = None
    for event in events:
        if event.get("type") == "compaction/start":
            open_ = event.get("seq")
        elif event.get("type") == "compaction/end":
            open_ = None
        elif event.get("type") == "session/end-seed":
            if open_ is not None:
                stale.add(open_)
            open_ = None
    return stale


def _assert_retry_chain(retries: list[dict], data: dict) -> None:
    prior = None
    for candidate in reversed(retries):
        value = candidate.get("data")
        if value.get("turn") == data.get("turn") and value.get("step") == data.get("step") \
                and value.get("provider") == data.get("provider") \
                and value.get("policyKey") == data.get("policyKey"):
            prior = candidate
            break
    expected = ((prior.get("data").get("retry", 0) if prior is not None else 0) + 1)
    if data.get("retry") != expected:
        raise fail(f"llm/retry must use retry {expected}")
    if prior is not None and prior.get("data").get("retryId") != data.get("retryId"):
        raise fail("llm/retry must preserve retryId across one policy chain")
    if prior is None and any(
            candidate.get("data").get("retryId") == data.get("retryId")
            for candidate in retries):
        raise fail(f"llm/retry reuses retryId {js_stringify(data.get('retryId'))} across policy chains")


def _require_open_step(event_type: str, data: dict, open_turn: int | None,
                       open_step: int | None) -> None:
    if data.get("turn") != open_turn or data.get("step") != open_step \
            or open_turn is None or open_step is None:
        raise fail(f"{event_type} does not match an open turn and step")


def _assert_no_unresolved_tools(lifecycles: dict, boundary: str) -> None:
    unresolved = next(iter(lifecycles), None)
    if unresolved is not None:
        raise fail(f"{boundary} leaves unresolved tool call {unresolved}")


def _is_exact_tool_not_started_repair(event: dict, content: list,
                                      error: dict | None) -> bool:
    data = event.get("data")
    message = data.get("message")
    source = message.get("source")
    call_id = source.get("callId")
    block = content[0] if content else None
    repair_content = block.get("content") if isinstance(block, dict) else None
    repair_text = repair_content[0].get("text") if isinstance(repair_content, list) \
        and len(repair_content) == 1 else None
    return (error is not None and error.get("name") == "ToolNotStartedError"
            and error.get("code") == "TOOL_NOT_STARTED"
            and event.get("sourceEventSeqs") is None
            and message.get("id") == f"interrupted-tool-result-{call_id}-{event.get('seq')}"
            and block is not None and block.get("isError") is True
            and isinstance(repair_content, list) and len(repair_content) == 1
            and isinstance(repair_content[0], dict) and repair_content[0].get("type") == "text"
            and repair_content[0].get("text")
            == "The tool call was interrupted before the Harness recorded it as started. "
               "Retry it if it is still needed.")


def _apply_surface(surface: list[int], event: dict) -> list[int]:
    operation = event.get("surfaceOp")
    if operation is None:
        raise fail(f"{event.get('type')} requires a surfaceOp marker")
    if operation == "append":
        return [*surface, event.get("seq")]
    start = _index_of(surface, operation["start"])
    end = _index_of(surface, operation["end"])
    if start < 0 or end < start:
        raise fail(f"{event.get('type')} replacement range is not on the current surface")
    shadowed = surface[start:end + 1]
    sources = event.get("sourceEventSeqs")
    source_set = set(sources) if isinstance(sources, list) else set()
    if any(seq not in source_set for seq in shadowed):
        raise fail(f"{event.get('type')} replacement sourceEventSeqs omit a shadowed surface node")
    return surface[:start] + [event.get("seq")] + surface[end + 1:]


def _index_of(values: list[int], item: Any) -> int:
    try:
        return values.index(item)
    except ValueError:
        return -1


def _assert_title_sources(events: list[dict], event: dict, data: dict,
                          validate_framed: bool) -> None:
    seqs = data.get("messageSeqs")
    if event.get("type") == "session/title":
        title_source = released_v0_record(data.get("source"),
                                          f"session/title {event.get('seq')} source")
        if (len(seqs) == 0) != (title_source.get("kind") == "user"):
            raise fail(f"session/title {event.get('seq')} messageSeqs must be empty exactly for a user title")
    selected: list[dict] = []
    for seq in seqs:
        source = events[seq]
        if source.get("type") != "user/message":
            raise fail(
                f"{event.get('type')} {event.get('seq')} messageSeqs must cite earlier human user/message events")
        source_data = released_v0_record(source.get("data"), f"{source.get('type')} {seq} data")
        provenance = released_v0_record(source_data.get("source"),
                                        f"{source.get('type')} {seq} source")
        if provenance.get("kind") != "user":
            raise fail(
                f"{event.get('type')} {event.get('seq')} messageSeqs must cite earlier human user/message events")
        texts = [block["text"] for block in source_data.get("content")
                 if block.get("type") == "text" and isinstance(block.get("text"), str)]
        selected.append({"seq": seq, "text": "\n".join(texts)})
    if event.get("type") == "session/title-llm-request":
        messages = data.get("messages")
        expected = "Generate the session title from this JSON array of human messages:\n" \
            + js_stringify(selected)
        message = messages[0] if len(messages) > 0 else None
        content = message.get("content") if isinstance(message, dict) else None
        if message is None:
            source = None
        else:
            source = released_v0_record(message.get("source"),
                                        "session/title-llm-request message source")
        if len(messages) != 1 or message is None or message.get("role") != "user" \
                or content is None or len(content) != 1 or source is None \
                or source.get("kind") != "plugin" or source.get("plugin") != "dsh-session-title-llm":
            raise fail("session/title-llm-request messages do not represent messageSeqs")
        framed = content[0]
        if framed is None or framed.get("type") != "text" \
                or (validate_framed and framed.get("text") != expected):
            raise fail("session/title-llm-request messages do not represent messageSeqs")


def _assert_compaction_owner(open_: dict | None, data: dict, type_: str) -> None:
    if open_ is None or data.get("compactionId") != open_["id"] \
            or data.get("sourceCommandId") != open_.get("sourceCommandId"):
        raise fail(f"{type_} has no matching compaction/start")


def _assert_compaction_turn(owner: int | None, open_turn: int | None, type_: str) -> None:
    if owner is None:
        if open_turn is not None:
            raise fail(f"{type_} does not match the open turn")
    elif owner != open_turn:
        raise fail(f"{type_} does not match the open turn")


def _assert_current_surface_span(surface: list[int], data: dict, type_: str) -> None:
    range_ = data.get("shadowedRange")
    seqs = data.get("shadowedSeqs")
    start = _index_of(surface, range_["start"])
    end = _index_of(surface, range_["end"])
    expected = [] if start < 0 or end < start else surface[start:end + 1]
    if len(expected) != len(seqs) or any(a != b for a, b in zip(expected, seqs)):
        raise fail(f"{type_} shadowedSeqs do not name an exact current surface span")


def _deep_equal(a: Any, b: Any) -> bool:
    from .helpers import deep_equal
    return deep_equal(a, b)