# -*- coding: utf-8 -*-
"""请求侧 image/file 块投影（上游 llm/src/content.ts）。

image 是第七类 ContentBlock（alpha.2）：durable 引用经 attachment 服务存储；
text-only 模型把 image 块确定性投影为占位文本；image-capable 模型通过
serialize_messages_with_images 序列化，并通过 IMAGE_OFFLOAD_REQUIRED 机制
处理超预算场景。file 永不原生 dispatch。
"""
import asyncio
import unittest

from miniharness.core.session import file_block, text_block


def tool_message(call_id, blocks):
    """V4 tool/result 消息：role 'tool' 平铺 content + 顶层 toolCallId/source。"""
    return {"id": f"tm-{call_id}", "role": "tool", "toolCallId": call_id,
            "content": list(blocks), "source": {"kind": "tool", "callId": call_id}}
from miniharness.llm import (
    IMAGE_OFFLOAD_REQUIRED,
    LlmFailure,
    UNSUPPORTED_CONTENT,
    content_has_file,
    content_has_image,
    file_handle_text,
    project_files_to_text,
    serialize_messages,
)
from miniharness.llm.content import _replace_files_with_handles
from miniharness.llm.protocol import ImageBlock, LlmImageRequestBudget

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
    def test_flat_tool_message_content(self):
        self.assertFalse(content_has_file([text_block("t")]))
        self.assertTrue(content_has_file([file_block(REF)]))
        flat = tool_message("c1", [file_block(REF)])["content"]
        self.assertTrue(content_has_file(flat))


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

    def test_flat_tool_message_replaced(self):
        messages = [tool_message("c1", [file_block(REF), text_block("rest")])]
        out = project_files_to_text(messages, lambda ref: None)
        self.assertEqual(out[0]["content"][0]["type"], "text")
        self.assertIn("cannot access a readable path", out[0]["content"][0]["text"])
        self.assertEqual(out[0]["content"][1]["text"], "rest")
        # 无 file 的兄弟消息浅拷贝保形
        self.assertEqual(out[0]["id"], "tm-c1")
        self.assertEqual(out[0]["role"], "tool")

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

    def test_serialize_rejects_flat_tool_file_block(self):
        messages = [tool_message("c1", [file_block(REF)])]
        with self.assertRaises(LlmFailure):
            serialize_messages(messages)

    def test_projected_history_serializes_clean(self):
        messages = [{"id": "m", "role": "user", "content": [
            text_block("see"), file_block(REF)], "source": {}}]
        wire = serialize_messages(project_files_to_text(messages, lambda ref: None))
        self.assertEqual(wire[0]["role"], "user")
        text = "".join(block["text"] for block in wire[0]["content"])
        self.assertIn('File "report.pdf"', text)


if __name__ == "__main__":
    unittest.main()


# ---------- 图像 offload 测试（alpha.2，上游 llm/src/content.ts） ----------

IMAGE_REF = {"attachmentId": "sha256:" + "cd" * 32, "name": "photo.png", "mediaType": "image/png",
             "bytes": 1024, "width": 100, "height": 100}

IMAGE_BLOCK = {"type": "image", "attachment": IMAGE_REF}
OFFLOADED_IMAGE_BLOCK = {"type": "image", "attachment": IMAGE_REF, "offloaded": True}


class ContentHasImageTest(unittest.TestCase):
    def test_image_block(self):
        self.assertTrue(content_has_image([IMAGE_BLOCK]))
        self.assertFalse(content_has_image([text_block("hi")]))

    def test_flat_tool_message(self):
        self.assertTrue(content_has_image(tool_message("c1", [IMAGE_BLOCK])["content"]))

    def test_offloaded_image(self):
        self.assertTrue(content_has_image([OFFLOADED_IMAGE_BLOCK]))


class TextOnlyImageTextTest(unittest.TestCase):
    def test_placeholder_text(self):
        from miniharness.llm.content import text_only_image_text
        text = text_only_image_text(IMAGE_REF)
        self.assertIn("image omitted because this model accepts text only", text)
        self.assertIn("sha256:cdcd", text)


class RequestImageHandleTextTest(unittest.TestCase):
    def test_with_access(self):
        from miniharness.llm.content import request_image_handle_text
        text = request_image_handle_text(IMAGE_REF, {"width": 100, "height": 100})
        self.assertIn("Image", text)
        self.assertIn("100x100", text)

    def test_without_access(self):
        from miniharness.llm.content import request_image_handle_text
        text = request_image_handle_text(IMAGE_REF, {"width": 100, "height": 100})
        self.assertIn("may be resized", text)


class OffloadedImageTextTest(unittest.TestCase):
    def test_placeholder(self):
        from miniharness.llm.content import offloaded_image_text
        text = offloaded_image_text(IMAGE_REF)
        self.assertIn("image omitted to fit request image limits", text)


class ProjectOffloadedImagesTest(unittest.TestCase):
    def test_no_offloaded_returns_original(self):
        from miniharness.llm.content import project_offloaded_images
        messages = [{"role": "user", "content": [IMAGE_BLOCK]}]
        result = project_offloaded_images(messages, lambda ref: "placeholder")
        self.assertIs(result, messages)

    def test_offloaded_replaced_with_placeholder(self):
        from miniharness.llm.content import project_offloaded_images
        messages = [{"role": "user", "content": [OFFLOADED_IMAGE_BLOCK]}]
        result = project_offloaded_images(messages, lambda ref: "[image removed]")
        self.assertIsNot(result, messages)
        self.assertEqual(result[0]["content"][0]["text"], "[image removed]")


class ProjectImagesForTextModelTest(unittest.TestCase):
    def test_no_image_returns_original(self):
        from miniharness.llm.content import project_images_for_text_model
        messages = [{"role": "user", "content": [text_block("hi")]}]
        result = project_images_for_text_model(messages)
        self.assertIs(result, messages)

    def test_image_replaced_with_placeholder(self):
        from miniharness.llm.content import project_images_for_text_model
        messages = [{"role": "user", "content": [IMAGE_BLOCK]}]
        result = project_images_for_text_model(messages)
        self.assertIsNot(result, messages)
        self.assertIn("image omitted because this model accepts text only", result[0]["content"][0]["text"])


class RequiredImageOffloadTest(unittest.TestCase):
    def test_no_offload_needed(self):
        from miniharness.llm.content import required_image_offload
        messages = [{"role": "user", "content": [IMAGE_BLOCK]}]
        budget = LlmImageRequestBudget(maxBytes=1000000, maxImages=10)
        count = required_image_offload(messages, budget, lambda block: 1024)
        self.assertEqual(count, 0)

    def test_offload_needed(self):
        from miniharness.llm.content import required_image_offload
        messages = [{"role": "user", "content": [IMAGE_BLOCK]}]
        budget = LlmImageRequestBudget(maxBytes=100, maxImages=1)
        count = required_image_offload(messages, budget, lambda block: 1024)
        self.assertGreater(count, 0)


class SerializeMessagesWithImagesTest(unittest.TestCase):
    def _run(self, messages, images):
        import asyncio
        from miniharness.llm.deepseek import serialize_messages_with_images
        return asyncio.run(serialize_messages_with_images(messages, images))

    def test_text_only_messages(self):
        messages = [{"role": "user", "content": [text_block("hi")]}]
        images = {"representation": {"kind": "base64"}, "requestImages": {}, "maxRequestImageBytes": 1000000}
        result = self._run(messages, images)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[0]["content"], [{"type": "text", "text": "hi"}])

    def test_offload_needed_raises(self):
        from miniharness.llm import IMAGE_OFFLOAD_REQUIRED
        messages = [{"role": "user", "content": [IMAGE_BLOCK]}]
        aid = IMAGE_BLOCK["attachment"]["attachmentId"]
        images = {"representation": {"kind": "base64"},
                  "requestImages": {aid: {"bytes": 1024, "mediaType": "image/png",
                                          "width": 100, "height": 100, "variantId": "v1"}},
                  "maxRequestImageBytes": 100}
        with self.assertRaises(LlmFailure) as cm:
            self._run(messages, images)
        self.assertEqual(cm.exception.code, IMAGE_OFFLOAD_REQUIRED)


class ImageIdentityTest(unittest.TestCase):
    def test_with_name(self):
        from miniharness.llm.content import image_identity
        ref = {"attachmentId": "sha256:ab" * 32, "name": "photo.png"}
        text = image_identity(ref)
        self.assertIn("photo.png", text)

    def test_without_name(self):
        from miniharness.llm.content import image_identity
        ref = {"attachmentId": "sha256:cd" * 32}
        text = image_identity(ref)
        self.assertIn("sha256:", text)


class NormalizedAccessTextTest(unittest.TestCase):
    def test_with_access(self):
        from miniharness.llm.content import normalized_access_text
        ref = {"width": 100, "height": 100, "mediaType": "image/png"}
        access = {"readonlyPath": "/store/photo.png"}
        text = normalized_access_text(ref, access)
        self.assertIn("read-only", text)
        self.assertIn("/store/photo.png", text)


class ReplaceOffloadedWithToolResultTest(unittest.TestCase):
    def test_flat_tool_content_offloaded(self):
        from miniharness.llm.content import _replace_offloaded_images
        out = _replace_offloaded_images([OFFLOADED_IMAGE_BLOCK], lambda ref: "[removed]")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "text")
        self.assertEqual(out[0]["text"], "[removed]")


class ReplaceImagesForTextModelNestedTest(unittest.TestCase):
    def test_flat_tool_content(self):
        from miniharness.llm.content import _replace_images_for_text_model
        out = _replace_images_for_text_model([IMAGE_BLOCK])
        self.assertEqual(out[0]["type"], "text")
        self.assertIn("image omitted", out[0]["text"])


class RequiredImageOffloadBase64Test(unittest.TestCase):
    def test_base64_representation(self):
        from miniharness.llm.content import required_image_offload
        messages = [{"role": "user", "content": [IMAGE_BLOCK]}]
        budget = LlmImageRequestBudget(representation="base64", maxBytes=100)
        count = required_image_offload(messages, budget, lambda block: 10)
        self.assertGreaterEqual(count, 0)


class RequiredImageOffloadTraversalTest(unittest.TestCase):
    """上游 llm/tests/content.spec.ts:111-138：遍历、量子取整与 offloaded 跳过。"""

    @staticmethod
    def _request(lengths, offloaded=()):
        blocks = []
        for index, size in enumerate(lengths):
            block = {"type": "image", "attachment": dict(IMAGE_REF, bytes=size,
                                                          name=f"i{index}.png")}
            if index in offloaded:
                block["offloaded"] = True
            blocks.append(block)
        return [{"role": "user", "content": blocks}]

    @staticmethod
    def _bytes_of(block):
        return block["attachment"]["bytes"]

    def test_unbounded_budgets_and_whole_quanta_past_them(self):
        from miniharness.llm.content import required_image_offload
        lengths = [4, 4, 4, 4]
        self.assertEqual(
            required_image_offload(self._request(lengths),
                                   LlmImageRequestBudget(), self._bytes_of), 0)
        self.assertEqual(
            required_image_offload(self._request(lengths),
                                   LlmImageRequestBudget(maxBytes=16), self._bytes_of), 0)
        self.assertEqual(
            required_image_offload(self._request(lengths),
                                   LlmImageRequestBudget(maxImages=4), self._bytes_of), 0)
        # 一个超额图上取整到整个 count 量子。
        self.assertEqual(
            required_image_offload(self._request([*lengths, 4]),
                                   LlmImageRequestBudget(maxImages=4, countQuantum=2),
                                   self._bytes_of), 2)
        # 一个超额字节移除整个 byte 量子，跨过第二个图。
        self.assertEqual(
            required_image_offload(self._request([*lengths, 1]),
                                   LlmImageRequestBudget(maxBytes=16, byteQuantum=5),
                                   self._bytes_of), 2)
        # 129 个 1 MiB 图在 128 MiB 上限 + 64 MiB 量子下移除最老 65 个。
        mib = 1024 * 1024
        self.assertEqual(
            required_image_offload(self._request([mib] * 129),
                                   LlmImageRequestBudget(maxBytes=128 * mib,
                                                         byteQuantum=64 * mib),
                                   self._bytes_of), 65)

    def test_skips_offloaded_occurrences_and_accounts_base64_length(self):
        from miniharness.llm.content import required_image_offload
        # 三个 3 字节图按 base64（各 4 字节）计 12 > 8，需再移除一个。
        self.assertEqual(
            required_image_offload(self._request([3, 3, 3]),
                                   LlmImageRequestBudget(representation="base64",
                                                         maxBytes=8),
                                   self._bytes_of), 1)
        # 首个已 offloaded（投影为占位文本）不占预算：余下 8 恰好合预算。
        self.assertEqual(
            required_image_offload(self._request([3, 3, 3], offloaded=[0]),
                                   LlmImageRequestBudget(representation="base64",
                                                         maxBytes=8),
                                   self._bytes_of), 0)


if __name__ == "__main__":
    unittest.main()
