# -*- coding: utf-8 -*-
"""DeepSeek Messages（Anthropic 兼容）SSE 解析契约 + httpx 传输契约。

解析：事件只在空行终结时派发、EOF 未到 message_stop = 截断（STREAM_CLOSED）、
multi-data join、畸形载荷 MALFORMED_RESPONSE、带内 error 即 provider 失败、
abort 覆盖截断判定。翻译：text/thinking/tool_use 块与 stop_reason/usage 映射。
传输：经 httpx.MockTransport 注入，覆盖 HTTP 错误映射 / facts / 超时。
"""
import asyncio
import json
import unittest

import httpx

from miniharness.identity import get_or_create_anonymous_user_id
from miniharness.llm import DeepSeekAdapter, LlmFailure, user_agent
from miniharness.llm.deepseek_files import DEFAULT_MODELS
from miniharness.llm.deepseek_messages import messages_api_root
from miniharness.llm.protocol import StreamAborted


def _frame(event_type, payload):
    return ['event: ' + event_type, 'data: ' + json.dumps(payload), '']


def _message_start(usage=None):
    message = {}
    if usage is not None:
        message['usage'] = usage
    return _frame('message_start', {'type': 'message_start', 'message': message})


def _start(index, content_block, type_='content_block_start'):
    return _frame(type_, {'type': type_, 'index': index, 'content_block': content_block})


def _delta(index, delta):
    return _frame('content_block_delta',
                  {'type': 'content_block_delta', 'index': index, 'delta': delta})


def _stop(index):
    return _frame('content_block_stop', {'type': 'content_block_stop', 'index': index})


def _settle(stop_reason, usage=None):
    payload = {'type': 'message_delta', 'delta': {'stop_reason': stop_reason}}
    if usage is not None:
        payload['usage'] = usage
    return _frame('message_delta', payload)


def _message_stop():
    return _frame('message_stop', {'type': 'message_stop'})


def _text_stream(text='ok', stop_reason='end_turn'):
    return (_message_start()
            + _start(0, {'type': 'text', 'text': ''})
            + _delta(0, {'type': 'text_delta', 'text': text})
            + _stop(0) + _settle(stop_reason) + _message_stop())


async def _alines(lines):
    # 模拟 httpx aiter_lines：按行产出且剥离行尾换行
    for line in lines:
        yield line.rstrip('\r\n')


async def _parse(lines, abort_event=None):
    adapter = DeepSeekAdapter(api_key='sk-test')
    return [c async for c in adapter._parse_sse(_alines(lines), abort_event)]


def _run(lines):
    return asyncio.run(_parse(lines))


def _tool_block(out):
    return [c for c in out if c['type'] == 'block-end'][0]['block']


class SseParsingTest(unittest.TestCase):
    def test_terminated_events_dispatch(self):
        out = _run(_text_stream())
        self.assertEqual([c['type'] for c in out],
                         ['block-start', 'text-delta', 'block-end', 'usage', 'finish'])
        self.assertEqual(out[1]['text'], 'ok')
        self.assertEqual(out[4]['reason'], {'kind': 'stop'})

    def test_unterminated_tail_is_truncation(self):
        # spec-strict：事件只在空行终结时派发，EOF 处的未终止尾部是截断 →
        # 缺 message_stop → STREAM_CLOSED
        lines = _message_start() + _settle('end_turn')
        lines = lines + ['data: ' + json.dumps({'type': 'message_stop'})]
        with self.assertRaises(LlmFailure) as cm:
            _run(lines)
        self.assertEqual(cm.exception.code, 'STREAM_CLOSED')

    def test_stream_without_message_stop_is_truncation(self):
        lines = (_message_start() + _start(0, {'type': 'text', 'text': ''})
                 + _delta(0, {'type': 'text_delta', 'text': 'partial'}) + _stop(0))
        with self.assertRaises(LlmFailure) as cm:
            _run(lines)
        self.assertEqual(cm.exception.code, 'STREAM_CLOSED')

    def test_multi_data_join(self):
        # 同一事件的多个 data: 行以 \n 连接（eventsource-parser multi-data join）
        lines = (_message_start()
                 + _start(0, {'type': 'text', 'text': ''})
                 + ['data: {"type":"content_block_delta","index":0,',
                    'data: "delta":{"type":"text_delta","text":"split"}}', '']
                 + _stop(0) + _settle('end_turn') + _message_stop())
        out = _run(lines)
        self.assertEqual(out[1]['text'], 'split')

    def test_malformed_payload_fails_loud(self):
        with self.assertRaises(LlmFailure) as cm:
            _run(['data: {not json', ''])
        self.assertEqual(cm.exception.code, 'MALFORMED_RESPONSE')

    def test_event_name_mismatch_fails_loud(self):
        lines = _frame('message_start', {'type': 'content_block_delta', 'index': 0,
                                          'delta': {'type': 'text_delta', 'text': 'x'}})
        with self.assertRaises(LlmFailure) as cm:
            _run(lines)
        self.assertEqual(cm.exception.code, 'MALFORMED_RESPONSE')

    def test_in_band_error_event_is_provider_failure(self):
        lines = _frame('error', {'type': 'error',
                                 'error': {'type': 'overloaded_error', 'message': 'busy'}})
        with self.assertRaises(LlmFailure) as cm:
            _run(lines)
        self.assertEqual(cm.exception.code, 'SERVER')

    def test_abort_overrides_truncation(self):
        # 取消路径不落 STREAM_CLOSED：解析器阻塞等待时外部置位 → StreamAborted
        adapter = DeepSeekAdapter(api_key='sk-test')
        abort = asyncio.Event()

        async def source():
            for line in _message_start():
                yield line
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


class TranslateTest(unittest.TestCase):
    def test_duplicate_message_start_rejected(self):
        with self.assertRaises(LlmFailure) as cm:
            _run(_message_start() + _message_start())
        self.assertEqual(cm.exception.code, 'MALFORMED_RESPONSE')

    def test_event_before_message_start_rejected(self):
        with self.assertRaises(LlmFailure) as cm:
            _run(_start(0, {'type': 'text', 'text': ''}))
        self.assertEqual(cm.exception.code, 'MALFORMED_RESPONSE')

    def test_thinking_maps_to_reasoning(self):
        lines = (_message_start()
                 + _start(0, {'type': 'thinking', 'thinking': ''})
                 + _delta(0, {'type': 'thinking_delta', 'thinking': 'why'})
                 + _delta(0, {'type': 'signature_delta', 'signature': 'sig'})
                 + _stop(0) + _settle('end_turn') + _message_stop())
        out = _run(lines)
        self.assertEqual([c['type'] for c in out],
                         ['block-start', 'reasoning-delta', 'block-end', 'usage', 'finish'])
        self.assertEqual(out[2]['block'], {'type': 'reasoning', 'text': 'why'})

    def test_tool_use_streams_arguments(self):
        lines = (_message_start()
                 + _start(0, {'type': 'tool_use', 'id': 'call_1', 'name': 'get_weather',
                              'input': {}})
                 + _delta(0, {'type': 'input_json_delta', 'partial_json': '{"ci'})
                 + _delta(0, {'type': 'input_json_delta', 'partial_json': 'ty":"SF"}'})
                 + _stop(0) + _settle('tool_use') + _message_stop())
        out = _run(lines)
        self.assertEqual(out[1]['type'], 'tool-call-delta')
        self.assertEqual(out[1]['id'], 'call_1')
        self.assertEqual(out[1]['name'], 'get_weather')
        self.assertEqual(_tool_block(out),
                         {'type': 'tool-call', 'id': 'call_1', 'name': 'get_weather',
                          'arguments': '{"city":"SF"}'})
        self.assertEqual(out[-1]['reason'], {'kind': 'tool-calls'})

    def test_empty_tool_identity_rejected(self):
        lines = _start(0, {'type': 'tool_use', 'id': '', 'name': 'x', 'input': {}})
        with self.assertRaises(LlmFailure) as cm:
            _run(_message_start() + lines)
        self.assertEqual(cm.exception.code, 'MALFORMED_RESPONSE')

    def test_unsupported_response_block_rejected(self):
        with self.assertRaises(LlmFailure) as cm:
            _run(_message_start() + _start(0, {'type': 'image'}))
        self.assertEqual(cm.exception.code, 'UNSUPPORTED_CONTENT')

    def test_unknown_stop_reason_rejected(self):
        with self.assertRaises(LlmFailure) as cm:
            _run(_text_stream(stop_reason='pause_turn'))
        self.assertEqual(cm.exception.code, 'MALFORMED_RESPONSE')

    def test_delta_without_open_block_rejected(self):
        with self.assertRaises(LlmFailure) as cm:
            _run(_message_start() + _delta(0, {'type': 'text_delta', 'text': 'x'}))
        self.assertEqual(cm.exception.code, 'MALFORMED_RESPONSE')

    def test_invalid_tool_json_at_message_stop_rejected(self):
        lines = (_message_start()
                 + _start(0, {'type': 'tool_use', 'id': 'c', 'name': 't', 'input': {}})
                 + _delta(0, {'type': 'input_json_delta', 'partial_json': '{"a":'})
                 + _stop(0) + _settle('tool_use') + _message_stop())
        with self.assertRaises(LlmFailure) as cm:
            _run(lines)
        self.assertEqual(cm.exception.code, 'MALFORMED_RESPONSE')

    def test_max_tokens_retains_truncated_tool_json(self):
        lines = (_message_start()
                 + _start(0, {'type': 'tool_use', 'id': 'c', 'name': 't', 'input': {}})
                 + _delta(0, {'type': 'input_json_delta', 'partial_json': '{"a":'})
                 + _stop(0) + _settle('max_tokens') + _message_stop())
        out = _run(lines)
        self.assertEqual(_tool_block(out)['arguments'], '{"a":')
        self.assertEqual(out[-1]['reason'], {'kind': 'max-tokens'})

    def test_empty_response_rejected(self):
        lines = _message_start() + _settle('end_turn') + _message_stop()
        with self.assertRaises(LlmFailure) as cm:
            _run(lines)
        self.assertEqual(cm.exception.code, 'EMPTY_RESPONSE')

    def test_usage_maps_anthropic_fields_and_total(self):
        lines = (_message_start({'input_tokens': 100, 'cache_read_input_tokens': 30,
                                 'cache_creation_input_tokens': 5})
                 + _start(0, {'type': 'text', 'text': ''})
                 + _delta(0, {'type': 'text_delta', 'text': 'ok'})
                 + _stop(0) + _settle('end_turn', {'output_tokens': 50})
                 + _message_stop())
        out = _run(lines)
        usage = next(c for c in out if c['type'] == 'usage')['usage']
        self.assertEqual(usage, {'inputTokens': 100, 'outputTokens': 50,
                                 'cacheReadTokens': 30, 'cacheWriteTokens': 5,
                                 'totalTokens': 185})

    def test_invalid_usage_rejected(self):
        with self.assertRaises(LlmFailure) as cm:
            _run(_message_start({'input_tokens': -1})
                 + _settle('end_turn') + _message_stop())
        self.assertEqual(cm.exception.code, 'MALFORMED_RESPONSE')


class MessagesApiRootTest(unittest.TestCase):
    def test_appends_v1_once(self):
        self.assertEqual(messages_api_root('https://api.deepseek.com/anthropic'),
                         'https://api.deepseek.com/anthropic/v1')
        self.assertEqual(messages_api_root('https://api.deepseek.com/anthropic/v1'),
                         'https://api.deepseek.com/anthropic/v1')
        self.assertEqual(messages_api_root('https://api.deepseek.com/anthropic/v1/'),
                         'https://api.deepseek.com/anthropic/v1')


class SerializeMessagesTest(unittest.TestCase):
    def test_text_tool_result_roundtrip(self):
        from miniharness.llm import serialize_messages
        wire = serialize_messages([
            {'role': 'system', 'content': [{'type': 'text', 'text': 'sys'}]},
            {'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]},
            {'role': 'assistant', 'content': [
                {'type': 'text', 'text': 'calling'},
                {'type': 'reasoning', 'text': 'because'},
                {'type': 'tool-call', 'id': 'c1', 'name': 't', 'arguments': '{"a":1}'}]},
            {'role': 'tool', 'toolCallId': 'c1', 'isError': True,
             'content': [{'type': 'text', 'text': 'out'}]},
        ])
        self.assertEqual(wire[0], {'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]})
        self.assertEqual(wire[1]['content'], [
            {'type': 'text', 'text': 'calling'},
            {'type': 'thinking', 'thinking': 'because'},
            {'type': 'tool_use', 'id': 'c1', 'name': 't', 'input': {'a': 1}},
        ])
        self.assertEqual(wire[2]['role'], 'user')
        self.assertEqual(wire[2]['content'], [
            {'type': 'tool_result', 'tool_use_id': 'c1',
             'content': [{'type': 'text', 'text': 'out'}], 'is_error': True}])

    def test_in_history_system_update_stays(self):
        from miniharness.llm import serialize_messages
        wire = serialize_messages([
            {'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]},
            {'role': 'system', 'content': [{'type': 'text', 'text': 'update'}]},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'ok'}]},
        ], model='deepseek-flash', models=DEFAULT_MODELS)
        self.assertEqual(wire[1], {'role': 'system',
                                   'content': [{'type': 'text', 'text': 'update'}]})

    def test_non_in_history_system_update_folds_to_history_system(self):
        from miniharness.llm import serialize
        body = serialize([
            {'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]},
            {'role': 'system', 'content': [{'type': 'text', 'text': 'update'}]},
        ], model='deepseek-v4-pro', models=DEFAULT_MODELS)
        self.assertEqual(body['system'], 'update')
        self.assertEqual([m['role'] for m in body['messages']], ['user'])

    def test_developer_message_rejected(self):
        from miniharness.llm import serialize_messages
        message = {'role': 'developer', 'content': [{'type': 'tool-addition', 'toolName': 'x'}]}
        with self.assertRaises(LlmFailure) as cm:
            serialize_messages([message])
        self.assertEqual(cm.exception.code, 'UNSUPPORTED_CONTENT')

    def test_tool_result_without_call_rejected(self):
        from miniharness.llm import serialize_messages
        message = {'role': 'tool', 'toolCallId': 'nope',
                   'content': [{'type': 'text', 'text': 'out'}]}
        with self.assertRaises(LlmFailure) as cm:
            serialize_messages([message])
        self.assertEqual(cm.exception.code, 'INVALID_REQUEST')

    def test_duplicate_tool_call_id_rejected(self):
        from miniharness.llm import serialize_messages
        message = {'role': 'assistant', 'content': [
            {'type': 'tool-call', 'id': 'c', 'name': 'a', 'arguments': '{}'},
            {'type': 'tool-call', 'id': 'c', 'name': 'b', 'arguments': '{}'}]}
        with self.assertRaises(LlmFailure) as cm:
            serialize_messages([message])
        self.assertEqual(cm.exception.code, 'INVALID_REQUEST')


def _stream(handler):
    return DeepSeekAdapter(api_key='sk-test', transport=httpx.MockTransport(handler))


MESSAGES = [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}]


def _collect(adapter, messages=MESSAGES):
    async def scenario():
        out = []
        async for chunk in adapter.stream(messages, None):
            out.append(chunk)
        return out
    return asyncio.run(scenario())


def _sse_response(text='hi', stop_reason='end_turn', headers=None):
    body = '\n'.join(_text_stream(text, stop_reason)) + '\n\n'
    return httpx.Response(200, content=body.encode(),
                          headers=headers or {'content-type': 'text/event-stream'})


class TransportTest(unittest.TestCase):
    def test_happy_path_full_stream(self):
        def handler(request):
            self.assertEqual(request.headers['x-api-key'], 'sk-test')
            self.assertEqual(request.headers['anthropic-version'], '2023-06-01')
            # 上游 runtime.spec.ts:376-378：归因与请求身份是独立 wire 事实。
            self.assertEqual(request.headers['user-agent'], user_agent())
            self.assertEqual(request.headers['x-deepseek-harness-user-id'],
                             get_or_create_anonymous_user_id())
            self.assertEqual(request.url.path, '/anthropic/v1/messages')
            return _sse_response()

        out = _collect(_stream(handler))
        self.assertEqual([c['type'] for c in out],
                         ['block-start', 'text-delta', 'block-end', 'usage', 'finish'])

    def test_session_identity_and_compaction_headers(self):
        # 上游 adapter.ts:129-130 + runtime.spec.ts:379,383：会话身份头按
        # options.sessionId 携带；压缩头仅 purpose='compaction' 携带；
        # 普通请求两者均缺省。
        seen = []

        def handler(request):
            seen.append((request.headers.get('x-deepseek-harness-session-id'),
                         request.headers.get('x-deepseek-harness-compact')))
            return _sse_response()

        adapter = _stream(handler)

        async def run():
            for kwargs in ({"session_id": "sess-1", "purpose": "compaction"},
                           {"session_id": "sess-1", "purpose": "session-title"},
                           {}):
                async for _ in adapter.stream(MESSAGES, None, **kwargs):
                    pass

        asyncio.run(run())
        self.assertEqual(seen, [("sess-1", "1"), ("sess-1", None), (None, None)])

    def test_account_token_uses_auth_token_header(self):
        def handler(request):
            self.assertNotIn('x-api-key', request.headers)
            self.assertEqual(request.headers['x-dsh-auth-token'], 'acct-1')
            return _sse_response()

        adapter = DeepSeekAdapter(api_key='sk-test', account_token='acct-1',
                                  transport=httpx.MockTransport(handler))
        _collect(adapter)

    def test_http_401_maps_to_auth_with_facts(self):
        def handler(request):
            return httpx.Response(401, json={'error': {'type': 'authentication_error',
                                                       'message': 'unauthorized'}},
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
        # 上游 error.ts:97-103 isQuotaExceededError 的五条文案逐条覆盖
        # （修复前 mini 只认前两条，后三条会被误判为可重试限流）。
        wordings = [
            'insufficient_quota',
            'usage limit exceeded',
            'You exceeded your current quota',
            'balance exhausted',
            'out of credits',
        ]
        for wording in wordings:
            with self.subTest(wording=wording):
                def handler(request, text=wording):
                    return httpx.Response(429, text=text)

                with self.assertRaises(LlmFailure) as cm:
                    _collect(_stream(handler))
                self.assertEqual(cm.exception.code, 'QUOTA')

        def limited(request):
            return httpx.Response(429, text='rate limited, retry after 5s')

        with self.assertRaises(LlmFailure) as cm:
            _collect(_stream(limited))
        self.assertEqual(cm.exception.code, 'RATE_LIMIT')

    def test_400_context_window_exceeded(self):
        def handler(request):
            return httpx.Response(400, text='this request exceeds the model context window')

        with self.assertRaises(LlmFailure) as cm:
            _collect(_stream(handler))
        self.assertEqual(cm.exception.code, 'CONTEXT_WINDOW_EXCEEDED')

    def test_400_other_maps_invalid_request(self):
        def handler(request):
            return httpx.Response(400, json={'error': {'type': 'invalid_request_error',
                                                       'message': 'bad param'}})

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
        for tier in ('low', 'high', 'max'):
            adapter = DeepSeekAdapter(api_key='sk-test', reasoning_effort=tier)
            body = adapter._build_body([], [])
            self.assertEqual(body['output_config']['effort'], tier)
            self.assertEqual(body['thinking'], {'type': 'enabled'})

    def test_off_disables_thinking_without_output_config(self):
        adapter = DeepSeekAdapter(api_key='sk-test', reasoning_effort='off')
        body = adapter._build_body([], [])
        self.assertEqual(body['thinking'], {'type': 'disabled'})
        self.assertNotIn('output_config', body)

    def test_unset_defaults_to_high(self):
        body = DeepSeekAdapter(api_key='sk-test')._build_body([], [])
        self.assertEqual(body['output_config']['effort'], 'high')

    def test_invalid_tier_rejected(self):
        with self.assertRaises(ValueError):
            DeepSeekAdapter(api_key='sk-test', reasoning_effort='medium')

    def test_thinking_disabled_rejects_non_off_effort(self):
        with self.assertRaises(ValueError):
            DeepSeekAdapter(api_key='sk-test', thinking='disabled', reasoning_effort='high')

    def test_property_exposes_tier(self):
        adapter = DeepSeekAdapter(api_key='sk-test', reasoning_effort='high')
        self.assertEqual(adapter.reasoning_effort, 'high')


class AuthSeamTest(unittest.TestCase):
    """resolveAuth 双 provider 拆分：official(x-api-key) / account(x-dsh-auth-token)。"""

    def test_default_infers_provider_from_account_token(self):
        self.assertEqual(DeepSeekAdapter(api_key='sk-test').provider,
                         'deepseek-official')
        self.assertEqual(DeepSeekAdapter(api_key='sk-test',
                                         account_token='acct-1').provider,
                         'deepseek-account')
        self.assertEqual(DeepSeekAdapter(api_key='sk-test', auth='account',
                                         account_token='acct-1').provider,
                         'deepseek-account')
        with self.assertRaises(ValueError):
            DeepSeekAdapter(api_key='sk-test', auth='nope')

    def test_official_missing_key_fails_before_fetch(self):
        calls = []

        def handler(request):
            calls.append(request)
            return _sse_response()

        adapter = DeepSeekAdapter(api_key='', transport=httpx.MockTransport(handler))
        with self.assertRaises(LlmFailure) as cm:
            _collect(adapter)
        self.assertEqual(cm.exception.code, 'MISSING_CREDENTIAL')
        self.assertEqual(calls, [])

    def test_official_malformed_key_fails_before_fetch(self):
        calls = []

        def handler(request):
            calls.append(request)
            return _sse_response()

        adapter = DeepSeekAdapter(api_key='bad\nkey',
                                  transport=httpx.MockTransport(handler))
        with self.assertRaises(LlmFailure) as cm:
            _collect(adapter)
        self.assertEqual(cm.exception.code, 'INVALID_CREDENTIAL')
        self.assertEqual(calls, [])

    def test_account_signed_out_fails_before_fetch(self):
        calls = []

        def handler(request):
            calls.append(request)
            return _sse_response()

        adapter = DeepSeekAdapter(auth='account',
                                  transport=httpx.MockTransport(handler))
        with self.assertRaises(LlmFailure) as cm:
            _collect(adapter)
        self.assertEqual(cm.exception.code, 'ACCOUNT_SIGN_IN_REQUIRED')
        self.assertEqual(calls, [])

    def test_account_402_maps_to_account_quota_without_rejection(self):
        rejected = []

        def handler(request):
            return httpx.Response(402, json={'error': {
                'code': 'insufficient_balance', 'message': 'Insufficient balance'}})

        adapter = DeepSeekAdapter(account_token='acct-1',
                                  reject_token=rejected.append,
                                  transport=httpx.MockTransport(handler))
        with self.assertRaises(LlmFailure) as cm:
            _collect(adapter)
        self.assertEqual(cm.exception.code, 'ACCOUNT_QUOTA')
        self.assertEqual(rejected, [])

    def test_official_402_stays_provider_neutral_quota(self):
        def handler(request):
            return httpx.Response(402, json={'error': {
                'code': 'insufficient_balance', 'message': 'Insufficient balance'}})

        adapter = _stream(handler)
        with self.assertRaises(LlmFailure) as cm:
            _collect(adapter)
        self.assertEqual(cm.exception.code, 'QUOTA')

    def test_account_in_band_quota_maps_to_account_quota(self):
        def handler(request):
            body = ('event: error\ndata: ' + json.dumps({'type': 'error', 'error': {
                'type': 'invalid_request_error', 'code': 'insufficient_balance',
                'message': 'Insufficient balance'}}) + '\n\n')
            return httpx.Response(200, content=body.encode(),
                                  headers={'content-type': 'text/event-stream'})

        adapter = DeepSeekAdapter(account_token='acct-1', reject_token=lambda _t: None,
                                  transport=httpx.MockTransport(handler))
        with self.assertRaises(LlmFailure) as cm:
            _collect(adapter)
        self.assertEqual(cm.exception.code, 'ACCOUNT_QUOTA')

    def test_account_401_rejects_token(self):
        rejected = []

        def handler(request):
            return httpx.Response(401, text='Unauthorized')

        adapter = DeepSeekAdapter(account_token='acct-1',
                                  reject_token=rejected.append,
                                  transport=httpx.MockTransport(handler))
        with self.assertRaises(LlmFailure) as cm:
            _collect(adapter)
        self.assertEqual(cm.exception.code, 'ACCOUNT_TOKEN_INVALID')
        self.assertEqual(rejected, ['acct-1'])

    def test_account_403_passes_through_without_rejection(self):
        rejected = []

        def handler(request):
            return httpx.Response(403, text='Forbidden')

        adapter = DeepSeekAdapter(account_token='acct-1',
                                  reject_token=rejected.append,
                                  transport=httpx.MockTransport(handler))
        with self.assertRaises(LlmFailure) as cm:
            _collect(adapter)
        self.assertEqual(cm.exception.code, 'AUTH')
        self.assertEqual(rejected, [])

    def test_account_token_uses_auth_token_header_and_provider(self):
        def handler(request):
            self.assertNotIn('x-api-key', request.headers)
            self.assertEqual(request.headers['x-dsh-auth-token'], 'acct-1')
            return _sse_response()

        adapter = DeepSeekAdapter(api_key='sk-test', account_token='acct-1',
                                  transport=httpx.MockTransport(handler))
        out = _collect(adapter)
        self.assertEqual(out[-1]['type'], 'finish')
        self.assertEqual(adapter.provider, 'deepseek-account')


if __name__ == '__main__':
    unittest.main()
