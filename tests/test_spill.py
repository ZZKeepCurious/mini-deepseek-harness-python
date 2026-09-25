"""spill 域验收（对齐 packages/spill/{spill,spill-local,spill-policy}）。

覆盖：路径段编码/本地存储、溢出提示格式化与识别（含整图省略）、
retain_content 的有序 text/image 保留，以及 tools/post-execute 溢出策略
（token 预算 + 图片整块保留 + 嵌套 PTC 省略图片的 additionalContexts 重注）。
"""
import json
import os
import pathlib
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.tools import ToolExec, ToolRegistry
from miniharness.spill import (
    LocalSpillStore,
    describe_omitted,
    encode_segment,
    format_spill_notice,
    has_spill_notice,
    install_local_spill,
    install_spill_policy,
    retain_content,
    session_dir,
)
from miniharness.spill import SpillRef


def _text(value: str) -> dict:
    return {"type": "text", "text": value}


def _image(name: str = "a") -> dict:
    return {"type": "image", "attachment": {
        "attachmentId": "sha256:" + name * 64, "mediaType": "image/png",
        "bytes": 1, "width": 1, "height": 1,
    }}


def _length_price(block: dict) -> int:
    return len(block["text"]) if block["type"] == "text" else 2


class TestEncodeSegment(unittest.TestCase):
    def test_safe_and_injective(self):
        self.assertEqual(encode_segment("a.b-c_1"), "a.b-c_1")
        self.assertEqual(encode_segment(""), "~")
        self.assertEqual(encode_segment("."), "~002E")
        self.assertEqual(encode_segment(".."), "~002E~002E")
        self.assertNotIn("/", encode_segment("../x"))
        self.assertNotIn(os.sep, encode_segment("a/b"))
        self.assertNotEqual(encode_segment("~002E"), encode_segment("."))


class TestLocalSpillStore(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.ctx = Context(name="root")
        self.store = LocalSpillStore(self.ctx, root=self.root)

    def tearDown(self):
        self.ctx.dispose()
        self._tmp.cleanup()

    async def test_save_text_private_scoped_file(self):
        ref = await self.store.save_text({
            "owner": {"sessionId": "s1"},
            "source": {"kind": "tool", "toolName": "web_fetch", "callId": "c1", "label": "result"},
            "suggestedName": "web_fetch.txt",
            "content": "hello spill",
        })
        self.assertEqual(ref.bytes, len("hello spill".encode("utf-8")))
        self.assertTrue(ref.locator.startswith(session_dir(self.root, "s1")))
        self.assertIn("web_fetch.txt", ref.locator)
        self.assertEqual(pathlib.Path(ref.locator).read_text(encoding="utf-8"), "hello spill")
        self.assertIn("retrieve the full result", ref.retrieval_hint)

    async def test_requires_owner_session(self):
        with self.assertRaisesRegex(ValueError, "sessionId"):
            await self.store.save_text({"owner": {}, "content": "x"})

    def test_install_idempotent(self):
        ctx = Context(name="install")
        try:
            first = install_local_spill(ctx, root=self.root)
            self.assertIs(ctx.get("spillStore"), first)
            self.assertIs(install_local_spill(ctx), first)
        finally:
            ctx.dispose()


# ---------- 溢出提示（notice.ts） ----------

class TestSpillNotice(unittest.TestCase):
    REF = SpillRef(locator="/spill/output.txt", bytes=0, retrieval_hint="Read the file.")

    def test_historical_spelling(self):
        notice = format_spill_notice({"kind": "exact", "count": 50000}, self.REF)
        self.assertEqual(
            notice,
            "(Omitted 50000 bytes. Full formatted result stored at: /spill/output.txt. Read the file.)",
        )
        self.assertTrue(has_spill_notice(notice))
        self.assertTrue(has_spill_notice(f"failed\n[exit code: 7]\n\n{notice}"))

    def test_recognizes_every_omission_form(self):
        for omitted in ({"kind": "none"}, {"kind": "unknown"}, {"kind": "exact", "count": 0}):
            notice = format_spill_notice(omitted, self.REF)
            self.assertTrue(has_spill_notice(notice))
            self.assertTrue(has_spill_notice(f"preview\n\n{notice}"))

    def test_image_omission_count(self):
        notice = format_spill_notice({"kind": "exact", "count": 12}, self.REF, 2)
        self.assertIn("Omitted 12 bytes. Omitted 2 images.", notice)
        self.assertTrue(has_spill_notice(notice))
        # 非规范计数（前导零 / 超出安全整数）不识别
        self.assertFalse(has_spill_notice(notice.replace("2 images", "02 images")))
        self.assertFalse(has_spill_notice(notice.replace("2 images", "9007199254740992 images")))

    def test_opaque_locator_and_retrieval(self):
        ref = SpillRef(locator="/spill/报告 (1).txt", bytes=0,
                       retrieval_hint="Read it.\n\n(additional guidance)")
        notice = format_spill_notice({"kind": "exact", "count": 42}, ref)
        self.assertTrue(has_spill_notice(notice))
        self.assertTrue(has_spill_notice(f"preview\n\n{notice}"))

    def test_rejects_ordinary_partial_and_malformed(self):
        notice = format_spill_notice({"kind": "exact", "count": 42}, self.REF)
        for text in (
            "", "ordinary output)", "(ordinary output)", "\n\n(ordinary output)",
            "prefix" + notice, notice[:-1], notice + "\nother output",
            notice.replace("42", "-1"), notice.replace("42", "1.5"),
            notice.replace("42", "0042"), notice.replace("Omitted", "Ignored"),
            notice.replace(". Read the file.", ""),
        ):
            self.assertFalse(has_spill_notice(text), text)

    def test_describe_omitted_forms(self):
        self.assertEqual(describe_omitted({"kind": "none"}, "bytes"), "")
        self.assertEqual(describe_omitted({"kind": "unknown"}, "bytes"), "More bytes were omitted.")
        self.assertEqual(describe_omitted({"kind": "exact", "count": 3}, "bytes"), "Omitted 3 bytes.")


# ---------- retain_content（retention.ts） ----------

class TestRetainContent(unittest.TestCase):
    def test_keeps_both_images_around_truncated_middle(self):
        first = _image("a")
        last = _image("b")
        result = retain_content(
            [_text("AA"), first, _text("B1B2B3B4B5"), last, _text("CC")], 12, _length_price)
        self.assertEqual(result, {
            "head": [_text("AA"), first, _text("B1")],
            "tail": [_text("B5"), last, _text("CC")],
            "omittedBytes": 6, "omittedImages": 0,
        })

    def test_omits_middle_image_with_surrounding_text(self):
        result = retain_content(
            [_text("A" * 20), _image("a"), _text("C" * 20)], 10, _length_price)
        self.assertEqual(result, {
            "head": [_text("AAAAA")], "tail": [_text("CCCCC")],
            "omittedBytes": 30, "omittedImages": 1,
        })

    def test_does_not_cross_image_that_cannot_fit_wholly(self):
        result = retain_content(
            [_text("A"), _image("a"), _text("B")], 6,
            lambda block: 5 if block["type"] == "image" else _length_price(block))
        self.assertEqual(result, {
            "head": [_text("A")], "tail": [_text("B")],
            "omittedBytes": 0, "omittedImages": 1,
        })

    def test_never_duplicates_overlapping_ends(self):
        result = retain_content([_text("AB"), _image("a"), _text("CD")], 100, _length_price)
        self.assertEqual([*result["head"], *result["tail"]],
                         [_text("AB"), _image("a"), _text("CD")])
        self.assertEqual(result["omittedBytes"], 0)
        self.assertEqual(result["omittedImages"], 0)

    def test_omits_everything_when_budget_zero(self):
        result = retain_content([_text("雪"), _image("a")], 0, _length_price)
        self.assertEqual(result, {
            "head": [], "tail": [], "omittedBytes": 3, "omittedImages": 1,
        })

    def test_minimum_framing_cost_exceeds_remaining_budget(self):
        result = retain_content(
            [_text("ABC")], 4,
            lambda block: block["text"].__len__() + 4 if block["type"] == "text" else 2)
        self.assertEqual(result, {
            "head": [], "tail": [], "omittedBytes": 3, "omittedImages": 0,
        })


# ---------- 溢出策略（tools/post-execute） ----------

class _Agent:
    def __init__(self, session, adapter=None, options=None):
        self.session = session
        self.adapter = adapter
        self.options = options or {}


class _Tool:
    name = "web_fetch"


class _Price:
    def __init__(self, visual_tokens: int, text: str):
        self.visualTokens = visual_tokens
        self.text = text


class _ImagePricing:
    def price_images(self, images):
        return [_Price(2, "image handle") for _ in images]


class _ImageAdapter:
    provider = "fake"
    model = "fake-model"

    def image_request_pricing(self, model):
        return _ImagePricing()


class _Attachments:
    def image_host_path(self, ref):
        return f"/host/{ref.attachmentId}"


class _Fs:
    def process_path_from_host_path(self, host_path):
        return f"/proc{host_path}"


class TestSpillPolicy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.ctx = Context(name="root")
        ToolRegistry(self.ctx)
        self.store = LocalSpillStore(self.ctx, root=self.root)
        self.session = Session("s1", meta={})
        self.exec = ToolExec(agent=_Agent(self.session))

    def tearDown(self):
        self.ctx.dispose()
        self._tmp.cleanup()

    def _decision(self, blocks: list) -> dict:
        return {"kind": "accept", "content": blocks}

    def _run(self, blocks: list, parent=None) -> dict:
        exec_ = self.exec if parent is None else ToolExec(agent=self.exec.agent, parent=parent)
        return self.ctx.waterfall("tools/post-execute",
                                  {"exec": exec_, "tool": _Tool()},
                                  base=lambda payload: self._decision(blocks))

    def test_oversized_result_spilled(self):
        # 预算须高于 worst notice 自身的 token 价（否则上游策略保持内联）
        install_spill_policy(self.ctx, max_inline_tokens=200)
        result = self._run([_text("x" * 4000)])
        text = result["content"][0]["text"]
        self.assertTrue(has_spill_notice(text))
        self.assertIn("Omitted", text)

    def test_small_result_untouched(self):
        install_spill_policy(self.ctx, max_inline_tokens=1000)
        result = self._run([_text("tiny")])
        self.assertEqual(result["content"], [_text("tiny")])

    def test_no_cap_registers_nothing(self):
        install_spill_policy(self.ctx, max_inline_tokens=None)
        big = [_text("y" * 100)]
        result = self._run(big)
        self.assertEqual(result["content"], big)

    def test_negative_cap_rejected(self):
        with self.assertRaises(ValueError):
            install_spill_policy(self.ctx, max_inline_tokens=-1)


class TestSpillPolicyImages(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.ctx = Context(name="root")
        ToolRegistry(self.ctx)
        self.store = LocalSpillStore(self.ctx, root=self.root)
        self.ctx.provide("attachments", _Attachments())
        self.ctx.provide("fs", _Fs())
        self.session = Session("s1", meta={})
        self.agent = _Agent(self.session, adapter=_ImageAdapter(),
                            options={"provider": "fake", "model": "fake-model"})

    def tearDown(self):
        self.ctx.dispose()
        self._tmp.cleanup()

    def _run(self, blocks: list, parent=None, exec_=None) -> dict:
        exec_ = exec_ or ToolExec(agent=self.agent, parent=parent, call_id="call-1")
        return self.ctx.waterfall("tools/post-execute",
                                  {"exec": exec_, "tool": _Tool()},
                                  base=lambda payload: {"kind": "accept", "content": blocks})

    def test_end_images_retained_whole(self):
        original = [_text("A"), _image("a"), _text("B" * 20000), _image("b"), _text("C")]
        install_spill_policy(self.ctx, max_inline_tokens=1000)
        result = self._run(original)
        self.assertEqual([block["type"] for block in result["content"]],
                         ["text", "image", "text", "image", "text"])
        saved = pathlib.Path(self._find_locator(result["content"])).read_text(encoding="utf-8")
        self.assertIn("B" * 20000, saved)
        self.assertIn("[Image:", saved)

    def test_middle_image_omitted_whole(self):
        install_spill_policy(self.ctx, max_inline_tokens=200)
        result = self._run([_text("A" * 4000), _image("a"), _text("C" * 4000)])
        content = result["content"]
        self.assertTrue(all(block["type"] == "text" for block in content))
        self.assertIn("Omitted 1 images.", json.dumps(content))
        saved = pathlib.Path(self._find_locator(content)).read_text(encoding="utf-8")
        self.assertIn("[Image:", saved)

    def test_nested_ptc_omitted_image_reinjected_as_context(self):
        install_spill_policy(self.ctx, max_inline_tokens=200)
        outer = ToolExec(agent=self.agent, call_id="outer")
        result = self._run([_text("A" * 4000), _image("a"), _text("C" * 4000)],
                           parent=outer)
        self.assertFalse(any(block["type"] == "image" for block in result["content"]))
        contexts = result["additionalContexts"]
        self.assertEqual(len(contexts), 1)
        context = contexts[0]
        self.assertEqual(context["role"], "user")
        self.assertEqual(context["source"], {"kind": "ptc-mode"})
        self.assertTrue(all(block["type"] == "text" for block in context["content"]))
        self.assertIn("Omitted 1 images.", json.dumps(context["content"]))

    def test_top_level_omitted_image_has_no_context(self):
        install_spill_policy(self.ctx, max_inline_tokens=200)
        result = self._run([_text("A" * 4000), _image("a"), _text("C" * 4000)])
        self.assertNotIn("additionalContexts", result)

    def test_no_image_calculator_keeps_inline(self):
        install_spill_policy(self.ctx, max_inline_tokens=200)
        original = [_text("A" * 4000), _image("a")]
        # 适配器无 image_request_pricing → 计价失败 → 保留原内容
        exec_ = ToolExec(agent=_Agent(self.session, adapter=None), call_id="call-1")
        result = self._run(original, exec_=exec_)
        self.assertEqual(result["content"], original)

    def test_unreadable_image_path_keeps_inline(self):
        original = [_text("A" * 4000), _image("a")]
        ctx = Context(name="bare")  # 无 attachments/fs 服务 → 图片地址不可解析
        try:
            ToolRegistry(ctx)
            LocalSpillStore(ctx, root=self.root)
            install_spill_policy(ctx, max_inline_tokens=200)
            session = Session("s2", meta={})
            agent = _Agent(session, adapter=_ImageAdapter(),
                           options={"provider": "fake", "model": "fake-model"})
            exec_ = ToolExec(agent=agent, call_id="call-1")
            result = ctx.waterfall("tools/post-execute", {"exec": exec_, "tool": _Tool()},
                                   base=lambda payload: {"kind": "accept", "content": original})
            self.assertEqual(result["content"], original)
        finally:
            ctx.dispose()

    def _find_locator(self, content: list) -> str:
        text = "".join(block.get("text", "") for block in content)
        marker = "Full formatted result stored at: "
        start = text.index(marker) + len(marker)
        end = text.index(". ", start)
        return text[start:end]


if __name__ == "__main__":
    unittest.main()
