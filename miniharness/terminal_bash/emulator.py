"""终端协议仿真器（session.ts:284-305 HeadlessTerminal 的等价面）。

定位：上游 LocalPtySession 只把仿真终端用作「协议应答器」——onData 吃进原始
字节块，逐 query 回写应答（DA1/DA2/CPR/DECRQM），返回文本由 sanitizer + 有界
缓冲独占（session.ts:241 明示）。本实现用 pyte Screen/Stream 解析并跟踪光标，
查询扫描器同步产出应答串；screen 状态不进返回面。

夹具值（已登记差异）：上游 @xterm/headless 的内置应答随依赖演进，mini 固定
稳定夹具并以此为 wire 契约——DA1 `ESC [?1;2c`、DA2 `ESC [>0;10;0c`、CPR
`ESC [{row};{col}R`、DECRQM `ESC [?{ps};2$y`（mode 2=reset）。
"""

from __future__ import annotations

from pyte import Screen, Stream

__all__ = ["TerminalProtocolEmulator"]

#: `\x1b[6n` 之外的 CPR 参数不答（夹具面仅标准 form）。
_CPR_PARAM = "6"


class TerminalProtocolEmulator:
    """流式 query 应答器：吃原始输出字节流，回写应答串。"""

    def __init__(self, cols: int, rows: int):
        self._screen = Screen(cols, rows)
        self._stream = Stream(self._screen)
        self._tail = ""
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True
        self._tail = ""

    def resize(self, cols: int, rows: int) -> None:
        if self._closed:
            return
        self._screen.resize(cols, rows)

    def feed(self, text: str) -> str:
        """消费输出文本；回写格式化的应答串（多条顺序拼接）。

        拆块安全：跨块挂起的不完整序列保留在 tail，下块补齐即答。

        载体差异（已登记）：pyte 0.8.2 的 Stream 对带 `?` 私有前缀的 CSI 部分
        崩溃（report_device_status/attributes 不接受 private kwarg）；因此 `?`
        私有序列不喂给 pyte（不影响渲染与本实现回答的查询，真实受控 shell 不
        依赖私有模式光标跟踪），应答逻辑在扫描层独立完成。
        """
        if self._closed:
            return ""
        self._tail += text
        replies: list[str] = []
        clean = []
        tail = self._tail
        cursor = 0
        if "\x1b" not in tail:
            self._tail = ""
            self._stream.feed(tail)
            return ""
        while True:
            start = tail.find("\x1b", cursor)
            if start < 0:
                clean.append(tail[cursor:])
                break
            clean.append(tail[cursor:start])
            if start + 1 >= len(tail):
                self._tail = tail[start:]
                break
            if tail[start + 1] != "[":
                clean.append(tail[start:start + 2])
                cursor = start + 2
                continue
            index = start + 2
            while index < len(tail):
                code = ord(tail[index])
                if 0x40 <= code <= 0x7E:
                    break
                index += 1
            if index >= len(tail):
                self._tail = tail[start:]
                break
            params = tail[start + 2:index]
            final = tail[index]
            if params.startswith("?"):
                reply = self._answer(params, final)
                if reply is not None:
                    replies.append(reply)
            elif final == "n" and params == _CPR_PARAM:
                row = self._screen.cursor.y + 1
                col = self._screen.cursor.x + 1
                replies.append(f"\x1b[{row};{col}R")
                clean.append(tail[start:index + 1])
            elif final == "c":
                reply = self._answer(params, final)
                if reply is not None:
                    replies.append(reply)
                clean.append(tail[start:index + 1])
            else:
                clean.append(tail[start:index + 1])
            cursor = index + 1
        remainder = tail[cursor:]
        self._tail = remainder if remainder.startswith("\x1b") else ""
        cleaned = "".join(clean)
        if cleaned:
            self._stream.feed(cleaned)
        return "".join(replies)

    @staticmethod
    def _answer(params: str, final: str) -> str | None:
        if final == "c":
            if params.startswith(">"):
                return "\x1b[>0;10;0c"
            return "\x1b[?1;2c"
        if final == "n":
            if params.startswith("?"):
                return f"\x1b[?{params[1:]};2$y"
            return None
        return None