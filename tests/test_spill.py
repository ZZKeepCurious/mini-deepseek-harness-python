"""spill 域验收（对齐 packages/spill/{spill,spill-local,spill-policy}）。"""
import os
import pathlib
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.core.session.session import Session
from miniharness.core.tools import ToolExec, ToolRegistry
from miniharness.spill import (
    LocalSpillStore,
    encode_segment,
    has_spill_notice,
    install_local_spill,
    install_spill_policy,
    session_dir,
)


class _Agent:
    def __init__(self, session):
        self.session = session


class _Tool:
    name = "web_fetch"


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

    def _decision(self, text: str) -> dict:
        return {"kind": "accept", "content": [{"type": "text", "text": text}]}

    def test_oversized_result_spilled(self):
        install_spill_policy(self.ctx, max_inline_bytes=16)
        big = "x" * 100
        result = self.ctx.waterfall("tools/post-execute",
                                    {"exec": self.exec, "tool": _Tool()},
                                    base=lambda payload: self._decision(big))
        text = result["content"][0]["text"]
        self.assertTrue(has_spill_notice(text))
        self.assertIn("Omitted", text)

    def test_small_result_untouched(self):
        install_spill_policy(self.ctx, max_inline_bytes=1000)
        small = "tiny"
        result = self.ctx.waterfall("tools/post-execute",
                                    {"exec": self.exec, "tool": _Tool()},
                                    base=lambda payload: self._decision(small))
        self.assertEqual(result["content"][0]["text"], "tiny")

    def test_no_cap_registers_nothing(self):
        install_spill_policy(self.ctx, max_inline_bytes=None)
        big = "y" * 100
        result = self.ctx.waterfall("tools/post-execute",
                                    {"exec": self.exec, "tool": _Tool()},
                                    base=lambda payload: self._decision(big))
        self.assertEqual(result["content"][0]["text"], big)


if __name__ == "__main__":
    unittest.main()
