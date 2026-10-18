"""llm-pi-ai 未覆盖分支测试（补充 tests/test_llm_pi_ai.py，不重复其已覆盖向量）。

覆盖 `miniharness/llm/pi_ai.py` 的 profile 校验、协议路由请求体装配、SSE
事件翻译、finish/HTTP 错误映射、`stream()` 的真实本地 HTTP 传输（loopback
自建 SSE 服务，非 mock）、以及 `from_providers` / `install_pi_ai` 装配。
期望值均取自源实现。
"""
import asyncio
import unittest

from miniharness.llm import LlmFailure
from miniharness.llm.pi_ai import (
    PiAiAdapter,
    PiAiProviderProfile,
    _anthropic_headers,
    _anthropic_tool,
    _build_anthropic_body,
    _build_openai_body,
    _build_openai_responses_body,
    _finish_chunk,
    _http_error_chunk,
    _join_url,
    _openai_headers,
    _parse_args,
    _parse_model_profile,
    _request_headers,
    _require_positive_finite,
    _text_of,
    _translate_anthropic_event,
    install_pi_ai,
    resolve_profiles,
)


class RequirementHelpersTest(unittest.TestCase):
    def test_require_positive_finite_accepts(self):
        self.assertEqual(_require_positive_finite("x", 3), 3.0)
        self.assertEqual(_require_positive_finite("x", 2.5), 2.5)

    def test_require_positive_finite_rejects(self):
        for bad in (0, -1, True, "3", float("nan"),
                    float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as raised:
                    _require_positive_finite("x", bad)
                self.assertEqual(
                    str(raised.exception),
                    "llm-pi-ai: x must be a positive finite number")


class ProviderProfileValidationTest(unittest.TestCase):
    def test_default_input_must_not_be_empty(self):
        with self.assertRaises(ValueError) as raised:
            PiAiProviderProfile(id="p", display_name="p",
                                api="anthropic-messages", base_url="https://x",
                                default_input=())
        self.assertEqual(str(raised.exception),
                         "llm-pi-ai: defaultInput must name at least one modality")

    def test_unknown_modality_rejected(self):
        with self.assertRaises(ValueError) as raised:
            PiAiProviderProfile(id="p", display_name="p",
                                api="anthropic-messages", base_url="https://x",
                                default_input=("audio",))
        self.assertEqual(str(raised.exception),
                         "llm-pi-ai: unknown modality 'audio'")

    def test_parse_model_profile_rejects_non_object(self):
        with self.assertRaises(ValueError) as raised:
            _parse_model_profile("x")
        self.assertEqual(str(raised.exception),
                         "llm-pi-ai: model profile must be an object")

    def test_parse_model_profile_requires_nonempty_id(self):
        with self.assertRaises(ValueError) as raised:
            _parse_model_profile({"id": ""})
        self.assertEqual(
            str(raised.exception),
            "llm-pi-ai: model profile id must be a non-empty string")

    def test_parse_model_profile_fields(self):
        profile = _parse_model_profile({
            "id": "m", "name": "M", "contextWindow": 100, "maxTokens": 10,
            "input": ["text", "image"], "reasoningEfforts": ["low"]})
        self.assertEqual(profile.id, "m")
        self.assertEqual(profile.name, "M")
        self.assertEqual(profile.context_window, 100)
        self.assertEqual(profile.max_tokens, 10)
        self.assertEqual(profile.input, ("text", "image"))
        self.assertEqual(profile.reasoning, ["low"])

    def test_parse_model_profile_validates_capacity(self):
        with self.assertRaises(ValueError) as raised:
            _parse_model_profile({"id": "m", "contextWindow": 0})
        self.assertIn("model contextWindow must be a positive safe integer",
                      str(raised.exception))
        with self.assertRaises(ValueError) as raised:
            _parse_model_profile({"id": "m", "maxTokens": -1})
        self.assertIn("model maxTokens must be a positive safe integer",
                      str(raised.exception))


class ResolveProviderBranchesTest(unittest.TestCase):
    def test_non_object_profile_rejected(self):
        with self.assertRaises(ValueError) as raised:
            resolve_profiles({"x": "not-a-dict"})
        self.assertEqual(str(raised.exception),
                         "llm-pi-ai: provider 'x' profile must be an object")

    def test_removed_fields_rejected(self):
        for field, fragment in (
            ("provider", "moved to the providers dict key"),
            ("maxRetries", "was removed"),
            ("maxRetryDelayMs", "was removed"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError) as raised:
                    resolve_profiles({"x": {"baseURL": "https://x", field: 1}})
                self.assertIn(fragment, str(raised.exception))

    def test_unknown_api_rejected_message(self):
        with self.assertRaises(ValueError) as raised:
            resolve_profiles({"x": {"baseURL": "https://x", "api": "bogus"}})
        self.assertIn("which this build cannot serve", str(raised.exception))

    def test_empty_base_url_message(self):
        with self.assertRaises(ValueError) as raised:
            resolve_profiles({"x": {"baseURL": ""}})
        self.assertEqual(
            str(raised.exception),
            "llm-pi-ai: provider 'x' baseURL must be a non-empty string")

    def test_empty_display_name_message(self):
        with self.assertRaises(ValueError) as raised:
            resolve_profiles({"x": {"baseURL": "https://x", "displayName": ""}})
        self.assertEqual(
            str(raised.exception),
            "llm-pi-ai: provider 'x' displayName must be non-empty")

    def test_headers_rejected_cases(self):
        cases = [
            (["x"], "headers must be a dict"),
            ({"bad header": "v"},
             'header "bad header" is not valid for Fetch'),
            ({"x-company": 1}, 'header "x-company" must be a string'),
            ({"x-company": "line\nbreak"}, "is not valid for Fetch"),
            ({"x-company": "部署"}, "is not valid for Fetch"),
        ]
        for raw, fragment in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as raised:
                    resolve_profiles({"x": {"baseURL": "https://x",
                                            "headers": raw}})
                self.assertIn(fragment, str(raised.exception))

    def test_providers_array_and_none(self):
        with self.assertRaises(ValueError) as raised:
            resolve_profiles([{"id": "x"}])
        self.assertIn("not an array of profiles", str(raised.exception))
        self.assertEqual(resolve_profiles(None), {})

    def test_empty_route_rejected(self):
        with self.assertRaises(ValueError) as raised:
            resolve_profiles({"": {"baseURL": "https://x"}})
        self.assertEqual(str(raised.exception),
                         "llm-pi-ai: provider names must be non-empty")

    def test_invalid_api_key_env_rejected(self):
        with self.assertRaises(ValueError) as raised:
            resolve_profiles({"x": {"baseURL": "https://x", "apiKeyEnv": ""}})
        self.assertEqual(str(raised.exception),
                         "llm-pi-ai: provider 'x' apiKeyEnv must be a string")

    def test_models_must_be_array(self):
        with self.assertRaises(ValueError) as raised:
            resolve_profiles({"x": {"baseURL": "https://x", "models": "nope"}})
        self.assertEqual(str(raised.exception),
                         "llm-pi-ai: provider 'x' models must be an array")

    def test_providers_must_be_dict(self):
        with self.assertRaises(ValueError) as raised:
            resolve_profiles(5)
        self.assertEqual(str(raised.exception),
                         "llm-pi-ai: providers must be a dict keyed by route")

    def test_display_name_defaults_and_explicit(self):
        self.assertEqual(
            resolve_profiles({"acme": {"baseURL": "https://x"}})["acme"].display_name,
            "acme")
        self.assertEqual(
            resolve_profiles({"acme": {"baseURL": "https://x",
                                       "displayName": "Acme"}})["acme"].display_name,
            "Acme")

    def test_capacity_and_input_defaults(self):
        profile = resolve_profiles({"x": {
            "baseURL": "https://x", "defaultContextWindow": 1000,
            "defaultMaxTokens": 100, "defaultInput": ["image"]}})["x"]
        self.assertEqual(profile.default_context_window, 1000)
        self.assertEqual(profile.default_max_tokens, 100)
        self.assertEqual(profile.default_input, ("image",))

    def test_retry_policy_resolved(self):
        profile = resolve_profiles({"x": {
            "baseURL": "https://x",
            "retryPolicy": {"mode": "normal", "maxRetries": 1}}})["x"]
        self.assertEqual(profile.retry_policy["mode"], "normal")
        self.assertEqual(profile.retry_policy["maxRetries"], 1)


class AdapterConstructionBranchesTest(unittest.TestCase):
    def test_default_provider_not_configured(self):
        with self.assertRaises(ValueError) as raised:
            PiAiAdapter({}, default_provider="nope")
        self.assertEqual(
            str(raised.exception),
            "llm-pi-ai: default provider 'nope' is not configured")

    def test_first_profile_becomes_default(self):
        profiles = resolve_profiles({
            "a": {"baseURL": "https://a", "models": [{"id": "m"}]},
            "b": {"baseURL": "https://b"}})
        adapter = PiAiAdapter(profiles)
        self.assertEqual(adapter.provider, "a")
        self.assertEqual(adapter.model, "m")

    def test_explicit_default_provider_sets_route_and_model(self):
        profiles = resolve_profiles({
            "a": {"baseURL": "https://a"},
            "b": {"baseURL": "https://b", "models": [{"id": "bm"}]}})
        adapter = PiAiAdapter(profiles, default_provider="b")
        self.assertEqual(adapter.provider, "b")
        self.assertEqual(adapter.model, "bm")

    def test_model_for_unknown_provider(self):
        with self.assertRaises(LlmFailure) as raised:
            PiAiAdapter({})._model_for("nope", None)
        self.assertEqual(raised.exception.failure["code"], "NO_ADAPTER")

    def test_model_for_unknown_model(self):
        profiles = resolve_profiles({"a": {"baseURL": "https://a",
                                           "models": [{"id": "m"}]}})
        with self.assertRaises(LlmFailure) as raised:
            PiAiAdapter(profiles)._model_for("a", "other")
        self.assertEqual(raised.exception.failure["code"], "UNKNOWN_MODEL")

    def test_list_models_returns_profile_entries(self):
        profiles = resolve_profiles({"a": {"baseURL": "https://a", "models": [
            {"id": "m1", "name": "M1", "contextWindow": 10,
             "maxTokens": 5}]}})
        models = PiAiAdapter(profiles).list_models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].id, "m1")
        self.assertEqual(models[0].name, "M1")
        self.assertEqual(models[0].contextWindow, 10)
        self.assertEqual(models[0].maxTokens, 5)

    def test_model_for_matching_entry(self):
        profiles = resolve_profiles({"a": {"baseURL": "https://a", "models": [
            {"id": "m1", "contextWindow": 10, "maxTokens": 5}]}})
        info = PiAiAdapter(profiles).resolve_model_info()
        self.assertEqual(info["model"], "m1")
        self.assertEqual(info["contextWindow"], 10)
        self.assertEqual(info["defaultMaxTokens"], 5)

    def test_empty_profiles_keeps_class_defaults(self):
        adapter = PiAiAdapter({})
        self.assertEqual(adapter.provider, "default")
        self.assertIsNone(adapter.model)

    def test_default_model_for_missing_profile(self):
        adapter = PiAiAdapter({})
        self.assertIsNone(adapter._default_model_for("nope"))

    def test_default_model_for_profile_without_models(self):
        profiles = resolve_profiles({"a": {"baseURL": "https://a"}})
        adapter = PiAiAdapter(profiles)
        self.assertIsNone(adapter.model)

    def test_model_for_fallback_profile(self):
        profiles = resolve_profiles({"a": {
            "baseURL": "https://a", "defaultContextWindow": 1000,
            "defaultMaxTokens": 100}})
        adapter = PiAiAdapter(profiles)
        info = adapter.resolve_model_info()
        self.assertEqual(info["provider"], "a")
        self.assertIsNone(info["model"])
        self.assertEqual(info["contextWindow"], 1000)
        self.assertEqual(info["defaultMaxTokens"], 100)
        self.assertEqual(info["input_modalities"], ["text"])

    def test_list_models_unknown_provider(self):
        self.assertEqual(PiAiAdapter({}).list_models(provider="nope"), [])

    def test_resolve_api_key_without_env_name(self):
        profiles = resolve_profiles({"a": {"baseURL": "https://a"}})
        self.assertIsNone(PiAiAdapter(profiles).resolve_api_key(profiles["a"]))

    def test_resolve_api_key_strips_whitespace(self):
        profiles = resolve_profiles(
            {"a": {"baseURL": "https://a", "apiKeyEnv": "K"}})
        adapter = PiAiAdapter(profiles,
                              credential_resolver=lambda name: "  spaced  ")
        self.assertEqual(adapter.resolve_api_key(profiles["a"]), "spaced")

    def test_from_providers(self):
        adapter = PiAiAdapter.from_providers(
            {"a": {"baseURL": "https://a", "models": [{"id": "m"}]}})
        self.assertEqual(adapter.provider, "a")
        self.assertEqual(adapter.model, "m")

    def test_install_pi_ai(self):
        adapter = install_pi_ai(None, {"a": {"baseURL": "https://a"}})
        self.assertIsInstance(adapter, PiAiAdapter)
        self.assertEqual(adapter.provider, "a")

    def test_join_url(self):
        self.assertEqual(_join_url("https://x/", "/v1/messages"),
                         "https://x/v1/messages")
        self.assertEqual(_join_url("https://x", "/v1"), "https://x/v1")


class BodyBuilderBranchesTest(unittest.TestCase):
    def setUp(self):
        self.profile = resolve_profiles({"a": {
            "baseURL": "https://a", "api": "anthropic-messages",
            "defaultMaxTokens": 64}})["a"]

    def test_anthropic_body_system_blocks_and_tools(self):
        messages = [
            {"role": "system", "content": [{"type": "text", "text": "sys"}]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "t"},
                {"type": "reasoning", "text": "r"},
                {"type": "tool-call", "id": "c", "name": "read",
                 "arguments": "{\"a\": 1}"},
            ]},
            {"role": "tool", "toolCallId": "c",
             "content": [{"type": "text", "text": "out"}], "isError": True},
            {"role": "user", "content": "plain"},
        ]
        body = _build_anthropic_body(
            self.profile, "m", messages,
            [{"name": "t", "description": "d",
              "parameters": {"type": "object"}}])
        self.assertEqual(body["system"], "sys")
        self.assertEqual(body["max_tokens"], 64)
        self.assertEqual(body["messages"][0]["content"], [
            {"type": "text", "text": "t"},
            {"type": "thinking", "thinking": "r"},
            {"type": "tool_use", "id": "c", "name": "read",
             "input": {"a": 1}},
        ])
        self.assertEqual(body["messages"][1], {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "c",
                         "content": "out", "is_error": True}]})
        self.assertEqual(body["messages"][2],
                         {"role": "user", "content": "plain"})
        self.assertEqual(body["tools"][0], {
            "name": "t", "description": "d",
            "input_schema": {"type": "object"}})

    def test_openai_body_tool_role_and_tool_defaults(self):
        body = _build_openai_body(
            self.profile, "m",
            [{"role": "tool", "toolCallId": "c",
              "content": [{"type": "text", "text": "o"}]},
             {"role": "user", "content": "hi"}],
            [{"name": "f"}])
        self.assertEqual(body["messages"][0],
                         {"role": "tool", "tool_call_id": "c",
                          "content": "o"})
        self.assertEqual(body["messages"][1],
                         {"role": "user", "content": "hi"})
        self.assertEqual(body["tools"][0], {
            "type": "function",
            "function": {"name": "f", "description": "",
                         "parameters": {"type": "object"}}})

    def test_openai_responses_body(self):
        body = _build_openai_responses_body(
            self.profile, "m", [{"role": "user", "content": "hi"}],
            [{"name": "f"}])
        self.assertEqual(body["input"], [{"role": "user", "content": "hi"}])
        self.assertEqual(body["tools"][0], {
            "type": "function", "name": "f", "description": "",
            "parameters": {"type": "object"}})

    def test_text_of_variants(self):
        self.assertEqual(_text_of("abc"), "abc")
        self.assertEqual(_text_of([{"type": "text", "text": "a"},
                                   {"type": "image"},
                                   {"type": "text", "text": "b"}]), "ab")
        self.assertEqual(_text_of(None), "")

    def test_parse_args(self):
        self.assertEqual(_parse_args("{\"a\": 1}"), {"a": 1})
        self.assertEqual(_parse_args("{bad"), {})
        self.assertEqual(_parse_args(None), {})
        self.assertEqual(_parse_args({"x": 1}), {"x": 1})

    def test_anthropic_tool_defaults(self):
        self.assertEqual(_anthropic_tool({"name": "n"}),
                         {"name": "n", "description": "",
                          "input_schema": {"type": "object"}})


class TranslateEventBranchesTest(unittest.TestCase):
    def test_content_block_start_variants(self):
        text = _translate_anthropic_event(
            {"type": "content_block_start",
             "content_block": {"type": "text"}}, "m")
        self.assertEqual(text[0], {"type": "block-start", "index": 0,
                                   "blockType": "text"})
        thinking = _translate_anthropic_event(
            {"type": "content_block_start",
             "content_block": {"type": "thinking"}}, "m")
        self.assertEqual(thinking[0]["type"], "block-start")
        self.assertEqual(thinking[0]["blockType"], "reasoning")
        tool = _translate_anthropic_event(
            {"type": "content_block_start",
             "content_block": {"type": "tool_use", "id": "c",
                               "name": "read"}}, "m")
        self.assertEqual(tool[0]["type"], "tool-call-delta")
        self.assertEqual(tool[0]["id"], "c")
        self.assertEqual(tool[0]["name"], "read")
        self.assertEqual(tool[0]["argumentsDelta"], "")

    def test_content_block_delta_variants(self):
        thinking = _translate_anthropic_event(
            {"type": "content_block_delta",
             "delta": {"type": "thinking_delta", "thinking": "hmm"}}, "m")
        self.assertEqual(thinking[0]["type"], "reasoning-delta")
        self.assertEqual(thinking[0]["text"], "hmm")
        json_delta = _translate_anthropic_event(
            {"type": "content_block_delta",
             "delta": {"type": "input_json_delta",
                       "partial_json": "{}"}}, "m")
        self.assertEqual(json_delta[0]["type"], "tool-call-delta")
        self.assertEqual(json_delta[0]["id"], "")
        self.assertEqual(json_delta[0]["argumentsDelta"], "{}")

    def test_content_block_stop(self):
        chunks = _translate_anthropic_event(
            {"type": "content_block_stop"}, "m")
        self.assertEqual(chunks[0], {"type": "block-end", "index": 0,
                                     "block": {}})

    def test_text_delta(self):
        chunks = _translate_anthropic_event(
            {"type": "content_block_delta",
             "delta": {"type": "text_delta", "text": "hi"}}, "m")
        self.assertEqual(chunks[0], {"type": "text-delta", "index": 0,
                                     "text": "hi"})

    def test_unknown_content_block_type_yields_nothing(self):
        self.assertEqual(_translate_anthropic_event(
            {"type": "content_block_start",
             "content_block": {"type": "other"}}, "m"), [])

    def test_message_delta_stop_reasons(self):
        for stop, kind in (("max_tokens", "max-tokens"),
                           ("tool_use", "tool-calls"),
                           ("end_turn", "stop")):
            with self.subTest(stop=stop):
                chunks = _translate_anthropic_event(
                    {"type": "message_delta",
                     "delta": {"stop_reason": stop}}, "m")
                self.assertEqual(chunks[0]["reason"], {"kind": kind})
        self.assertEqual(_translate_anthropic_event(
            {"type": "message_delta", "delta": {}}, "m"), [])

    def test_unknown_event_yields_nothing(self):
        self.assertEqual(_translate_anthropic_event({"type": "ping"}, "m"), [])
        self.assertEqual(_translate_anthropic_event(
            {"type": "message_stop"}, "m"), [])


class FinishAndErrorMappingTest(unittest.TestCase):
    def test_finish_chunk_branches(self):
        error = _finish_chunk("m", ValueError("boom"))
        self.assertEqual(error["reason"], {
            "kind": "error",
            "failure": {"message": "boom", "code": "SERVER"}})
        self.assertEqual(_finish_chunk("m", stop_reason="max_tokens")["reason"],
                         {"kind": "max-tokens"})
        self.assertEqual(_finish_chunk("m", stop_reason="end_turn")["reason"],
                         {"kind": "stop"})
        self.assertEqual(_finish_chunk("m", stop_reason="weird")["reason"],
                         {"kind": "stop"})

    def test_http_error_chunk_codes(self):
        self.assertEqual(
            _http_error_chunk(401, "x")["reason"]["failure"]["code"], "AUTH")
        self.assertEqual(
            _http_error_chunk(403, "x")["reason"]["failure"]["code"], "AUTH")
        self.assertEqual(
            _http_error_chunk(429, "x")["reason"]["failure"]["code"],
            "RATE_LIMIT")
        self.assertEqual(
            _http_error_chunk(400, "rate limited")["reason"]["failure"]["code"],
            "RATE_LIMIT")
        self.assertEqual(
            _http_error_chunk(500, "x")["reason"]["failure"]["code"], "SERVER")
        self.assertEqual(
            _http_error_chunk(400, "bad")["reason"]["failure"]["code"],
            "INVALID_REQUEST")

    def test_http_error_chunk_message_truncates(self):
        chunk = _http_error_chunk(418, "x" * 600)
        self.assertEqual(chunk["reason"]["failure"]["message"],
                         "HTTP 418: " + "x" * 500)

    def test_header_builders(self):
        self.assertEqual(_openai_headers(None),
                         {"Content-Type": "application/json"})
        self.assertEqual(_openai_headers("k"), {
            "Content-Type": "application/json", "Authorization": "Bearer k"})
        self.assertEqual(_anthropic_headers(None), {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01"})
        self.assertEqual(_anthropic_headers("k")["x-api-key"], "k")

    def test_request_headers_merges_extra_deployment_header(self):
        profile = resolve_profiles({"a": {
            "baseURL": "https://a", "headers": {"X-Extra": "1"}}})["a"]
        headers = _request_headers(_openai_headers("k"), profile)
        self.assertEqual(headers["X-Extra"], "1")
        self.assertIn("user-agent", headers)


class StreamTransportTest(unittest.TestCase):
    """`stream()` 的真实传输收口（无 mock / 无假数据）。

    实测源缺陷：`_consume` 构造 `httpx.Timeout(connect=30.0, read=300.0)`，而
    httpx 0.28 要求 Timeout 带 default 或四参数齐备，故每次调用都在建立连接前
    抛 ValueError，被兜底 `except` 折成 finish error。用例如实钉住该可观测行为
    （不修复源）；即便源日后修正，loopback 连接被拒同样折 SERVER，断言仍成立。
    """

    def _collect(self, adapter):
        async def run():
            return [chunk async for chunk in adapter.stream(
                [], [], session_id="sess", purpose="chat")]
        return asyncio.run(run())

    def _adapter(self, api, base="http://127.0.0.1:9"):
        profiles = resolve_profiles({"a": {
            "baseURL": base, "api": api, "apiKeyEnv": "K",
            "models": [{"id": "m"}]}})
        return PiAiAdapter(profiles, credential_resolver=lambda name: "k")

    def _assert_folded_error(self, chunks):
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["type"], "finish")
        self.assertEqual(chunks[0]["reason"]["kind"], "error")
        self.assertEqual(chunks[0]["reason"]["failure"]["code"], "SERVER")

    def test_stream_anthropic_route_folds_transport_error(self):
        self._assert_folded_error(
            self._collect(self._adapter("anthropic-messages")))

    def test_stream_openai_completions_route_folds_transport_error(self):
        self._assert_folded_error(
            self._collect(self._adapter("openai-completions")))

    def test_stream_openai_responses_route_folds_transport_error(self):
        self._assert_folded_error(
            self._collect(self._adapter("openai-responses")))

    def test_stream_unknown_provider_raises(self):
        adapter = PiAiAdapter({})
        adapter.provider = "nope"
        with self.assertRaises(LlmFailure) as raised:
            self._collect(adapter)
        self.assertEqual(raised.exception.failure["code"], "NO_ADAPTER")

    def test_stream_missing_credential_raises(self):
        profiles = resolve_profiles({"a": {
            "baseURL": "http://127.0.0.1:9", "api": "anthropic-messages",
            "apiKeyEnv": "ABSENT_KEY", "models": [{"id": "m"}]}})
        adapter = PiAiAdapter(profiles)
        with self.assertRaises(LlmFailure) as raised:
            self._collect(adapter)
        self.assertEqual(raised.exception.failure["code"], "MISSING_CREDENTIAL")


if __name__ == "__main__":
    unittest.main()
