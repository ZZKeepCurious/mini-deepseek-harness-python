"""DeepSeek SSE 解析契约：spec-strict 事件派发（上游 sse.ts:7-9）。

覆盖：事件只在空行终结时派发、EOF 未终止尾部 = 截断（STREAM_CLOSED）、
multi-data join、畸形载荷 MALFORMED_RESPONSE。经 mock urlopen 逐行喂入。
"""
import json
import unittest
from unittest.mock import MagicMock, patch

from miniharness.llm import DeepSeekAdapter, LlmFailure

BODY = {'model': 'deepseek-chat', 'messages': [{'role': 'user', 'content': 'hi'}]}


def _resp(lines):
    resp = MagicMock()
    resp.__iter__ = MagicMock(return_value=iter([l.encode('utf-8') for l in lines]))
    resp.status = 200
    resp.headers = {}
    return resp


def _run(lines):
    adapter = DeepSeekAdapter(api_key='sk-test')
    with patch('miniharness.llm.deepseek.urllib.request.urlopen', return_value=_resp(lines)):
        return [c for c in adapter._iter_chunks(BODY)]


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


if __name__ == '__main__':
    unittest.main()
