"""llm-pi-ai（M15 功能对标迁移）验收。

对齐上游 `packages/llm/llm-pi-ai` 的可移植功能面：profile 解析/校验、
协议路由（anthropic-messages / openai-completions / openai-responses）、
apiKeyEnv 凭据解析、模型能力/发现。

载体差异（登记）：无 `ctx.llm` 注册表与动态 dormant→live 切换（mini 单适配器
架构）；profile 静态传入；pi-ai SDK 目录数据与 OAuth 流不移植。
"""
import json
import unittest

from miniharness.llm import LlmFailure
from miniharness.llm.pi_ai import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_TOKENS,
    PiAiAdapter,
    SUPPORTED_PROTOCOLS,
    resolve_profiles,
)


class ProfileResolutionTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(resolve_profiles(None), {})

    def test_array_rejected(self):
        with self.assertRaises(ValueError):
            resolve_profiles([{"id": "x"}])

    def test_basic_profile(self):
        profiles = resolve_profiles({
            "acme": {"baseURL": "https://acme.example.com",
                     "api": "anthropic-messages",
                     "apiKeyEnv": "ACME_KEY",
                     "models": [{"id": "m1"}]}})
        profile = profiles["acme"]
        self.assertEqual(profile.api, "anthropic-messages")
        self.assertEqual(profile.base_url, "https://acme.example.com")
        self.assertEqual(profile.api_key_env, "ACME_KEY")
        self.assertEqual(profile.display_name, "acme")
        self.assertEqual(profile.models[0].id, "m1")
        self.assertEqual(profile.default_context_window, DEFAULT_CONTEXT_WINDOW)
        self.assertEqual(profile.default_max_tokens, DEFAULT_MAX_TOKENS)

    def test_defaults_applied(self):
        profiles = resolve_profiles({
            "plain": {"baseURL": "https://x.example.com"}})
        self.assertEqual(profiles["plain"].api, "anthropic-messages")
        self.assertEqual(profiles["plain"].default_input, ("text",))

    def test_unknown_api_rejected(self):
        with self.assertRaises(ValueError):
            resolve_profiles({"x": {"baseURL": "https://x.example.com",
                                    "api": "bogus-protocol"}})

    def test_empty_base_url_rejected(self):
        with self.assertRaises(ValueError):
            resolve_profiles({"x": {"baseURL": ""}})

    def test_removed_field_rejected(self):
        with self.assertRaises(ValueError) as raised:
            resolve_profiles({"x": {"baseURL": "https://x.example.com",
                                    "provider": "deepseek"}})
        self.assertIn("moved to the providers dict key", str(raised.exception))

    def test_empty_display_name_rejected(self):
        with self.assertRaises(ValueError):
            resolve_profiles({"x": {"baseURL": "https://x.example.com",
                                    "displayName": ""}})

    def test_negative_context_rejected(self):
        with self.assertRaises(ValueError):
            resolve_profiles({"x": {"baseURL": "https://x.example.com",
                                    "defaultContextWindow": 0}})

    def test_protocols(self):
        self.assertEqual(set(SUPPORTED_PROTOCOLS),
                         {"openai-completions", "openai-responses",
                          "anthropic-messages"})


class PiAiAdapterTest(unittest.TestCase):
    def setUp(self):
        self.profiles = resolve_profiles({
            "acme": {"baseURL": "https://acme.example.com",
                     "api": "anthropic-messages",
                     "apiKeyEnv": "ACME_KEY",
                     "models": [
                         {"id": "m1", "contextWindow": 8192, "maxTokens": 512},
                         {"id": "m2", "input": ["text", "image"]},
                     ]},
            "openai": {"baseURL": "https://openai.example.com/v1",
                       "api": "openai-completions", "apiKeyEnv": "OPENAI_KEY"},
        })

    def test_default_route(self):
        adapter = PiAiAdapter(self.profiles, default_provider="acme")
        self.assertEqual(adapter.provider, "acme")
        self.assertEqual(adapter.model, "m1")

    def test_resolve_model_info(self):
        adapter = PiAiAdapter(self.profiles, default_provider="acme")
        info = adapter.resolve_model_info()
        self.assertEqual(info["provider"], "acme")
        self.assertEqual(info["model"], "m1")
        self.assertEqual(info["contextWindow"], 8192)
        self.assertEqual(info["defaultMaxTokens"], 512)
        self.assertEqual(info["input_modalities"], ["text"])

    def test_unknown_provider(self):
        adapter = PiAiAdapter(self.profiles, default_provider="acme")
        with self.assertRaises(LlmFailure) as raised:
            adapter.resolve_model_info(provider="nope")
        self.assertEqual(raised.exception.failure["code"], "NO_ADAPTER")

    def test_unknown_model(self):
        adapter = PiAiAdapter(self.profiles, default_provider="acme")
        with self.assertRaises(LlmFailure) as raised:
            adapter.resolve_model_info(model="nope")
        self.assertEqual(raised.exception.failure["code"], "UNKNOWN_MODEL")

    def test_image_modality(self):
        adapter = PiAiAdapter(self.profiles, default_provider="acme")
        info = adapter.resolve_model_info(model="m2")
        self.assertIn("image", info["input_modalities"])

    def test_list_models(self):
        adapter = PiAiAdapter(self.profiles, default_provider="acme")
        models = adapter.list_models()
        self.assertEqual([m.id for m in models], ["m1", "m2"])
        self.assertEqual(models[0].contextWindow, 8192)

    def test_credential_resolution_env(self):
        import os
        os.environ["ACME_KEY"] = "test-key"
        try:
            adapter = PiAiAdapter(self.profiles, default_provider="acme")
            self.assertEqual(adapter.resolve_api_key(self.profiles["acme"]),
                             "test-key")
        finally:
            os.environ.pop("ACME_KEY", None)

    def test_credential_resolution_resolver(self):
        adapter = PiAiAdapter(self.profiles, default_provider="acme",
                              credential_resolver=lambda name: f"resolved-{name}")
        self.assertEqual(adapter.resolve_api_key(self.profiles["acme"]),
                         "resolved-ACME_KEY")

    def test_missing_credential(self):
        adapter = PiAiAdapter(self.profiles, default_provider="acme")
        with self.assertRaises(LlmFailure) as raised:
            adapter.resolve_api_key(self.profiles["acme"])
        self.assertEqual(raised.exception.failure["code"], "MISSING_CREDENTIAL")

    def test_anthropic_body(self):
        from miniharness.llm.pi_ai import _build_anthropic_body
        body = _build_anthropic_body(
            self.profiles["acme"], "m1",
            [{"role": "user", "content": [{"type": "text", "text": "hi"}]}], [])
        self.assertEqual(body["model"], "m1")
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])
        self.assertIn("max_tokens", body)

    def test_openai_body(self):
        from miniharness.llm.pi_ai import _build_openai_body
        body = _build_openai_body(
            self.profiles["openai"], "gpt-x",
            [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            [{"name": "f", "description": "d", "parameters": {"type": "object"}}])
        self.assertEqual(body["model"], "gpt-x")
        self.assertEqual(body["messages"][0]["content"], "hi")
        self.assertEqual(body["tools"][0]["function"]["name"], "f")

    def test_translate_anthropic_events(self):
        from miniharness.llm.pi_ai import _translate_anthropic_event
        chunks = _translate_anthropic_event(
            {"type": "content_block_start",
             "content_block": {"type": "text"}}, "m1")
        self.assertEqual(chunks[0]["type"], "block-start")
        self.assertEqual(chunks[0]["blockType"], "text")
        delta = _translate_anthropic_event(
            {"type": "content_block_delta",
             "delta": {"type": "text_delta", "text": "hello"}}, "m1")
        self.assertEqual(delta[0]["type"], "text-delta")
        self.assertEqual(delta[0]["text"], "hello")
        stop = _translate_anthropic_event(
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, "m1")
        self.assertEqual(stop[0]["type"], "finish")
        self.assertEqual(stop[0]["reason"]["kind"], "stop")
        tool_stop = _translate_anthropic_event(
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}, "m1")
        self.assertEqual(tool_stop[0]["reason"]["kind"], "tool-calls")


if __name__ == "__main__":
    unittest.main()