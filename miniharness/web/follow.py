"""session/follow WebController 语义（对齐 session-controller web/controller.ts）。"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, AsyncIterator

from ..core.session import thaw
from ..session_query.config import SessionQueryError
from ..session_query.observation import observe_session
from .api import _Reject
from .assistant_stream_follow import assistant_stream_baseline, bind_assistant_stream
from .event_listener import emit_event_frame, expand_created_backfill
from .history import (
    address_id,
    history_page,
    resolve_history_request,
    validate_observation_address,
)

__all__ = [
    "promote_prepared_observation",
    "run_session_follow",
    "opening_for_session_follow",
]


def _meta_from_header(header: dict) -> dict:
    meta: dict[str, Any] = {}
    if header.get("cwd") is not None:
        meta["cwd"] = header["cwd"]
    if header.get("parentSession") is not None:
        meta["parentSession"] = header["parentSession"]
    if header.get("origin") is not None:
        meta["origin"] = header["origin"]
    if header.get("delegationDepth") is not None:
        meta["delegationDepth"] = header["delegationDepth"]
    if header.get("agentPreset") is not None:
        meta["agentPreset"] = header["agentPreset"]
    if header.get("isSeeded"):
        meta["isSeeded"] = True
    if header.get("createdAt") is not None:
        meta["createdAt"] = header["createdAt"]
    return meta


def promote_prepared_observation(api, observation) -> None:
    """prepared 读租约 → live 进店 + Agent attach（WebController.promote 等价）。"""
    header = observation.header
    session_id = header["id"]
    live = api.store.get(session_id)
    if live is not None:
        if api._agents.get(session_id) is None:
            api._attach(live)
        return
    seed = [thaw(dict(event)) for event in observation.events]
    session = api.store.prepare(session_id, {
        "meta": _meta_from_header(header),
        "seed": seed,
        "inheritedEventCount": observation.inherited_event_count,
        "mode": "restore",
    })
    api._attach(session)


def _validate_follow_address(address: Any) -> None:
    if not isinstance(address, dict):
        raise _Reject("gateway/arguments-invalid",
                      "address must be a session or subagent address", {})
    kind = address.get("kind")
    if kind == "session":
        if "sessionId" not in address:
            raise _Reject(
                "gateway/arguments-invalid",
                'args fields do not match the descriptor: missing "sessionId"', {})
        return
    if kind == "subagent":
        missing = [key for key in ("parentSessionId", "childSessionId") if key not in address]
        if missing:
            raise _Reject(
                "gateway/arguments-invalid",
                "args fields do not match the descriptor: "
                + "; ".join(f'missing "{key}"' for key in missing), {})
        return
    raise _Reject("gateway/arguments-invalid",
                  "address must be a session or subagent address", {})


def _map_observation_query_error(address: dict, error: SessionQueryError) -> None:
    if error.code == "SESSION_QUERY_SESSION_NOT_FOUND":
        if address.get("kind") == "session":
            target = address.get("sessionId", "")
            raise _Reject("session/not-found", f'session "{target}" not found',
                          {"sessionId": target}) from error
        raise _Reject("subagent/not-found", "subagent is unavailable", {
            "parentSessionId": address.get("parentSessionId"),
            "childSessionId": address.get("childSessionId"),
        }) from error
    raise error


def opening_for_session_follow(api, args: dict) -> tuple[dict, int]:
    """observation 快照 + 可选 activate/promote；租约关闭后返回 opening 与 next_seq。"""
    address = args.get("address")
    if address is None:
        raise _Reject("gateway/arguments-invalid",
                      'args fields do not match the descriptor: missing "address"', {})
    _validate_follow_address(address)
    spec = resolve_history_request({
        "throughSeq": args.get("throughSeq"),
        "beforeSeq": args.get("beforeSeq"),
        "maxMessages": args.get("maxMessages"),
        "turnWindow": args.get("turnWindow"),
    })
    session_id = address_id(address)
    need_projection = True
    activate = args.get("activate") is True
    want_assistant_stream = args.get("assistantStream") is True
    try:
        with observe_session(api.ctx, session_id, projection=need_projection) as observation:
            if need_projection:
                api._attach_observation_projections(observation)
            validate_observation_address(address, observation)
            page = history_page(observation, spec, api._paginate)
            projections = observation.projections or {"asOfSeq": page["cursor"], "values": {}}
            opening: dict[str, Any] = {
                "type": "snapshot",
                "header": dict(observation.header),
                "cursor": page["cursor"],
                "records": page["records"],
                "hasMore": page["hasMore"],
                "projections": {
                    "asOfSeq": projections["asOfSeq"],
                    "values": projections["values"],
                },
            }
            if want_assistant_stream:
                opening["assistantStream"] = assistant_stream_baseline(session_id)
            next_seq = page["cursor"] + 1

            if (activate and observation.source == "prepared"
                    and observation.header.get("origin") != "subagent"):
                promotion = observation.retain()
                try:
                    promote_prepared_observation(api, promotion)
                except Exception:
                    promotion.close()
                    raise
                promotion.close()
            elif activate and observation.source == "live":
                live = api.store.get(session_id)
                if live is not None and api._agents.get(session_id) is None:
                    api._attach(live)
        return opening, next_seq
    except SessionQueryError as error:
        _map_observation_query_error(address, error)


async def run_session_follow(api, args: dict, signal) -> AsyncIterator[dict]:
    address = args.get("address")
    if address is None:
        raise _Reject("gateway/arguments-invalid",
                      'args fields do not match the descriptor: missing "address"', {})
    _validate_follow_address(address)
    session_id = address_id(address)
    want_assistant_stream = args.get("assistantStream") is True

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    state: dict[str, Any] = {"next_seq": None}
    disposers: list[Any] = []

    def enqueue(item: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, item)

    def on_event(payload: dict) -> None:
        current = payload.get("session")
        event = payload.get("event")
        if getattr(current, "session_id", None) != session_id or not isinstance(event, Mapping):
            return
        seq = event["seq"]
        next_seq = state["next_seq"]
        if next_seq is not None and seq < next_seq:
            return
        if next_seq is not None and seq != next_seq:
            enqueue({"__error__": (
                "gateway/internal",
                f"session event stream skipped seq {next_seq}",
                {})})
            return
        state["next_seq"] = seq + 1
        enqueue(emit_event_frame(event))

    def on_created(payload: dict) -> None:
        session = payload.get("session")
        if getattr(session, "session_id", None) != session_id:
            return
        try:
            frames, next_seq = expand_created_backfill(session, state["next_seq"])
        except _Reject as error:
            enqueue({"__error__": (error.code, error.message, error.details)})
            return
        for frame in frames:
            enqueue(frame)
        state["next_seq"] = next_seq

    ctx = api.ctx
    disposers.extend([
        ctx.on("session/event", on_event, global_=True),
        ctx.on("session/created", on_created, global_=True),
    ])
    assistant_disposer = None
    if want_assistant_stream:
        assistant_disposer = bind_assistant_stream(ctx, session_id, enqueue)

    try:
        opening, next_seq = opening_for_session_follow(api, args)
        state["next_seq"] = next_seq
        yield opening

        while True:
            if signal is not None and getattr(signal, "cancelled", lambda: False)():
                return
            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.05)
            except asyncio.TimeoutError:
                continue
            if isinstance(item, dict) and "__error__" in item:
                code, message, details = item["__error__"]
                raise _Reject(code, message, details)
            yield item
    except SessionQueryError as error:
        _map_observation_query_error(address, error)
    finally:
        if assistant_disposer is not None:
            assistant_disposer()
        for dispose in disposers:
            dispose()
