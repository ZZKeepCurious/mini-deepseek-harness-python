"""BoundedTextBuffer / read_scrollback / LocalSendOperation 确定性面。

对应 terminal-bash/tests/session-buffer.spec.ts 中不依赖 LocalPtySession 载体
（TextDecoder/PassThrough）的确定性契约，逐条移植；ReferenceBuffer 复刻上游
测试内嵌 reference（naive tail），逐块比对保证字节账目等价。TextDecoder 分片
解码面属 P2 会话载体差异。
"""

import unittest

from miniharness.terminal.bounded_buffer import BoundedTextBuffer, read_scrollback
from miniharness.terminal.operation import LocalSendOperation

LIMIT_4M = 4 * 1024 * 1024


def _units(text):
    """UTF-16 语义单位：代理对 1 单位（对齐 JS Array.from / charCodeAt 面）。"""
    index = 0
    size = len(text)
    while index < size:
        code = ord(text[index])
        if 0xD800 <= code <= 0xDBFF and index + 1 < size and 0xDC00 <= ord(text[index + 1]) <= 0xDFFF:
            yield text[index:index + 2]
            index += 2
        else:
            yield text[index]
            index += 1


def _bytes(text):
    """Node Buffer.byteLength utf8 等价面（代理对 4 字节 / 孤立代理 3 字节）。"""
    total = 0
    for unit in _units(text):
        if len(unit) == 2:
            total += 4
        else:
            code = ord(unit)
            total += 1 if code < 0x80 else 2 if code < 0x800 else 3 if code < 0x10000 else 4
    return total


def reference_tail(text, max_bytes):
    if _bytes(text) <= max_bytes:
        return {"text": text, "truncated": False}
    # Node 尾部淘汰从头部逐单元出队（session.ts:105-118）：代理对整体扣 4 字节
    # 出 2 单元，单个按字节表扣 1 单元；一旦剩余字节 ≤ max 立即停。逐单元复刻。
    total = _bytes(text)
    index = 0
    size = len(text)
    while index < size and total > max_bytes:
        head = text[index]
        second = text[index + 1] if index + 1 < size else None
        paired = 0xD800 <= ord(head) <= 0xDBFF and second is not None and 0xDC00 <= ord(second) <= 0xDFFF
        if paired:
            total -= 4
            index += 2
        else:
            code = ord(head)
            if code < 0x80:
                total -= 1
            elif code < 0x800:
                total -= 2
            elif code < 0x10000:
                total -= 3
            else:
                total -= 4
            index += 1
    return {"text": text[index:], "truncated": True}


class ReferenceBuffer:
    def __init__(self, max_bytes, max_lines=None):
        self.max_bytes = max_bytes
        self.max_lines = max_lines
        self.text = ""
        self.truncated = False

    def append(self, chunk):
        if len(chunk) == 0:
            return
        self.text += chunk
        if self.max_lines is not None:
            lines = self.text.split("\n")
            if len(lines) > self.max_lines:
                self.text = "\n".join(lines[-self.max_lines:])
                self.truncated = True
        bounded = reference_tail(self.text, self.max_bytes)
        self.text = bounded["text"]
        self.truncated = self.truncated or bounded["truncated"]

    def snapshot(self):
        return {"text": self.text, "truncated": self.truncated}

    def consume(self):
        result = {"delta": self.text, "truncated": self.truncated}
        self.text = ""
        self.truncated = False
        return result


def reference_read(buffer, max_bytes, request=None):
    request = dict(request or {})
    snapshot = buffer.snapshot()
    lines = snapshot["text"].split("\n")
    total_lines = 0 if len(snapshot["text"]) == 0 else len(lines)
    offset = request.get("offset", 0)
    if offset >= total_lines:
        return {"text": "", "totalLines": total_lines, "lineBegin": offset,
                "lineEnd": offset, "truncated": snapshot["truncated"]}
    end = total_lines - offset
    bounded = reference_tail("\n".join(lines[max(0, end - request.get("count", 500)):end]),
                             max_bytes)
    returned_lines = 0 if len(bounded["text"]) == 0 else len(bounded["text"].split("\n"))
    return {
        "text": bounded["text"], "totalLines": total_lines,
        "lineBegin": offset, "lineEnd": offset + returned_lines,
        "truncated": snapshot["truncated"] or bounded["truncated"],
    }


def deterministic_chunks(alphabet, count):
    state = 0x12345678
    chunks = []
    for _ in range(count):
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        chunks.append(alphabet[state % len(alphabet)])
    return chunks


class TestBoundedBufferTail(unittest.TestCase):
    def test_retains_exact_tail_across_limit_while_consuming_active_output(self):
        limit = LIMIT_4M
        scrollback = BoundedTextBuffer(limit, 10_000)
        operation = LocalSendOperation(limit, 0, lambda: None)
        chunk = "a" * 4096
        for _ in range(limit // len(chunk)):
            scrollback.append(chunk)
            operation.append(chunk)
        self.assertEqual(read_scrollback(scrollback.snapshot(), limit, {}), {
            "text": "a" * limit, "totalLines": 1, "lineBegin": 0, "lineEnd": 1,
            "truncated": False,
        })

        scrollback.append("界😀TAIL")
        operation.append("界😀TAIL")
        retained = f"{'a' * (limit - 11)}界😀TAIL"
        self.assertEqual(read_scrollback(scrollback.snapshot(), limit, {}), {
            "text": retained, "totalLines": 1, "lineBegin": 0, "lineEnd": 1,
            "truncated": True,
        })
        self.assertEqual(operation.read_output(), {"delta": retained, "truncated": True})
        self.assertEqual(operation.read_output(), {"delta": "", "truncated": False})
        operation.append("é")
        self.assertEqual(operation.read_output(), {"delta": "é", "truncated": False})
        operation.append("done")
        operation.settle("session_exit", {"kind": "exited", "exitCode": 0, "signal": None}, True)
        self.assertEqual(operation.done, {
            "viewport": "done", "waitReason": "session_exit",
            "sessionStatus": {"kind": "exited", "exitCode": 0, "signal": None},
            "truncated": True,
        })
        self.assertEqual(operation.read_output(), {"delta": "done", "truncated": False})
        self.assertEqual(operation.read_output(), {"delta": "", "truncated": False})

    def test_counts_trailing_empty_lines_and_keeps_truncation_sticky(self):
        scrollback = BoundedTextBuffer(64, 3)
        reference = ReferenceBuffer(64, 3)
        for chunk in ("a\nb\n", "\n"):
            scrollback.append(chunk)
            reference.append(chunk)
        self.assertEqual(read_scrollback(scrollback.snapshot(), 64, {}),
                         {"text": "b\n\n", "totalLines": 3, "lineBegin": 0,
                          "lineEnd": 3, "truncated": True})
        self.assertEqual(read_scrollback(scrollback.snapshot(), 64, {"count": 1}),
                         {"text": "", "totalLines": 3, "lineBegin": 0,
                          "lineEnd": 0, "truncated": True})
        self.assertEqual(read_scrollback(scrollback.snapshot(), 64, {"offset": 2, "count": 1}),
                         {"text": "b", "totalLines": 3, "lineBegin": 2,
                          "lineEnd": 3, "truncated": True})
        self.assertTrue(read_scrollback(scrollback.snapshot(), 64, {"offset": 3})["truncated"])
        for chunk in ("", "c"):
            scrollback.append(chunk)
            reference.append(chunk)
        self.assertEqual(read_scrollback(scrollback.snapshot(), 64, {})["text"], "b\n\nc")
        self.assertTrue(read_scrollback(scrollback.snapshot(), 64, {})["truncated"])


class TestBoundedBufferUtf8(unittest.TestCase):
    def test_bounds_oversized_multibyte_chunk(self):
        scrollback = BoundedTextBuffer(11, 10_000)
        operation = LocalSendOperation(11, 0, lambda: None)
        for chunk in ("abc", "界😀éz"):
            scrollback.append(chunk)
            operation.append(chunk)
        self.assertEqual(read_scrollback(scrollback.snapshot(), 11, {})["text"], "c界😀éz")
        self.assertEqual(operation.read_output(), {"delta": "c界😀éz", "truncated": True})
        scrollback.append("界😀" * 1000 + "éEND")
        operation.append("界😀" * 1000 + "éEND")
        self.assertEqual(read_scrollback(scrollback.snapshot(), 11, {})["text"], "😀éEND")
        self.assertEqual(operation.read_output(), {"delta": "😀éEND", "truncated": True})
        self.assertEqual(operation.read_output(), {"delta": "", "truncated": False})
        operation.append("ok")
        operation.settle("session_exit",
                         {"kind": "exited", "exitCode": 0, "signal": None}, True)
        self.assertEqual(operation.done["viewport"], "ok")

    def test_resets_operation_truncation_independently_of_retained_scrollback(self):
        scrollback = BoundedTextBuffer(128, 10_000)
        operation = LocalSendOperation(5, 0, lambda: None)
        for index in range(4):
            scrollback.append("123456")
            operation.append("123456")
            self.assertEqual(operation.read_output(), {"delta": "23456", "truncated": True})
            self.assertEqual(operation.read_output(), {"delta": "", "truncated": False})
            scrollback.append("é")
            operation.append("é")
            self.assertEqual(operation.read_output(), {"delta": "é", "truncated": False})
        self.assertEqual(operation.read_output(), {"delta": "", "truncated": False})
        operation.settle("session_exit", {"kind": "running"}, False)
        self.assertEqual(operation.done["viewport"], "")
        self.assertFalse(operation.done["truncated"])
        self.assertEqual(operation.read_output(), {"delta": "", "truncated": False})

    def test_matches_reference_over_deterministic_chunks(self):
        for scrollback_max, read_max, lines in ((1, 1, 1), (17, 7, 3), (64, 13, 5)):
            scrollback = BoundedTextBuffer(scrollback_max, lines)
            operation = LocalSendOperation(read_max, 0, lambda: None)
            reference = ReferenceBuffer(scrollback_max, lines)
            ref_output = ReferenceBuffer(read_max)
            for index, chunk in enumerate(
                    deterministic_chunks(["a", "bc", "\n", "\n\n", "界", "😀", "éz",
                                          "", "long line\nend\n"], 120)):
                scrollback.append(chunk)
                operation.append(chunk)
                reference.append(chunk)
                ref_output.append(chunk)
                for request in ({}, {"offset": 1, "count": 2}, {"offset": 9, "count": 1}):
                    self.assertEqual(read_scrollback(scrollback.snapshot(), read_max, request),
                                     reference_read(reference, read_max, request))
                if index % 7 == 0:
                    self.assertEqual(operation.read_output(), ref_output.consume())

    def test_retains_tiny_chunks_through_coalesced_node_rollovers(self):
        limit = 5000
        scrollback = BoundedTextBuffer(limit, 10_000)
        operation = LocalSendOperation(limit, 0, lambda: None)
        text = "0123456789" * 1000
        checkpoints = {1, 4096, 4097, 5000, 5001, 8193, len(text)}
        for index, char in enumerate(text):
            scrollback.append(char)
            operation.append(char)
            if index + 1 in checkpoints:
                self.assertEqual(read_scrollback(scrollback.snapshot(), limit, {}), {
                    "text": text[max(0, index + 1 - limit):index + 1],
                    "totalLines": 1, "lineBegin": 0, "lineEnd": 1,
                    "truncated": index + 1 > limit,
                })
        self.assertEqual(operation.read_output(), {"delta": text[-limit:], "truncated": True})
        self.assertEqual(operation.read_output(), {"delta": "", "truncated": False})
        operation.append("fresh")
        operation.settle("session_exit", {"kind": "running"}, True)
        self.assertEqual(operation.done["viewport"], "fresh")
        self.assertTrue(operation.done["truncated"])


class TestBoundedBufferSurrogates(unittest.TestCase):
    def test_preserves_split_surrogate_pairs_when_coalesced_copy_reaches_eviction_head(self):
        limit = 4100
        buffer = BoundedTextBuffer(limit, 10_000)
        reference = ReferenceBuffer(limit, 10_000)
        chunks = ["H", "a" * 4095, "\ud83d", "\ude00", "xy", "b" * 4094, "z", "\ud800", "\udfff"]
        for chunk in chunks:
            buffer.append(chunk)
            reference.append(chunk)
            self.assertEqual(buffer.snapshot(), reference.snapshot())
        self.assertEqual(buffer.snapshot(),
                         {"text": f"y{'b' * 4094}z\ud800\udfff", "truncated": True})
        self.assertEqual(buffer.consume(), reference.consume())
        self.assertEqual(buffer.consume(), reference.consume())

    def test_preserves_lone_and_split_surrogates_with_small_byte_limits(self):
        for max_bytes in (1, 2, 3, 4, 5, 8, 17):
            buffer = BoundedTextBuffer(max_bytes, 3)
            reference = ReferenceBuffer(max_bytes, 3)
            chunks = [
                "\ud83d", "\ude00", "x", "\ud83d", "", "\ude00", "\n", "\ud800", "abc", "\udfff",
                "prefix" * 20 + "\ud800", "\udfff", "\n\n\n",
                *deterministic_chunks(["a", "\ud800", "\udfff", "\ud83d\ude00", "\n", "é", "界",
                                       "", "\n\n"], 150),
            ]
            for index, chunk in enumerate(chunks):
                buffer.append(chunk)
                reference.append(chunk)
                self.assertEqual(buffer.snapshot(), reference.snapshot())
                if index % 19 == 18:
                    self.assertEqual(buffer.consume(), reference.consume())
                    self.assertEqual(buffer.consume(), reference.consume())
            self.assertEqual(buffer.consume(), reference.consume())


class TestReadValidation(unittest.TestCase):
    def test_rejects_invalid_offsets_and_counts(self):
        scrollback = BoundedTextBuffer(64)
        scrollback.append("a\nb\n")
        snapshot = scrollback.snapshot()
        with self.assertRaisesRegex(RuntimeError, "offset"):
            read_scrollback(snapshot, 64, {"offset": -1})
        with self.assertRaisesRegex(RuntimeError, "offset"):
            read_scrollback(snapshot, 64, {"offset": "1"})
        with self.assertRaisesRegex(RuntimeError, "count"):
            read_scrollback(snapshot, 64, {"count": 0})
        with self.assertRaisesRegex(RuntimeError, "count"):
            read_scrollback(snapshot, 64, {"count": -3})

    def test_pagination_beyond_retained_lines(self):
        scrollback = BoundedTextBuffer(64, 3)
        for chunk in ("a\nb\n", "\n"):
            scrollback.append(chunk)
        result = read_scrollback(scrollback.snapshot(), 64, {"offset": 3, "count": 1})
        self.assertEqual(result, {"text": "", "totalLines": 3, "lineBegin": 3,
                                  "lineEnd": 3, "truncated": True})


if __name__ == "__main__":
    unittest.main()