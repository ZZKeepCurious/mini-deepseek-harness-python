"""实验性 plugin record（对齐 packages/core/session/src/index.ts appendPluginRecord）。"""
from __future__ import annotations

import re
from typing import Any

from .json import is_json_safe

__all__ = [
    "PLUGIN_RECORD_TYPE",
    "append_plugin_record",
    "is_plugin_record_type",
    "plugin_record_of",
]

PLUGIN_RECORD_TYPE = re.compile(
    r"^plugin:[a-z0-9][a-z0-9._-]*(?:\/[a-z0-9][a-z0-9._-]*)*$")


def is_plugin_record_type(type_: str) -> bool:
    return isinstance(type_, str) and PLUGIN_RECORD_TYPE.match(type_) is not None


def plugin_record_of(event: dict) -> dict | None:
    if event.get("ignorable") is not True:
        return None
    type_ = event.get("type")
    if not is_plugin_record_type(type_):
        return None
    return {
        "type": type_,
        "seq": event.get("seq"),
        "time": event.get("time"),
        "data": event.get("data"),
    }


def plugin_record_payload(type_: str, data: Any) -> dict:
    if not is_plugin_record_type(type_):
        raise ValueError(
            f'plugin record type "{type_}" must be "plugin:" followed by '
            "lowercase slash-separated segments")
    if not isinstance(data, dict):
        raise TypeError("plugin record data must be a JSON object")
    payload = {"type": type_, "data": data, "ignorable": True}
    if not is_json_safe(payload):
        raise TypeError(f"plugin record must be losslessly JSON-serializable: {type_!r}")
    return payload


def append_plugin_record(session: Any, type_: str, data: dict) -> int:
    """向会话日志追加一条 ignorable plugin record，返回 seq。"""
    return session.append_plugin_record(type_, data)
