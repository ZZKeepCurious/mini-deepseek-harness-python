"""DeepSeek SSE 解析契约（spec-strict，上游 sse.ts:7-9）+ httpx 传输契约。

解析：事件只在空行终结时派发、EOF 未终止尾部 = 截断（STREAM_CLOSED）、
multi-data join、畸形载荷 MALFORMED_RESPONSE、abort 覆盖截断判定。
传输：经 httpx.MockTransport 注入，覆盖 HTTP 错误映射 / facts / 超时。
"""
import asyncio
import json
import unittest

import httpx

from miniharness.llm import DeepSeekAdapter, LlmFailure
from miniharness.llm.protocol import StreamAborted

BODY = {'model': 'deepseek-chat', 'messages': [{'role': 'user', 'content': 'hi'}]}


async def _alines(lines):
    # 模拟 httpx aiter_lines：按行产出且剥离行尾换行
    for line in lines:
        yield line.rstrip('\r\n')


async def _parse(lines, abort_event=None):
    adapter = DeepSeekAdapter(api_key='sk-test')
    return [c async for c in adapter._parse_sse(_alines(lines), abort_event)]


def _run(lines):
    return asyncio.run(_parse(lines))


def _chunk(text='ok', finish=None):
    return 'data: ' + json.dumps(
        {'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': finish}]}) + '\n'


DONE = 'data: [DONE]\n'


class SseParsingTest(unittest.TestCase):
    def test_terminated_events_dispatch(self):
        out = _run([_chunk(), '', DONE, ''])
        self.assertEqual([c['type'] for c in out],
                         ['block-start', 'text-delta', 'block-end', 'finish'])

    def test_unterminated_tail_is_truncation(self):
        # spec-strict（上游 sse.ts:7-9）：事件只在空行终结时派发，
        # EOF 处的未终止尾部是截断 → 缺 [DONE] → STREAM_CLOSED
        with self.assertRaises(LlmFailure) as cm:
            _run([_chunk(), '', _chunk().rstrip('\n')])
        self.assertEqual(cm.exception.code, 'STREAM_CLOSED')

    def test_unterminated_done_is_truncation(self):
        # 未终止的 [DONE] 同样不派发 → STREAM_CLOSED
        with self.assertRaises(LlmFailure) as cm:
            _run([_chunk(), '', 'data: [DONE]'])
        self.assertEqual(cm.exception.code, 'STREAM_CLOSED')

    def test_multi_data_join(self):
        # 同一事件的多个 data: 行以 \n 连接（eventsource-parser multi-data join）
        out = _run(['data: {"choices":[{"index":0,\n',
                    'data: "delta":{"content":"split"}}]}\n',
                    '\n', DONE, ''])
        self.assertEqual([c['type'] for c in out],
                         ['block-start', 'text-delta', 'block-end', 'finish'])
        self.assertEqual(out[1]['text'], 'split')

    def test_malformed_payload_fails_loud(self):
        with self.assertRaises(LlmFailure) as cm:
            _run(['data: {not json\n', '\n', DONE, ''])
        self.assertEqual(cm.exception.code, 'MALFORMED_RESPONSE')

    def test_abort_overrides_truncation(self):
        # 取消路径不落 STREAM_CLOSED：解析器阻塞等待时外部置位 → StreamAborted
        # （而非 EOF 截断判定；_aiter_raced 竞速在下一次取块判负即抛）
        adapter = DeepSeekAdapter(api_key='sk-test')
        abort = asyncio.Event()

        async def source():
            yield _chunk().rstrip('\n')
            yield ''
            # 流在此截断且不再推进：若无 abort，将判 STREAM_CLOSED
            await asyncio.Event().wait()

        async def scenario():
            async def killer():
                await asyncio.sleep(0.05)
                abort.set()
            task = asyncio.create_task(killer())
            try:
                async for chunk in adapter._parse_sse(source(), abort):
                    pass
            finally:
                await task

        with self.assertRaises(StreamAborted):
            asyncio.run(scenario())


def _stream(handler):
    adapter = DeepSeekAdapter(api_key='sk-test', transport=httpx.MockTransport(handler))
    return adapter


MESSAGES = [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}]


def _collect(adapter, messages=MESSAGES):
    async def scenario():
        out = []
        async for chunk in adapter.stream(messages, None):
            out.append(chunk)
        return out
    return asyncio.run(scenario())


def _sse_response(text='hi', finish='stop', headers=None):
    piece = json.dumps({'choices': [{'index': 0, 'delta': {'content': text},
                                     'finish_reason': finish}]})
    return httpx.Response(
        200, content=f'data: {piece}\n\ndata: [DONE]\n\n'.encode(),
        headers=headers or {'content-type': 'text/event-stream'})


class TransportTest(unittest.TestCase):
    def test_happy_path_full_stream(self):
        def handler(request):
            self.assertEqual(request.headers['authorization'], 'Bearer sk-test')
            self.assertEqual(request.url.path, '/chat/completions')
            return _sse_response()

        out = _collect(_stream(handler))
        self.assertEqual([c['type'] for c in out],
                         ['block-start', 'text-delta', 'block-end', 'finish'])

    def test_http_401_maps_to_auth_with_facts(self):
        def handler(request):
            return httpx.Response(401, text='{"error":"unauthorized"}',
                                  headers={'x-request-id': 'rid-1'})

        with self.assertRaises(LlmFailure) as cm:
            _collect(_stream(handler))
        self.assertEqual(cm.exception.code, 'AUTH')
        self.assertEqual(cm.exception.status, 401)
        self.assertEqual(cm.exception.request_id, 'rid-1')

    def test_http_429_with_retry_after(self):
        def handler(request):
            return httpx.Response(429, text='rate limited',
                                  headers={'retry-after': '5'})

        with self.assertRaises(LlmFailure) as cm:
            _collect(_stream(handler))
        self.assertEqual(cm.exception.code, 'RATE_LIMIT')
        self.assertEqual(cm.exception.provider_retry_after_ms, 5000)

    def test_quota_wording_wins_over_status(self):
        # quota 措辞（任意状态，先于 429）→ QUOTA
        def handler(request):
            return httpx.Response(429, text='insufficient_quota')

        with self.assertRaises(LlmFailure) as cm:
            _collect(_stream(handler))
        self.assertEqual(cm.exception.code, 'QUOTA')

    def test_400_context_window_exceeded(self):
        def handler(request):
            return httpx.Response(400, text='this request exceeds the model context window')

        with self.assertRaises(LlmFailure) as cm:
            _collect(_stream(handler))
        self.assertEqual(cm.exception.code, 'CONTEXT_WINDOW_EXCEEDED')

    def test_400_other_maps_invalid_request(self):
        def handler(request):
            return httpx.Response(400, text='bad param')

        with self.assertRaises(LlmFailure) as cm:
            _collect(_stream(handler))
        self.assertEqual(cm.exception.code, 'INVALID_REQUEST')

    def test_500_maps_server(self):
        def handler(request):
            return httpx.Response(502, text='bad gateway')

        with self.assertRaises(LlmFailure) as cm:
            _collect(_stream(handler))
        self.assertEqual(cm.exception.code, 'SERVER')

    def test_other_status_maps_http(self):
        def handler(request):
            return httpx.Response(418, text="i'm a teapot")

        with self.assertRaises(LlmFailure) as cm:
            _collect(_stream(handler))
        self.assertEqual(cm.exception.code, 'HTTP_418')

    def test_connect_timeout_maps_timedout(self):
        def handler(request):
            raise httpx.ConnectTimeout('boom')

        with self.assertRaises(LlmFailure) as cm:
            _collect(_stream(handler))
        self.assertEqual(cm.exception.code, 'TIMEOUT')

    def test_transport_error_maps_transport(self):
        def handler(request):
            raise httpx.ConnectError('refused')

        with self.assertRaises(LlmFailure) as cm:
            _collect(_stream(handler))
        self.assertEqual(cm.exception.code, 'TRANSPORT')


class ReasoningEffortTest(unittest.TestCase):
    def test_valid_tiers_sent_on_wire(self):
        for tier in ("low", "high", "max"):
            adapter = DeepSeekAdapter(api_key='sk-test', reasoning_effort=tier)
            body = adapter._build_body([], [])
            self.assertEqual(body.get("reasoning_effort"), tier)

    def test_off_and_unset_omitted_on_wire(self):
        off = DeepSeekAdapter(api_key='sk-test', reasoning_effort='off')
        self.assertNotIn("reasoning_effort", off._build_body([], []))
        unset = DeepSeekAdapter(api_key='sk-test')
        self.assertNotIn("reasoning_effort", unset._build_body([], []))

    def test_invalid_tier_rejected(self):
        with self.assertRaises(ValueError):
            DeepSeekAdapter(api_key='sk-test', reasoning_effort='medium')

    def test_property_exposes_tier(self):
        adapter = DeepSeekAdapter(api_key='sk-test', reasoning_effort='high')
        self.assertEqual(adapter.reasoning_effort, 'high')


if __name__ == '__main__':
    unittest.main()