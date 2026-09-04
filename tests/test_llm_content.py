# -*- coding: utf-8 -*-
"""请求侧 file 块投影（alpha.1，上游 llm/src/content.ts:137-201）。

file 是第六类 ContentBlock：请求组装无条件把 file（含嵌套 tool-result）投影为
fileHandleText 确定性 handle 文本——file 永不原生 dispatch；provider 序列化层
（serialize_messages）对漏网 file 块 last-line 拒绝（UNSUPPORTED_CONTENT）。
"""
import unittest

from miniharness.core.session import file_block, text_block, tool_result_block
from miniharness.llm import (
    UNSUPPORTED_CONTENT,
    LlmFailure,
    content_has_file,
    file_handle_text,
    project_files_to_text,
    serialize_messages,
)
from miniharness.llm.content import _replace_files_with_handles

REF = {"attachmentId": "sha256:" + "ab" * 32, "name": "report.pdf", "bytes": 12}


class FileHandleTextTest(unittest.TestCase):
    def test_identity_and_path_branch(self):
        text = file_handle_text(REF, "/store/report.pdf")
        self.assertIn('File "report.pdf" (12 bytes, sha256:abababab)', text)
        self.assertIn('verbatim read-only copy saved at "/store/report.pdf"', text)
        self.assertIn("Read that path with your file tools", text)
        self.assertIn("only subagents sharing this execution environment can read it", text)

    def test_no_readable_path_branch(self):
        text = file_handle_text(REF, None)
        self.assertIn("cannot access a readable path", text)
        self.assertIn("do not claim to have read it", text)
        self.assertNotIn("verbatim read-only copy", text)


class ContentHasFileTest(unittest.TestCase):
    def test_recursive_tool_result(self):
        self.assertFalse(content_has_file([text_block("t")]))
        self.assertTrue(content_has_file([file_block(REF)]))
        nested = [tool_result_block("c1", [file_block(REF)])]
        self.assertTrue(content_has_file(nested))
        deep = [tool_result_block("c1", [tool_result_block("c2", [file_block(REF)])])]
        self.assertTrue(content_has_file(deep))


class ProjectFilesToTextTest(unittest.TestCase):
    def test_no_file_returns_original_object(self):
        messages = [{"id": "m", "role": "user", "content": [text_block("hi")], "source": {}}]
        self.assertIs(project_files_to_text(messages, lambda ref: None), messages)

    def test_file_block_replaced_with_handle_text(self):
        messages = [{"id": "m", "role": "user", "content": [
            text_block("看这个："), file_block(REF)], "source": {}}]
        out = project_files_to_text(messages, lambda ref: "/store/report.pdf")
        self.assertIsNot(out, messages)
        self.assertEqual(out[0]["content"][0]["type"], "text")
        self.assertEqual(out[0]["content"][1]["type"], "text")
        self.assertIn('File "report.pdf"', out[0]["content"][1]["text"])

    def test_nested_tool_result_replaced(self):
        messages = [{"id": "m", "role": "user", "content": [
            tool_result_block("c1", [file_block(REF), text_block("rest")])], "source": {}}]
        out = project_files_to_text(messages, lambda ref: None)
        result = out[0]["content"][0]
        self.assertEqual(result["type"], "tool-result")
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertIn("cannot access a readable path", result["content"][0]["text"])
        self.assertEqual(result["content"][1]["text"], "rest")
        # 无 file 的兄弟消息浅拷贝保形
        self.assertEqual(out[0]["id"], "m")
        self.assertEqual(out[0]["role"], "user")

    def test_replace_preserves_order_and_non_file_blocks(self):
        blocks = [text_block("a"), {"type": "reasoning", "text": "r"},
                  file_block(REF), text_block("b")]
        out = _replace_files_with_handles(blocks, lambda ref: None)
        self.assertEqual([b["type"] for b in out],
                         ["text", "reasoning", "text", "text"])


class SerializeFileBlockDefenseTest(unittest.TestCase):
    def test_serialize_rejects_file_block(self):
        # 投影漏网的 file 块在 serialize 层 last-line 拒绝（绝不静默剥离）
        messages = [{"id": "m", "role": "user", "content": [file_block(REF)], "source": {}}]
        with self.assertRaises(LlmFailure) as cm:
            serialize_messages(messages)
        self.assertEqual(cm.exception.code, UNSUPPORTED_CONTENT)

    def test_serialize_rejects_nested_file_block(self):
        messages = [{"id": "m", "role": "user", "content": [
            tool_result_block("c1", [file_block(REF)])], "source": {}}]
        with self.assertRaises(LlmFailure):
            serialize_messages(messages)

    def test_projected_history_serializes_clean(self):
        messages = [{"id": "m", "role": "user", "content": [
            text_block("see"), file_block(REF)], "source": {}}]
        wire = serialize_messages(project_files_to_text(messages, lambda ref: None))
        self.assertEqual(wire[0]["role"], "user")
        self.assertIn('File "report.pdf"', wire[0]["content"])


if __name__ == "__main__":
    unittest.main()
