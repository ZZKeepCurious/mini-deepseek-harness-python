"""DeepSeekAdapter image-capable 请求路径端到端（Files API file-id → wire）。

对照上游 adapter.spec.ts 的 image 请求段落：模型目录宣称 image → 走
requestImages 准备 + Files 解析 + serializeMessagesWithImages；解析失败整请求
回退 inline base64；模型不接受图片则 UNSUPPORTED_CONTENT。
"""
import asyncio
import hashlib
import io
import json
import os
import tempfile
import unittest

import httpx
from PIL import Image

from miniharness.attachment import LocalAttachmentStore, SaveImageAttachment
from miniharness.llm import DeepSeekAdapter, LlmFailure


def _png(width=800, height=800):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _multipart_file_bytes(request):
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    for part in request.content.split(b"--" + boundary):
        if b'name="file"' in part:
            index = part.find(b"\r\n\r\n")
            data = part[index + 4:]
            return data[:-2] if data.endswith(b"\r\n") else data
    return b""


class _FilesServer:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.uploaded = b""

    def handler(self, request):
        self.calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/anthropic/v1/files":
            if self.fail:
                return httpx.Response(500, json={"error": {"message": "boom"}})
            self.uploaded = _multipart_file_bytes(request)
            return httpx.Response(200, json={
                "id": "file-1", "type": "file", "mime_type": "image/png",
                "size_bytes": len(self.uploaded), "created_at": "1970-01-01T00:16:40Z",
                "filename": "n"})
        if request.method == "GET" and request.url.path == "/anthropic/v1/files":
            return httpx.Response(200, json={
                "data": [], "has_more": False, "first_id": None, "last_id": None})
        if request.method == "DELETE":
            return httpx.Response(200, json={"id": "file-1", "type": "file_deleted"})
        return httpx.Response(404, json={"error": {"message": "not found"}})


def _anthropic_sse(text="ok"):
    lines = (
        ['event: message_start',
         'data: ' + json.dumps({'type': 'message_start', 'message': {}}), '',
         'event: content_block_start',
         'data: ' + json.dumps({'type': 'content_block_start', 'index': 0,
                                'content_block': {'type': 'text', 'text': ''}}), '',
         'event: content_block_delta',
         'data: ' + json.dumps({'type': 'content_block_delta', 'index': 0,
                                'delta': {'type': 'text_delta', 'text': text}}), '',
         'event: content_block_stop',
         'data: ' + json.dumps({'type': 'content_block_stop', 'index': 0}), '',
         'event: message_delta',
         'data: ' + json.dumps({'type': 'message_delta',
                                'delta': {'stop_reason': 'end_turn'}}), '',
         'event: message_stop',
         'data: ' + json.dumps({'type': 'message_stop'}), ''])
    return ('\n'.join(lines) + '\n\n').encode()


class _ChatServer:
    def __init__(self):
        self.bodies = []

    def handler(self, request):
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_anthropic_sse())


def _adapter(store, files, chat, model="deepseek-flash"):
    return DeepSeekAdapter(
        api_key="sk-test",
        model=model,
        transport=httpx.MockTransport(chat.handler),
        attachments=lambda: store,
        files_transport=httpx.MockTransport(files.handler),
    )


class DeepSeekAdapterImagesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="mini-adapter-image-")
        self.store = LocalAttachmentStore(root=self._tmp)
        self.ref = self.store.save_image(
            SaveImageAttachment(data=_png(), mediaType="image/png", name="p.png"))
        self.image_block = {"type": "image", "attachment": self.ref.to_dict()}

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, adapter, messages):
        async def drain():
            return [chunk async for chunk in adapter.stream(messages, [])]
        return asyncio.run(drain())

    def test_image_request_uses_files_file_id(self):
        files = _FilesServer()
        chat = _ChatServer()
        adapter = _adapter(self.store, files, chat)
        chunks = self._run(adapter, [{"role": "user",
                                      "content": [{"type": "text", "text": "look"},
                                                  self.image_block]}])
        self.assertEqual([c["type"] for c in chunks if c["type"] == "finish"], ["finish"])
        body = chat.bodies[0]
        image_part = next(p for p in body["messages"][0]["content"]
                          if isinstance(p, dict) and p["type"] == "image")
        self.assertEqual(image_part["source"], {"type": "file", "file_id": "file-1"})
        # 上传字节是请求版本（可能被重新编码），摘要只断言非空
        self.assertTrue(len(files.uploaded) > 0)

    def test_files_failure_falls_back_to_inline_base64(self):
        files = _FilesServer(fail=True)
        chat = _ChatServer()
        adapter = _adapter(self.store, files, chat)
        self._run(adapter, [{"role": "user", "content": [self.image_block]}])
        body = chat.bodies[0]
        image_part = next(p for p in body["messages"][0]["content"]
                          if isinstance(p, dict) and p["type"] == "image")
        self.assertEqual(image_part["source"]["type"], "base64")
        self.assertEqual(image_part["source"]["media_type"], "image/png")

    def test_text_unsupported_model_rejects_images(self):
        files = _FilesServer()
        chat = _ChatServer()
        adapter = _adapter(self.store, files, chat, model="deepseek-v4-flash")
        with self.assertRaises(LlmFailure) as cm:
            self._run(adapter, [{"role": "user", "content": [self.image_block]}])
        self.assertEqual(cm.exception.code, "UNSUPPORTED_CONTENT")
        self.assertEqual(files.calls, [])

    def test_no_attachment_service_rejects_images(self):
        files = _FilesServer()
        chat = _ChatServer()
        adapter = DeepSeekAdapter(
            api_key="sk-test", model="deepseek-flash",
            transport=httpx.MockTransport(chat.handler),
            files_transport=httpx.MockTransport(files.handler))
        with self.assertRaises(LlmFailure) as cm:
            self._run(adapter, [{"role": "user", "content": [self.image_block]}])
        self.assertEqual(cm.exception.code, "UNSUPPORTED_CONTENT")

    def test_model_info_reflects_catalog(self):
        adapter = DeepSeekAdapter(api_key="sk-test", model="deepseek-flash")
        self.assertIn("image", adapter.resolve_model_info()["input_modalities"])
        plain = DeepSeekAdapter(api_key="sk-test", model="deepseek-chat")
        self.assertEqual(plain.resolve_model_info()["input_modalities"], ["text"])

    def test_image_request_pricing(self):
        adapter = DeepSeekAdapter(api_key="sk-test", model="deepseek-flash")
        pricing = adapter.image_request_pricing("deepseek-flash")
        prices = pricing.price_images([{"type": "image", "attachment": self.ref.to_dict()}])
        self.assertGreater(prices[0].visualTokens, 0)
        self.assertIn("request preview", prices[0].text)
        text_only = adapter.image_request_pricing("deepseek-chat")
        stripped = text_only.price_images([{"type": "image", "attachment": self.ref.to_dict()}])
        self.assertEqual(stripped[0].visualTokens, 0)


if __name__ == "__main__":
    unittest.main()
