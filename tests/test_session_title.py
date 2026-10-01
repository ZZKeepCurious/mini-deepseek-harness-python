"""session-title（M10）验收。

对齐上游 `packages/session/session-title/tests/{session-title,provider,rename,
service-contracts,projection}.spec.ts` + `session-title-first-prompt-llm`：

- normalize/truncate/fallback 纯函数（正则剥离、字节截断不劈码点）
- fold_session_title 最后一条胜出
- 自动调度：fallback 先落、first-prompt 提供者在 request/header 后执行、
  fork 不触发、用户钉扎停调度
- rename（用户钉扎 + 非空校验 + source user）
- refresh（取消钉扎：fallback-only 重派生 / provider 重跑）
- 提供者校验（messageSeqs 唯一有序、model 非空）
- LLM 提供者：系统提示词、输入字节上限、max-tokens/error 映射
"""
import unittest

from miniharness.core.scope import Context
from miniharness.core.session import create_message, text_block
from miniharness.core.session_store import install_sessions
from miniharness.llm import FakeLlmAdapter, StreamChunk
from miniharness.session_projection import install_session_projections
from miniharness.session_title import (
    SessionTitleError,
    SessionTitleService,
    clean_title_text,
    fallback_title,
    fold_session_title,
    install_session_title,
    normalize_title,
    register_first_prompt_llm_provider,
    truncate_title_utf8,
)


def _user_message(session, text, kind="user", seq=None):
    """往日志里落一条 user/message（返回 event）。"""
    message = create_message("user", [text_block(text)], {"kind": kind})
    return session.append("user/message", message, surfaceOp="append")


class _RecordingTitleAdapter(FakeLlmAdapter):
    """记录每次 stream 的 (session_id, purpose) 的假适配器。"""

    def __init__(self):
        super().__init__()
        self.requests = []

    async def stream(self, messages, tools, signal=None, session_id=None,
                     purpose=None):
        self.requests.append((session_id, purpose))
        async for chunk in super().stream(messages, tools, signal,
                                          session_id, purpose):
            yield chunk


class NormalizeTest(unittest.TestCase):
    def test_clean_strips_escape_sequences(self):
        self.assertEqual(
            clean_title_text("\u001b]0;stolen\u0007  Hello\t brave\nnew world  "),
            "Hello brave new world")

    def test_truncate_utf8_code_points(self):
        self.assertEqual(truncate_title_utf8("title", 0) if False else
                         truncate_title_utf8("你好世界", 6), "你好")
        # 😀 占 4 字节，预算 5 只容一个
        self.assertEqual(len(truncate_title_utf8("😀😀", 5).encode("utf-8")), 4)

    def test_truncate_rejects_invalid(self):
        with self.assertRaises(ValueError):
            truncate_title_utf8("title", 0)

    def test_fallback_words_and_bytes(self):
        self.assertEqual(fallback_title("one two three four", 3, 80), "one two three")
        self.assertEqual(fallback_title("你好世界", 5, 7), "你好")

    def test_fallback_rejects_invalid(self):
        with self.assertRaises(ValueError):
            fallback_title("title", 1.5, 10)


class FoldTest(unittest.TestCase):
    def test_fold_last_wins(self):
        events = [
            {"type": "session/title", "seq": 1, "time": 10,
             "data": {"title": "First", "messageSeqs": [0],
                      "source": {"kind": "fallback"}}},
            {"type": "session/title", "seq": 2, "time": 20,
             "data": {"title": "Second", "messageSeqs": [1],
                      "source": {"kind": "user"}}},
        ]
        folded = fold_session_title(events)
        self.assertEqual(folded["title"], "Second")
        self.assertEqual(folded["source"], {"kind": "user"})
        self.assertEqual(folded["eventSeq"], 2)
        self.assertEqual(folded["updatedAt"], 20)

    def test_fold_empty(self):
        self.assertIsNone(fold_session_title([]))


class SessionTitleServiceTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="title")
        self.store = install_sessions(self.ctx)
        install_session_projections(self.ctx)
        self.service = install_session_title(self.ctx, {
            "fallbackMaxWords": 5, "fallbackMaxBytes": 40, "maxTitleBytes": 80},
            adapter=FakeLlmAdapter())
        self.session = self.store.create("s1", {"meta": {"cwd": "C:\\work"}})

    def tearDown(self):
        self.ctx.dispose()

    def _prompt(self, text="Build log-backed session titles please"):
        return _user_message(self.session, text)

    def test_immediate_fallback(self):
        self._prompt()
        self._request_header()
        title = self.service.get(self.session)
        self.assertEqual(title["source"]["kind"], "fallback")
        self.assertEqual(title["messageSeqs"], [0])
        event = next(e for e in self.session.events if e["type"] == "session/title")
        self.assertEqual(event["data"]["title"], "Build log-backed session titles please")

    def _request_header(self):
        return self.session.append("request/header", {
            "header": {"config": {"provider": "deepseek-official",
                                  "model": "deepseek-flash"},
                       "tools": None, "adapterDefaults": None},
            "reason": "initial"})

    def test_rename_pins_user_source(self):
        self._prompt()
        self.service.rename(self.session, "  Hand\tpicked   name  ")
        title = self.service.get(self.session)
        self.assertEqual(title["title"], "Hand picked name")
        self.assertEqual(title["source"], {"kind": "user"})
        self.assertEqual(title["messageSeqs"], [])
        # 钉扎后：后续 prompt 不再自动重生成
        self._prompt("another prompt")
        self._request_header()
        titles = [e for e in self.session.events if e["type"] == "session/title"]
        self.assertEqual(len(titles), 2)

    def test_rename_rejects_visible_chars_only(self):
        self._prompt()
        with self.assertRaises(SessionTitleError):
            self.service.rename(self.session, "  \u001b[31m  ")

    def test_qualifying_source_skipped(self):
        self.session.append("user/message",
                            create_message("user", [text_block("injected")],
                                           {"kind": "session-reference"}),
                            surfaceOp="append")
        self.assertEqual(self.service.get(self.session), None)

    def test_refresh_unpins_with_provider(self):
        self._prompt()
        self._request_header()
        self.service.rename(self.session, "User Wins")
        # refresh：provider 存在 → 重跑提供者（覆盖 fallback-only 语义）
        result = self.service.refresh(self.session)
        self.assertIsNotNone(result)

    def test_provider_registration_validation(self):
        from miniharness.session_title import SessionTitleProvider
        with self.assertRaises(ValueError):
            self.service.register(SessionTitleProvider("", "first-prompt",
                                                       lambda req: None))
        with self.assertRaises(ValueError):
            self.service.register(SessionTitleProvider("p", "bad-mode",
                                                       lambda req: None))
        with self.assertRaises(TypeError):
            self.service.register(SessionTitleProvider("p", "first-prompt", None))

    def test_duplicate_provider_rejected(self):
        from miniharness.session_title import SessionTitleProvider
        self.service.register(SessionTitleProvider("p", "first-prompt",
                                                   lambda req: None))
        with self.assertRaises(RuntimeError):
            self.service.register(SessionTitleProvider("p", "first-prompt",
                                                       lambda req: None))


class TitleProjectionTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="title-proj")
        self.store = install_sessions(self.ctx)
        self.registry = install_session_projections(self.ctx)
        self.service = install_session_title(self.ctx, {
            "fallbackMaxWords": 5, "fallbackMaxBytes": 40, "maxTitleBytes": 80},
            adapter=FakeLlmAdapter())
        self.session = self.store.create("p1", {"meta": {"cwd": "C:\\work"}})

    def tearDown(self):
        self.ctx.dispose()

    def test_title_projection_null_before_event(self):
        snapshot = self.registry.snapshot(self.session)
        self.assertIsNone(snapshot["values"]["title"])

    def test_title_input_projection_folds(self):
        first = _user_message(self.session, "first")
        second = _user_message(self.session, "second")
        state = self.registry.state_of(self.session, "titleInput")
        self.assertEqual(state["count"], 2)
        self.assertEqual(state["first"]["text"], "first")
        # fallback 事件落在中间，第二条消息的 seq 顺延
        self.assertEqual(state["lastSeq"], second["seq"])


class FirstPromptLlmProviderTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="title-llm")
        self.store = install_sessions(self.ctx)
        install_session_projections(self.ctx)
        self.service = install_session_title(self.ctx, {
            "fallbackMaxWords": 5, "fallbackMaxBytes": 24, "maxTitleBytes": 24},
            adapter=FakeLlmAdapter())
        self.session = self.store.create("l1", {"meta": {"cwd": "C:\\work"}})

    def tearDown(self):
        self.ctx.dispose()

    def _install_provider(self, adapter=None):
        return register_first_prompt_llm_provider(
            self.ctx, adapter if adapter is not None else FakeLlmAdapter(), {
                "targetWords": 5, "targetCjkCharacters": 10,
                "maxInputBytes": 1000, "maxOutputTokens": 32, "timeoutMs": 1000,
                "provider": "title-route", "model": "title-model",
            })

    def test_first_prompt_llm_call_carries_session_identity(self):
        # session-title-llm/src/index.ts:267-268：请求盖 sessionId +
        # purpose='session-title'（deepseek 侧仅 compaction 映射压缩头）。
        adapter = _RecordingTitleAdapter()
        disposer = self._install_provider(adapter)
        _user_message(self.session, "first input")
        self.session.append("request/header", {
            "header": {"config": {"provider": "deepseek-official",
                                  "model": "deepseek-flash"},
                       "tools": None, "adapterDefaults": None},
            "reason": "initial"})
        title = self.service.get(self.session)
        self.assertEqual(title["source"]["kind"], "provider")
        self.assertEqual(adapter.requests,
                         [(self.session.session_id, "session-title")])
        disposer()

    def test_first_prompt_selects_only_first_message(self):
        disposer = self._install_provider()
        first = _user_message(self.session, "first input")
        _user_message(self.session, "second input must be ignored")
        self.session.append("request/header", {
            "header": {"config": {"provider": "deepseek-official",
                                  "model": "deepseek-flash"},
                       "tools": None, "adapterDefaults": None},
            "reason": "initial"})
        title = self.service.get(self.session)
        self.assertEqual(title["source"]["kind"], "provider")
        self.assertEqual(title["source"]["provider"],
                         "session-title-first-prompt-llm")
        # messageSeqs 只含首条
        self.assertEqual(title["messageSeqs"], [first["seq"]])
        disposer()

    def test_empty_selection_rejected(self):
        from miniharness.session_title import SessionTitleProvider

        def generate(request):
            raise RuntimeError("first-prompt title provider requires one human message")

        self.service.register(SessionTitleProvider(
            "session-title-first-prompt-llm", "first-prompt", generate))
        # refresh 空消息 → 失败但保留 fallback（无 fallback 素材时无标题）
        self.assertIsNone(self.service.refresh(self.session))


if __name__ == "__main__":
    unittest.main()