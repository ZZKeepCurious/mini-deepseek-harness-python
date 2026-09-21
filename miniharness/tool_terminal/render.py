"""terminal 六工具的模型渲染面（对齐 packages/terminal/tool-terminal/src/render.ts 逐字）。

* `boundTerminalText`：完整结果上限封口（TextRetainer head 语义，保 UTF-8 边界，
  `\n[output truncated]` 标记）。
* `renderSpawn/renderSend/renderSendRead/renderRead/renderList`：各工具的模型可见
  文本；spawn/read/list/send 在字节超限时保后缀元数据（`[wait: ...]`/`[lines: ...]`/
  状态行）与截断标记，内容部分按 TextRetainer tail 语义截断——对齐上游
  `boundBodyWithSuffix` 的 metadata 优先策略。
* 纯函数、无状态：最大字节上限逐调用传入（对齐 render.spec.ts 测试面）。
"""
from __future__ import annotations

__all__ = [
    "TRUNCATED",
    "bound_terminal_text",
    "render_list",
    "render_read",
    "render_send",
    "render_send_read",
    "render_spawn",
]

#: 截断标记（render.ts:50 TRUNCATED，逐字）。
TRUNCATED = "\n[output truncated]"


def _byte_length(text: str) -> int:
    """UTF-8 字节数（对齐 render.ts byteLength → TextEncoder byteLength）。"""
    return len(text.encode("utf-8"))


def _retain_head(text: str, max_bytes: int) -> str:
    """保留前 max_bytes 字节，不在多字节字符中间劈裂（TextRetainer head）。"""
    if _byte_length(text) <= max_bytes:
        return text
    out: list[str] = []
    size = 0
    for ch in text:
        width = len(ch.encode("utf-8"))
        if size + width > max_bytes:
            break
        size += width
        out.append(ch)
    return "".join(out)


def _retain_tail(text: str, max_bytes: int) -> str:
    """保留后 max_bytes 字节（TextRetainer tail，等价作业家族 fitWithSuffix）。"""
    if _byte_length(text) <= max_bytes:
        return text
    out: list[str] = []
    size = 0
    for ch in reversed(text):
        width = len(ch.encode("utf-8"))
        if size + width > max_bytes:
            break
        size += width
        out.append(ch)
    return "".join(reversed(out))


def _fit_with_suffix(content: str, suffix: str, max_bytes: int) -> str:
    """完整超限时内容按 tail 截断 + 固定后缀（render.ts:62-66）。"""
    fixed_bytes = _byte_length(suffix)
    if fixed_bytes >= max_bytes:
        return _retain_tail(suffix, max_bytes)
    return f"{_retain_tail(content, max_bytes - fixed_bytes)}{suffix}"


def _fit_with_prefix(prefix: str, content: str, max_bytes: int) -> str:
    """完整超限时前缀保头 + 内容 tail 截断 + 截断标记（render.ts:68-73）。"""
    fixed = f"{prefix}{TRUNCATED}"
    fixed_bytes = _byte_length(fixed)
    if fixed_bytes >= max_bytes:
        return _retain_head(fixed, max_bytes)
    return f"{prefix}{_retain_tail(content, max_bytes - fixed_bytes)}{TRUNCATED}"


def _bound_body_with_suffix(content: str, metadata: str, upstream_truncated: bool,
                            max_bytes: int) -> str:
    """body + 元数据 +（上游已截断标记）的整结果封口（render.ts:75-85）。"""
    suffix = f"{metadata}{TRUNCATED if upstream_truncated else ''}"
    complete = f"{content}{suffix}"
    if _byte_length(complete) <= max_bytes:
        return complete
    return _fit_with_suffix(content, f"{metadata}{TRUNCATED}", max_bytes)


def bound_terminal_text(text: str, max_bytes: int) -> str:
    """一条完整终端确认文本按 head 封口（render.ts:93-98 boundTerminalText）。"""
    if _byte_length(text) <= max_bytes:
        return text
    marker_bytes = _byte_length(TRUNCATED)
    if marker_bytes >= max_bytes:
        return _retain_tail(TRUNCATED, max_bytes)
    return f"{_retain_head(text, max_bytes - marker_bytes)}{TRUNCATED}"


def _status_text(status: dict) -> str:
    """会话状态行的 `running` / `exited code=.. signal=..` 面（nullish 用 'null'）。"""
    if status.get("kind") != "running":
        exit_code = status.get("exitCode")
        signal = status.get("signal")
        return f"exited code={'null' if exit_code is None else exit_code} " \
               f"signal={'null' if signal is None else signal}"
    return "running"


def render_spawn(result: dict, max_bytes: int) -> str:
    """一条已发布会话的创建确认（render.ts:106-112 renderSpawn）。"""
    label = result["sessionId"] if result.get("name") is None else f"{result['sessionId']} ({result['name']})"
    prefix = f"started terminal session {label} [type: {result['type']}]\n"
    motd = result.get("motd") or "(no startup output)"
    complete = f"{prefix}{motd}"
    if _byte_length(complete) <= max_bytes:
        return complete
    return _fit_with_prefix(prefix, motd, max_bytes)


def render_send(result: dict, max_bytes: int) -> str:
    """一次已结算交互式 send（render.ts:120-131 renderSend）。"""
    output = result["viewport"] or "(no new output)"
    status = _status_text(result["sessionStatus"])
    return _bound_body_with_suffix(
        output,
        f"\n[wait: {result['waitReason']}]\n[session: {status}]",
        result["truncated"],
        max_bytes,
    )


def render_send_read(read: dict) -> str:
    """一次后台增量读（render.ts:139-142 renderSendRead）；任务层随后追加状态行。"""
    delta = read["delta"]
    separator = "" if delta.endswith("\n") or len(delta) == 0 else "\n"
    return f"{delta}{separator}[output truncated]" if read["truncated"] else delta


def render_read(result: dict, max_bytes: int) -> str:
    """一页保留滚动记录（render.ts:150-158 renderRead）。"""
    output = result["text"] or "(no retained output)"
    return _bound_body_with_suffix(
        output,
        f"\n[lines: {result['lineBegin']}-{result['lineEnd']} of {result['totalLines']}]",
        result["truncated"],
        max_bytes,
    )


def render_list(sessions: list[dict], max_bytes: int) -> str:
    """owner 可见活会话一行一个（render.ts:166-177 renderList）。"""
    if not sessions:
        return "(no terminal sessions)"
    lines = []
    for session in sessions:
        name = "" if session.get("name") is None else f" ({session['name']})"
        pid = "" if session.get("pid") is None else f" pid={session['pid']}"
        status = _status_text(session["status"])
        lines.append(f"{session['sessionId']}{name} [{session['type']}] {status}{pid}")
    return _bound_body_with_suffix("\n".join(lines), "", False, max_bytes)