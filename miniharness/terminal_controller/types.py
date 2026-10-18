"""terminal-controller 公共类型、配置与错误（对齐 packages/api/terminal-controller）。

上游：
  * `types.ts:8,10` Remote 错误码扩展 `terminal/control-unavailable` /
    `terminal/limit-reached`（details 分别带 reason / limit）。
  * `types.ts:20-64` wire 类型：TerminalShell / TerminalEnvironment /
    WebTerminalInfo / TerminalCreateRequest / TerminalFrame（snapshot 恒为首帧）/
    TerminalInput（`write{sequence,data}` / `resize{sequence,cols,rows}`）。
  * `index.ts:28-80` Config 的 Schemastery 缺省（shell 可选、shellCandidates、
    maxTerminals/maxCols/maxRows/scrollback/maxBufferedBytes/maxInputBytes/
    disposeGraceMs）。

载体（与上游差异，已在 verified-diffs / design-terminal-domain.md 登记）：
  * 上游 schemastery 在插件加载期校验 Config；mini 以 `resolve_config` 显式
    fail loud（同 headless/terminal-bash 惯例）。
  * wire 类型以纯 dict 承载（camelCase 键、判别字段 `type`），可直接 JSON 序列化。
"""
from __future__ import annotations

import json
import re
from typing import Any

__all__ = [
    "IDENTITY_PATTERN",
    "TERMINAL_CONFIG_DEFAULTS",
    "ERROR_CONTROL_UNAVAILABLE",
    "ERROR_LIMIT_REACHED",
    "ERROR_UNAVAILABLE",
    "TerminalControlUnavailable",
    "TerminalLimitReached",
    "TerminalUnavailable",
    "resolve_config",
    "is_identity",
    "frame_bytes",
    "decode_terminal_input",
]

#: WebTerminalId / TerminalAttachmentId 词法（index.ts:157,195 `^[\w-]{1,128}$`）：
#: 上游 `/u` 标志下 `\w` 仍等价 ASCII `[A-Za-z0-9_]`，故显式 `re.ASCII`。
IDENTITY_PATTERN = re.compile(r"^[\w-]{1,128}$", re.ASCII)

#: 上游 TerminalController.Config 的 Schemastery 缺省物化（index.ts:68-80）。
TERMINAL_CONFIG_DEFAULTS: dict[str, Any] = {
    "shell": None,
    "shellCandidates": ["zsh", "bash", "fish", "pwsh", "powershell", "cmd"],
    "maxTerminals": 8,
    "maxCols": 500,
    "maxRows": 200,
    "scrollback": 1000,
    "maxBufferedBytes": 2 * 1024 * 1024,
    "maxInputBytes": 64 * 1024,
    "disposeGraceMs": 1000,
}

#: Remote 错误码（types.ts:8,10,12）。
ERROR_CONTROL_UNAVAILABLE = "terminal/control-unavailable"
ERROR_LIMIT_REACHED = "terminal/limit-reached"
ERROR_UNAVAILABLE = "terminal/unavailable"

#: 数值字段的上界要求（字段 → 最小合法值；None 表示仅要求非负整数）。
_NUMERIC_MINIMUMS = {
    "maxTerminals": 1,
    "maxCols": 2,
    "maxRows": 1,
    "scrollback": 0,
    "maxBufferedBytes": 1024,
    "maxInputBytes": 1,
    "disposeGraceMs": 1,
}


class TerminalControlUnavailable(Exception):
    """输入/调整被拒但输出附加保持有效（types.ts:8，reason 判别）。

    @param reason - `read-only`（控制权在其它附加）或 `not-running`（退出/关闭中）。
    """

    code = ERROR_CONTROL_UNAVAILABLE

    def __init__(self, reason: str):
        if reason not in ("read-only", "not-running"):
            raise ValueError(f"unknown terminal control reason: {reason!r}")
        super().__init__("Terminal is not running" if reason == "not-running"
                         else "Terminal input is controlled by another attachment")
        self.details = {"reason": reason}


class TerminalLimitReached(Exception):
    """会话保留终端与待分配额度耗尽（types.ts:10）。"""

    code = ERROR_LIMIT_REACHED

    def __init__(self, limit: int):
        super().__init__("Session terminal limit reached")
        self.details = {"limit": limit}


class TerminalUnavailable(Exception):
    """终端身份缺失或已进入进程清理（types.ts:8，details 恒空）。"""

    code = ERROR_UNAVAILABLE

    def __init__(self, message: str = "Terminal is closing or unavailable"):
        super().__init__(message)
        self.details = {}


def is_identity(value: Any) -> bool:
    """`^[\\w-]{1,128}$`（WebTerminalId / TerminalAttachmentId 词法）。"""
    return isinstance(value, str) and IDENTITY_PATTERN.match(value) is not None


def decode_terminal_input(value: Any) -> dict:
    """`terminal/attach` 声明的上行 codec（types.ts:66-69 的 TerminalInput 判别联合）。

    上游该 codec 由 typert 从类型图生成，逐字校验判别字段与精确字段集；mini 以
    `web/uplink.py` 声明的 codec 等价承担：字段非法即抛，由 `UplinkItems` 折成
    `gateway/input-invalid` 中止整条逻辑流。`sequence` 的安全整数与连续编号检查
    在 `TerminalAttachment.submit`（对齐 terminal.ts:119 的 `Number.isSafeInteger`
    + gap 检查），此处只做 wire 形状与类型闭集校验。
    """
    if not isinstance(value, dict):
        raise ValueError("terminal input must be an object")
    kind = value.get("type")
    if not _is_integer(value.get("sequence")):
        raise ValueError("terminal input sequence must be an integer")
    if kind == "write":
        if set(value) != {"type", "sequence", "data"}:
            raise ValueError("terminal write input has unexpected fields")
        if not isinstance(value["data"], str):
            raise ValueError("terminal write data must be a string")
        return {"type": "write", "sequence": value["sequence"], "data": value["data"]}
    if kind == "resize":
        if set(value) != {"type", "sequence", "cols", "rows"}:
            raise ValueError("terminal resize input has unexpected fields")
        if not _is_integer(value["cols"]) or not _is_integer(value["rows"]):
            raise ValueError("terminal resize dimensions must be integers")
        return {"type": "resize", "sequence": value["sequence"],
                "cols": value["cols"], "rows": value["rows"]}
    raise ValueError(f"unknown terminal input type: {kind!r}")


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def frame_bytes(frame: dict) -> int:
    """`Buffer.byteLength(JSON.stringify(frame), 'utf8')` 等价（stream.ts:23）。

    JSON.stringify 无分隔空白；Python 缺省会加空格，故显式 separators。
    """
    return len(json.dumps(frame, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8"))


def _require_positive_int(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < _NUMERIC_MINIMUMS[name]:
        raise ValueError(
            f"terminal-controller: {name} must be an integer >= {_NUMERIC_MINIMUMS[name]}, "
            f"got {value!r}")


def _resolve_shell(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("terminal-controller: shell profile must be an object or absent")
    path = value.get("path")
    name = value.get("name")
    args = value.get("args", [])
    if not isinstance(path, str) or not path:
        raise ValueError("terminal-controller: shell.path must be a non-empty string")
    if not isinstance(name, str) or not name:
        raise ValueError("terminal-controller: shell.name must be a non-empty string")
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        raise ValueError("terminal-controller: shell.args must be an array of strings")
    return {"path": path, "name": name, "args": list(args)}


def resolve_config(config: dict | None = None) -> dict:
    """应用缺省并校验，返回全量 Config（index.ts:68-80 的显式 resolve 步骤）。"""
    resolved = dict(TERMINAL_CONFIG_DEFAULTS)
    for key, value in dict(config or {}).items():
        if value is not None:
            resolved[key] = value
    resolved["shell"] = _resolve_shell(resolved.get("shell"))
    candidates = resolved["shellCandidates"]
    if (not isinstance(candidates, list)
            or any(not isinstance(c, str) or not c for c in candidates)):
        raise ValueError("terminal-controller: shellCandidates must be an array of non-empty strings")
    resolved["shellCandidates"] = list(candidates)
    for name in _NUMERIC_MINIMUMS:
        _require_positive_int(name, resolved[name])
    return resolved
