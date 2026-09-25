"""time-context：可选的每步时钟上下文（对齐 packages/context/time-context）。

在符合条件的 step 上追加一条带源归属的持久读数：当前时间（数值偏移 + IANA 时区）、
开放请求的浏览器时区策略、以及自上一个模型可见消息起的耗时。opt-in：默认组合不装。

载体差异（登记）：
  * 上游用 `Intl.DateTimeFormat`；mini 用 `zoneinfo` + `datetime` 格式化 ISO 形状时间戳。
  * 上游进程缺省时区为 IANA 名；mini 依次尝试 `TZ` 环境变量、`/etc/localtime` 链接，
    否则退回固定偏移标签 `UTC±HH:MM`（无法取得 IANA 名时的诚实降级）。
  * 上游经 `ctx.sessionProjections.register` 记录读数；mini 用 M7 投影注册表同款 API。
  * 上游 `PreStepDecision` 显式 `{kind:'enter', messages}`；mini 的 pre-step payload 即
    决策对象，监听器返回同形 dict（追加 messages）。
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..core.scope import Context
from ..core.session.message import create_message, text_block
from ..session_projection import ProjectionDefinition

__all__ = [
    "NAME",
    "apply_time_context",
    "create_timestamp_formatter",
    "derive_browser_time_zone_context",
    "format_duration",
    "format_timestamp",
    "install_time_context",
    "render_browser_time_zone_context",
    "request_messages",
]

NAME = "time-context"

#: 浏览器时区词法（request-zone.ts:6）。
IANA_TIME_ZONE = re.compile(r"^[A-Za-z][A-Za-z0-9_+.-]*(?:/[A-Za-z0-9_+.-]+)+$")


def format_duration(elapsed_ms: float) -> str:
    """非负毫秒 → 紧凑整秒单位（index.ts:63-77）。"""
    seconds = int(max(0, elapsed_ms) // 1000)
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def _resolve_zone(time_zone: str) -> ZoneInfo:
    """解析一个规范 IANA 时区或 UTC；不支持即抛 TypeError。"""
    if time_zone != "UTC" and IANA_TIME_ZONE.match(time_zone) is None:
        raise TypeError(
            f"browser time zone must be canonical UTC or IANA Area/Location: {time_zone!r}")
    try:
        return ZoneInfo(time_zone)
    except Exception as error:  # noqa: BLE001 - ZoneInfoNotFoundError / tzdata 缺失
        raise TypeError(f"browser time zone is unsupported: {time_zone!r}") from error


def create_timestamp_formatter(time_zone: str | None = None) -> tuple[Any, str]:
    """返回 (tzinfo, 规范标签)；缺省解析进程时区（timestamp.ts:10-22 的等价面）。

    进程时区：`TZ` 环境变量 → `/etc/localtime` 链接 → 固定偏移标签 `UTC±HH:MM`。
    """
    if time_zone is not None:
        return _resolve_zone(time_zone), time_zone
    env_zone = os.environ.get("TZ")
    if env_zone:
        try:
            return _resolve_zone(env_zone), env_zone
        except TypeError:
            pass
    link = "/etc/localtime"
    try:
        target = os.path.realpath(link)
        marker = "/zoneinfo/"
        index = target.find(marker)
        if index >= 0:
            name = target[index + len(marker):]
            return _resolve_zone(name), name
    except OSError:
        pass
    local = datetime.now().astimezone()
    offset = local.utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    label = f"UTC{sign}{hours:02d}:{minutes:02d}"
    return timezone(offset, label), label


def format_timestamp(now_ms: float, tzinfo: Any, time_zone: str) -> str:
    """纪元毫秒 → 带数值偏移与 IANA 标签的 ISO 形状时间戳（timestamp.ts:31-37）。"""
    moment = datetime.fromtimestamp(now_ms / 1000, tz=tzinfo)
    offset = moment.strftime("%z") or "+0000"
    offset = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}{offset}[{time_zone}]"


def _browser_time_zone(message: Any) -> str | None:
    """从一个普通 user-rpc 消息读取 Host 规范化的浏览器时区（request-zone.ts:15-40）。"""
    source = message.get("source") if isinstance(message, dict) else None
    if not isinstance(source, dict):
        return None
    if source.get("kind") != "user" or "rpcId" not in source:
        return None
    value = source.get("clientTimeZone")
    if not isinstance(value, str):
        return None
    _resolve_zone(value)
    return value


def derive_browser_time_zone_context(messages: list) -> dict:
    """派生一个开放 turn 的唯一/混合/缺失浏览器时区（request-zone.ts:48-59）。"""
    zones = sorted({zone for zone in (_browser_time_zone(m) for m in messages) if zone})
    if not zones:
        return {"kind": "missing"}
    if len(zones) == 1:
        return {"kind": "resolved", "timeZone": zones[0]}
    return {"kind": "mixed", "timeZones": zones}


def render_browser_time_zone_context(context: dict) -> str:
    """渲染面向模型的一条持久策略行（request-zone.ts:66-80 逐字）。"""
    kind = context["kind"]
    if kind == "resolved":
        return (f"Browser time zone for this request: {context['timeZone']}. "
                "Interpret otherwise-unqualified dates and times in this zone.")
    if kind == "mixed":
        import json
        zones = json.dumps(list(context["timeZones"]), separators=(",", ":"))
        return (f"Browser time zone for this request: mixed {zones}. "
                "Ask the user to clarify otherwise-unqualified dates and times.")
    return ("Browser time zone for this request: unavailable. "
            "Ask the user to clarify otherwise-unqualified dates and times.")


def request_messages(agent: Any, turn: int, proposed: list) -> list:
    """收集某开放 turn 内已进入与提议中的 user 消息（index.ts:80-91）。"""
    entered: list = []
    for seq in range(agent.session.seq - 1, -1, -1):
        event = agent.session.event_at(seq)
        if event is None:
            break
        if event.get("type") == "turn/start" and (event.get("data") or {}).get("turn") == turn:
            return list(reversed(entered)) + list(proposed)
        if event.get("type") == "user/message":
            entered.append(event.get("data"))
    return list(proposed)


def _render_text(now_ms: float, turn: int, step: int, previous: Any, tzinfo: Any,
                 time_zone: str, browser: dict) -> str:
    elapsed = "unavailable" if previous is None else format_duration(now_ms - previous)
    baseline = "model-visible message" if step == 1 else "step context"
    return (f"Time sampled while preparing turn {turn}, step {step}: "
            f"{format_timestamp(now_ms, tzinfo, time_zone)}\n"
            f"{render_browser_time_zone_context(browser)}\n"
            f"Elapsed since the preceding {baseline}: {elapsed}.")


def _validate_config(config: dict | None) -> tuple[str | None, int | None]:
    config = dict(config or {})
    time_zone = config.get("timeZone")
    if time_zone is not None and not isinstance(time_zone, str):
        raise TypeError(f"time-context: timeZone must be a string, got {time_zone!r}")
    refresh = config.get("refreshIntervalMs")
    if refresh is not None and (isinstance(refresh, bool)
                                or not isinstance(refresh, int) or refresh < 0):
        raise TypeError(
            f"time-context: refreshIntervalMs must be a non-negative safe integer, got {refresh!r}")
    return time_zone, refresh


def _init_projection(header: Any = None, inherited: Any = None) -> dict:
    return {"lastMessageTime": None, "lastInjectionTime": None, "lastTurnInjectionTime": None}


def _apply_projection(state: dict, event: dict) -> dict:
    type_ = event.get("type")
    data = event.get("data") or {}
    if type_ in ("turn/start", "turn/end"):
        return state if state["lastTurnInjectionTime"] is None \
            else {**state, "lastTurnInjectionTime": None}
    if type_ == "user/message":
        source = data.get("source") or {}
        injected = source.get("kind") == NAME
        with_message = (state if state["lastMessageTime"] == event.get("time")
                        else {**state, "lastMessageTime": event.get("time")})
        if not injected:
            return with_message
        return {**with_message, "lastInjectionTime": event.get("time"),
                "lastTurnInjectionTime": event.get("time")}
    if type_ in ("assistant/message", "tool/result"):
        return (state if state["lastMessageTime"] == event.get("time")
                else {**state, "lastMessageTime": event.get("time")})
    return state


def apply_time_context(ctx: Context, config: dict | None = None) -> None:
    """注册投影单元与 prepend 的 pre-step 监听器（index.ts:128-221）。"""
    time_zone, refresh_interval = _validate_config(config)
    try:
        fallback_tzinfo, fallback_zone = create_timestamp_formatter(time_zone)
    except TypeError as error:
        if time_zone is None:
            raise RuntimeError("time-context: failed to resolve the system time zone") from error
        raise RuntimeError(f"time-context: invalid IANA timeZone {time_zone!r}") from error
    formatters: dict[str, Any] = {fallback_zone: fallback_tzinfo}

    def formatter_for(zone: str):
        if zone not in formatters:
            tzinfo, _ = create_timestamp_formatter(zone)
            formatters[zone] = tzinfo
        return formatters[zone]

    projections = ctx.get("sessionProjections")
    if projections is None:
        raise RuntimeError(
            "time-context: the sessionProjections service is required (install M7 registry)")
    projections.register(ProjectionDefinition(
        "timeContext", init=_init_projection, apply=_apply_projection, state_version=2))

    async def _on_pre_step(payload: dict, next_fn) -> dict:
        decision = await next_fn()
        if isinstance(decision, dict) and decision.get("kind") == "reject":
            return decision
        signal = payload.get("signal")
        if signal is not None and getattr(signal, "aborted", False):
            return decision
        agent = payload.get("agent")
        turn = payload.get("turn")
        step = payload.get("step")
        if agent is None or turn is None or step is None:
            return decision
        now = int(time.time() * 1000)
        state = projections.state_of(agent.session, "timeContext") or _init_projection()
        if refresh_interval is not None and refresh_interval > 0:
            last = state["lastInjectionTime"]
            if last is not None and now >= last and now - last < refresh_interval:
                return decision
        previous = (state["lastMessageTime"] if step == 1
                    else state["lastTurnInjectionTime"])
        messages = decision.get("messages") if isinstance(decision, dict) else None
        proposed = list(messages) if isinstance(messages, list) else []
        browser = derive_browser_time_zone_context(request_messages(agent, turn, proposed))
        selected = browser["timeZone"] if browser["kind"] == "resolved" else fallback_zone
        text = _render_text(now, turn, step, previous, formatter_for(selected),
                            selected, browser)
        message = create_message("user", [text_block(text)], {
            "kind": NAME, "form": "snapshot",
            "sections": [{"name": NAME, "text": text}]})
        return {**decision, "messages": [*proposed, message]}

    ctx.on("agent/pre-step", _on_pre_step, prepend=True)


def install_time_context(ctx: Context, config: dict | None = None) -> None:
    """装配 time-context（幂等：同一 ctx 只注册一次）。"""
    if getattr(ctx, "_miniharness_time_context_installed", False):
        return
    apply_time_context(ctx, config)
    ctx._miniharness_time_context_installed = True
