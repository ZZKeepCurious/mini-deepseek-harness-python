"""web 测试：上行 inbox 有界队列（`web/uplink.py`，对齐 stream-server.ts UplinkInbox）。

覆盖：字节上限（`gateway/uplink-overflow`）、end 半关、end 后 item（`gateway/protocol`）、
单消费者、挂起读唯一、失败优先于缓冲、释放后丢帧、违例带 endpoint details；
以及方法侧 `UplinkItems` 的逐项 codec 解码（`gateway/input-invalid` field `uplink`）。
"""
import asyncio
import unittest

from miniharness.web.uplink import (
    DEFAULT_STREAM_INBOX_BYTES,
    UplinkDecodeError,
    UplinkInbox,
    UplinkItems,
    UplinkViolation,
)


def _inbox():
    return UplinkInbox(1024, "ns/e")


async def _drain(inbox):
    return [value async for value in inbox]


class UplinkInboxTest(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_default_inbox_bytes_matches_upstream_config(self):
        # 上游 gateway Config `streamInboxBytes` @default 262144（index.ts:137/203）
        self.assertEqual(DEFAULT_STREAM_INBOX_BYTES, 262_144)

    def test_push_then_read_in_order(self):
        async def go():
            inbox = _inbox()
            inbox.push("a", 10)
            inbox.push("b", 10)
            inbox.end()
            return await _drain(inbox)
        self.assertEqual(self._run(go()), ["a", "b"])

    def test_end_ends_the_read(self):
        async def go():
            inbox = _inbox()
            inbox.end()
            return await _drain(inbox)
        self.assertEqual(self._run(go()), [])

    def test_read_waits_for_a_push(self):
        async def go():
            inbox = _inbox()
            seen = []

            async def consume():
                async for value in inbox:
                    seen.append(value)

            task = asyncio.ensure_future(consume())
            await asyncio.sleep(0)
            inbox.push(1, 4)
            await asyncio.sleep(0)
            inbox.push(2, 4)
            inbox.end()
            await task
            return seen
        self.assertEqual(self._run(go()), [1, 2])

    def test_item_after_end_is_gateway_protocol(self):
        async def go():
            inbox = _inbox()
            inbox.end()
            return inbox.push("late", 8)
        violation = self._run(go())
        self.assertEqual(violation.code, "gateway/protocol")
        self.assertEqual(violation.details, {"endpoint": "ns/e"})

    def test_overflow_is_gateway_uplink_overflow(self):
        async def go():
            inbox = UplinkInbox(16, "ns/e")
            self.assertIsNone(inbox.push("12345", 10))
            return inbox.push("6789", 10)
        violation = self._run(go())
        self.assertEqual(violation.code, "gateway/uplink-overflow")
        self.assertEqual(violation.details, {"endpoint": "ns/e"})

    def test_frame_exactly_at_the_cap_is_accepted(self):
        async def go():
            inbox = UplinkInbox(10, "ns/e")
            self.assertIsNone(inbox.push("12345", 10))
            inbox.end()
            return await _drain(inbox)
        self.assertEqual(self._run(go()), ["12345"])

    def test_one_byte_over_the_cap_fails(self):
        async def go():
            return UplinkInbox(10, "ns/e").push("123456", 11)
        self.assertEqual(self._run(go()).code, "gateway/uplink-overflow")

    def test_multibyte_frames_count_utf8_bytes(self):
        # 上限按 UTF-8 字节计（上游 Buffer.byteLength(text,'utf8')）：6 字节的中文字
        async def go():
            inbox = UplinkInbox(6, "ns/e")
            self.assertIsNone(inbox.push("中文", 6))
            return inbox.push("x", 1)
        self.assertEqual(self._run(go()).code, "gateway/uplink-overflow")

    def test_violation_fails_the_pending_read(self):
        async def go():
            inbox = UplinkInbox(16, "ns/e")
            inbox.push("12345", 10)
            inbox.push("6789", 10)
            with self.assertRaises(UplinkViolation) as caught:
                await _drain(inbox)
            return caught.exception
        self.assertEqual(self._run(go()).code, "gateway/uplink-overflow")

    def test_violation_drops_buffered_items(self):
        async def go():
            inbox = UplinkInbox(16, "ns/e")
            inbox.push("12345", 10)
            inbox.push("6789", 10)
            with self.assertRaises(UplinkViolation):
                await _drain(inbox)
        self._run(go())

    def test_failure_drops_buffered_items(self):
        async def go():
            inbox = _inbox()
            inbox.push("a", 4)
            inbox.fail(RuntimeError("stream ended"))
            with self.assertRaises(RuntimeError):
                await _drain(inbox)
        self._run(go())

    def test_failure_is_first_wins(self):
        first = RuntimeError("first")

        async def go():
            inbox = _inbox()
            inbox.fail(first)
            inbox.fail(RuntimeError("second"))
            self.assertIsNone(inbox.push("late", 4))
            with self.assertRaises(RuntimeError) as caught:
                await _drain(inbox)
            return caught.exception
        self.assertIs(self._run(go()), first)

    def test_second_consumer_rejected(self):
        async def go():
            inbox = _inbox()
            inbox.__aiter__()
            with self.assertRaises(RuntimeError):
                inbox.__aiter__()
        self._run(go())

    def test_one_pending_read_only(self):
        async def go():
            inbox = _inbox()
            first = asyncio.ensure_future(inbox.__anext__())
            await asyncio.sleep(0)
            with self.assertRaises(RuntimeError):
                await inbox.__anext__()
            inbox.end()
            with self.assertRaises(StopAsyncIteration):
                await first
        self._run(go())

    def test_release_drops_buffer_and_later_items(self):
        async def go():
            inbox = _inbox()
            inbox.push("a", 4)
            inbox.release()
            self.assertIsNone(inbox.push("b", 4))
            inbox.end()
            self.assertIsNone(inbox.push("c", 4))
            return await _drain(inbox)
        self.assertEqual(self._run(go()), [])


def _items(inbox, codec=None):
    return UplinkItems(inbox, "ns/e", codec)


class UplinkItemsTest(unittest.TestCase):
    """方法侧解码迭代器（上游 `UplinkDecoder`，index.ts:1214-1285）。"""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_default_codec_accepts_lossless_json_items(self):
        async def go():
            inbox = _inbox()
            inbox.push({"seq": 1}, 12)
            inbox.push(None, 5)
            inbox.push([1, "two"], 12)
            inbox.end()
            return [value async for value in _items(inbox)]
        self.assertEqual(self._run(go()), [{"seq": 1}, None, [1, "two"]])

    def test_default_codec_rejects_items_json_cannot_carry(self):
        async def go():
            inbox = _inbox()
            inbox.push(float("nan"), 8)
            try:
                return [value async for value in _items(inbox)]
            except UplinkDecodeError as error:
                return error
        error = self._run(go())
        self.assertEqual(error.code, "gateway/input-invalid")
        self.assertEqual(
            error.message,
            'typert gateway: ns/e: wire field "uplink" failed boundary validation')
        self.assertEqual(error.details, {"endpoint": "ns/e", "field": "uplink"})

    def test_declared_codec_decodes_each_item(self):
        def codec(value):
            if not isinstance(value, dict) or not isinstance(value.get("n"), int):
                raise ValueError("n required")
            return {"n": value["n"], "doubled": value["n"] * 2}
        self.assertEqual(self._run(self._decoded(codec)),
                         [{"n": 1, "doubled": 2}, {"n": 2, "doubled": 4}])

    async def _decoded(self, codec):
        inbox = _inbox()
        inbox.push({"n": 1}, 8)
        inbox.push({"n": 2}, 8)
        inbox.end()
        return [value async for value in _items(inbox, codec)]

    def test_declared_codec_rejection_fails_the_stream(self):
        def codec(value):
            if value != "ok":
                raise ValueError("not ok")
            return value
        error = self._run(self._rejected(codec))
        self.assertEqual(error.code, "gateway/input-invalid")
        self.assertEqual(error.details, {"endpoint": "ns/e", "field": "uplink"})
        self.assertIsInstance(error.__cause__, ValueError)

    async def _rejected(self, codec):
        inbox = _inbox()
        inbox.push("bad", 5)
        try:
            return [value async for value in _items(inbox, codec)]
        except UplinkDecodeError as error:
            return error

    def test_decoded_value_must_stay_lossless_json(self):
        def codec(value):
            return {value: object()}
        async def go():
            inbox = _inbox()
            inbox.push("k", 4)
            with self.assertRaises(UplinkDecodeError):
                return [value async for value in _items(inbox, codec)]
        self._run(go())

    def test_decode_failure_wakes_a_pending_read(self):
        def codec(value):
            raise ValueError("no")
        async def go():
            inbox = _inbox()
            items = _items(inbox, codec)
            read = asyncio.ensure_future(items.__anext__())
            await asyncio.sleep(0)
            inbox.push("x", 4)
            with self.assertRaises(UplinkDecodeError):
                await read
            # 失败同时中止本流：之后的读不再等新项。
            with self.assertRaises(UplinkDecodeError):
                await items.__anext__()
        self._run(go())

    def test_one_consumer_per_call(self):
        async def go():
            inbox = _inbox()
            items = _items(inbox)
            items.__aiter__()
            with self.assertRaises(RuntimeError) as caught:
                items.__aiter__()
            return str(caught.exception)
        self.assertIn("invocation.uplink() is available once per call",
                      self._run(go()))

    def test_close_releases_the_source(self):
        async def go():
            inbox = _inbox()
            items = _items(inbox)
            inbox.push("a", 4)
            items.close()
            self.assertIsNone(inbox.push("b", 4))
            return [value async for value in items]
        self.assertEqual(self._run(go()), [])

    def test_client_half_close_ends_the_iteration(self):
        async def go():
            inbox = _inbox()
            inbox.push("a", 4)
            inbox.end()
            return [value async for value in _items(inbox)]
        self.assertEqual(self._run(go()), ["a"])


if __name__ == "__main__":
    unittest.main()
