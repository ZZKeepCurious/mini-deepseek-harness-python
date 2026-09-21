"""流式终端控制字符 sanitizer（对齐 upstream terminal-bash/src/sanitize.ts）。

删除 CSI / OSC / 两字节短转义序列，同时保留跨数据块的分片挂起状态；OSC 内容
恰为 `133;D;` 前缀时回报 prompt 完成并跟踪其后的可打印尾巴促文。完整终端仿真
（@xterm/headless）刻意延后（sanitize.ts:20-23 明示），line-oriented 首版只保
证普通行输出与私人 prompt marker 契约。promptTail 按分块交集累计（session.ts
onData 会接着按 CONTROLLED_PROMPT 长度合订）。maxPendingBytes 以 UTF-8 字节数
计（对齐 Buffer.byteLength 语义）。
"""

from __future__ import annotations

from .types import _utf8_bytes

#: sanitize.ts:6 PROMPT_MARKER_PREFIX
PROMPT_MARKER_PREFIX = "133;D;"
#: sanitize.ts:9 CONTROLLED_PROMPT
CONTROLLED_PROMPT = "dsh> "

__all__ = ["CONTROLLED_PROMPT", "PROMPT_MARKER_PREFIX", "TerminalSanitizer", "normalize_terminal_text"]


class TerminalSanitizer:
    """流式剥 CSI/OSC/短转义；分片安全，超限进入 discard 模式跨块丢。"""

    def __init__(self, max_pending_bytes: int):
        self.max_pending_bytes = max_pending_bytes
        self._pending = ""
        self._discard_mode: str | None = None  # 'osc' | 'csi' | None
        self._discard_osc_escape = False
        self._trailing_carriage_return = False
        self._tracking_prompt_tail = False

    def push(self, chunk: str) -> dict:
        """消费一个已解码的数据块，返回 {text, prompt, promptTail?}。"""
        self._pending += self._discard_prefix(chunk)
        text = ""
        prompt = False
        include_prompt_tail = self._tracking_prompt_tail
        prompt_tail = ""
        index = 0

        def append_text(value: str) -> None:
            nonlocal text, prompt_tail
            text += value
            if self._tracking_prompt_tail:
                prompt_tail += value

        while index < len(self._pending):
            escape = self._pending.find("\x1b", index)
            if escape < 0:
                append_text(self._pending[index:])
                index = len(self._pending)
                break
            append_text(self._pending[index:escape])
            if escape + 1 >= len(self._pending):
                index = escape
                break
            kind = self._pending[escape + 1]
            if kind == "]":
                bel = self._pending.find("\x07", escape + 2)
                string_terminator = self._pending.find("\x1b\\", escape + 2)
                end = -1
                if bel >= 0 and string_terminator >= 0:
                    end = min(bel + 1, string_terminator + 2)
                elif bel >= 0:
                    end = bel + 1
                elif string_terminator >= 0:
                    end = string_terminator + 2
                if end < 0:
                    index = escape
                    break
                terminator_bytes = 1 if self._pending[end - 1] == "\x07" else 2
                content = self._pending[escape + 2:end - terminator_bytes]
                if content.startswith(PROMPT_MARKER_PREFIX):
                    prompt = True
                    self._tracking_prompt_tail = True
                    include_prompt_tail = True
                    prompt_tail = ""
                index = end
                continue
            if kind == "[":
                end_index = escape + 2
                while end_index < len(self._pending):
                    code = ord(self._pending[end_index])
                    if 0x40 <= code <= 0x7E:
                        break
                    end_index += 1
                if end_index >= len(self._pending):
                    index = escape
                    break
                index = end_index + 1
                continue
            # 两字节转义族（save/restore cursor 等）。
            index = escape + 2
        self._pending = self._pending[index:]
        self._enforce_pending_bound()
        result = {"text": self._normalize_text(text), "prompt": prompt}
        if include_prompt_tail:
            result["promptTail"] = prompt_tail
        return result

    def flush(self) -> str:
        """PTY 退出时刷出尾部可打印碎片；不完整转义序列被丢弃。"""
        text = "" if self._pending.startswith("\x1b") else self._pending
        self._pending = ""
        self._discard_mode = None
        self._discard_osc_escape = False
        self._tracking_prompt_tail = False
        normalized = self._normalize_text(text)
        if not self._trailing_carriage_return:
            return normalized
        self._trailing_carriage_return = False
        return f"{normalized}\n"

    def _normalize_text(self, text: str) -> str:
        complete = f"\r{text}" if self._trailing_carriage_return else text
        self._trailing_carriage_return = False
        if complete.endswith("\r"):
            complete = complete[:-1]
            self._trailing_carriage_return = True
        return normalize_terminal_text(complete)

    def _enforce_pending_bound(self) -> None:
        if _utf8_bytes(self._pending) <= self.max_pending_bytes:
            return
        self._discard_mode = "osc" if self._pending[1] == "]" else "csi"
        self._pending = ""

    def _discard_prefix(self, chunk: str) -> str:
        if self._discard_mode is None:
            return chunk
        if self._discard_mode == "csi":
            for length in range(len(chunk)):
                code = ord(chunk[length])
                if 0x40 <= code <= 0x7E:
                    self._discard_mode = None
                    return chunk[length + 1:]
            return ""

        length = 0
        if self._discard_osc_escape:
            self._discard_osc_escape = False
            if chunk.startswith("\\"):
                self._discard_mode = None
                return chunk[1:]
        while length < len(chunk):
            if chunk[length] == "\x07":
                self._discard_mode = None
                return chunk[length + 1:]
            if chunk[length] == "\x1b":
                if length + 1 < len(chunk) and chunk[length + 1] == "\\":
                    self._discard_mode = None
                    return chunk[length + 2:]
                if length + 1 == len(chunk):
                    self._discard_osc_escape = True
            length += 1
        return ""


def normalize_terminal_text(text: str) -> str:
    """CRLF 与孤立 CR 归一为 LF、删除 BEL（对齐 sanitize.ts:186-188）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x07", "")