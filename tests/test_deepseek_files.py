"""DeepSeek 请求侧图片/文件基础设施（A 组 #1-3）。

对照上游 `packages/llm/llm-deepseek/tests/{image-tokens,request-pricing,
file-store,files-api,upload-index}.spec.ts` 的逐向量移植。
"""
import asyncio
import hashlib
import json
import os
import tempfile
import unittest

import httpx

from miniharness.attachment.projection import request_image_dimensions
from miniharness.attachment.types import (
    AttachmentId,
    ImageAttachmentRef,
    ImageRequestTarget,
    ImageVariantId,
    RequestImageAttachment,
)
from miniharness.llm.deepseek_files import (
    DeepSeekCatalogModel,
    DeepSeekConnectionOptions,
    DeepSeekFileConnection,
    DeepSeekFilePolicy,
    DeepSeekFileStore,
    DeepSeekFilesClient,
    DeepSeekFilesError,
    DeepSeekUploadIndex,
    DeepSeekUploadRecord,
    FileResolutionFailure,
    ImageWireLocation,
    RequestDefaults,
    RequestFiles,
    deep_seek_file_scope,
    deep_seek_image_request_pricing,
    deep_seek_image_tokens,
    deep_seek_request_image_dimensions,
    is_files_quota_error,
    model_info,
    provider_rejected_file_id,
    resolve_request_image_target,
)
from miniharness.llm.deepseek_files.request_files import (
    detail_names_file_id,
    normalized_image_diagnostic,
    stale_mappings,
)
from miniharness.llm.protocol import ImageBlock, LlmFailure


def _ref(name, width=800, height=800, bytes_=1024):
    digest = hashlib.sha256(name.encode()).hexdigest()
    return ImageAttachmentRef(
        attachmentId=AttachmentId(f"sha256:{digest}"),
        mediaType="image/png", bytes=bytes_, width=width, height=height, name=name)


def _block(ref, offloaded=False):
    return ImageBlock(attachment=ref.to_dict(), offloaded=offloaded)


def _connection(models=()):
    return DeepSeekConnectionOptions(
        baseURL="https://api.deepseek.com", models=tuple(models))


VISION_MODEL = DeepSeekCatalogModel(id="vision", inputModalities=("text", "image"))


class ImageTokensTest(unittest.TestCase):
    def test_provider_published_vectors(self):
        vectors = [
            (100, 100, 184), (544, 544, 184), (640, 480, 206), (800, 800, 422),
            (1024, 768, 496), (1066, 600, 407), (1300, 1300, 994),
            (1920, 1080, 968), (2000, 2000, 994), (5000, 5000, 994),
            (300, 50, 200), (8192, 100, 593), (16, 8192, 590),
        ]
        for width, height, expected in vectors:
            self.assertEqual(deep_seek_image_tokens(width, height), expected,
                             f"{width}x{height}")

    def test_caps_every_image_at_1024(self):
        for width, height in [(2000, 2000), (5000, 5000), (8192, 8192),
                              (16, 8192), (9000, 1), (1, 9000)]:
            self.assertLessEqual(deep_seek_image_tokens(width, height), 1024)

    def test_small_image_scale_up_floor(self):
        self.assertEqual(deep_seek_image_tokens(100, 100),
                         deep_seek_image_tokens(544, 544))

    def test_one_row_and_one_column_grids(self):
        self.assertEqual(deep_seek_image_tokens(9000, 1), 1024)
        self.assertEqual(deep_seek_image_tokens(1, 9000), 1024)

    def test_repeated_projection_convergence(self):
        self.assertEqual(deep_seek_image_tokens(12, 1123), 380)
        self.assertEqual(deep_seek_image_tokens(89, 2076), 254)


class RequestImageDimensionsTest(unittest.TestCase):
    def test_crosses_token_cell_boundary_preserving_aspect(self):
        sent = deep_seek_request_image_dimensions(1224, 1429)
        self.assertEqual((sent.width, sent.height), (1187, 1386))
        self.assertEqual(deep_seek_image_tokens(1224, 1429), 959)
        self.assertEqual(deep_seek_image_tokens(sent.width, sent.height), 992)

    def test_sends_unchanged_when_padded_grid_fits(self):
        for width, height in [(800, 800), (1302, 1302), (8192, 78), (1, 9000)]:
            sent = deep_seek_request_image_dimensions(width, height)
            self.assertEqual((sent.width, sent.height), (width, height))

    def test_downscales_at_solved_long_edge(self):
        for width, height, ew, eh in [
            (1303, 1303, 1302, 1302), (2048, 2048, 1302, 1302),
            (2048, 1024, 1848, 924), (3840, 2160, 1708, 961),
            (1080, 2400, 838, 1862),
        ]:
            sent = deep_seek_request_image_dimensions(width, height)
            self.assertEqual((sent.width, sent.height), (ew, eh), f"{width}x{height}")
            self.assertEqual(deep_seek_image_tokens(sent.width, sent.height),
                             deep_seek_image_tokens(width, height))


class RequestImageTargetTest(unittest.TestCase):
    def test_per_side_cap(self):
        target = resolve_request_image_target(VISION_MODEL, {"width": 8192, "height": 1})
        self.assertEqual((target.width, target.height), (4096, 1))
        self.assertEqual(target.maxBytes, 2 * 1024 * 1024)

    def test_numeric_pixel_budget_override(self):
        model = DeepSeekCatalogModel(
            id="budget", inputModalities=("text", "image"), imagePixelBudget=640_000)
        target = resolve_request_image_target(model, {"width": 4096, "height": 4096})
        self.assertEqual((target.width, target.height), (800, 800))
        self.assertEqual((target.width, target.height),
                         tuple(request_image_dimensions(4096, 4096, 640_000)))

    def test_low_detail_preset(self):
        model = DeepSeekCatalogModel(
            id="low", inputModalities=("text", "image"), imagePixelBudget="low")
        target = resolve_request_image_target(model, {"width": 4096, "height": 4096})
        self.assertEqual(target.width * target.height, 512 * 512)
        self.assertEqual((target.width, target.height),
                         tuple(request_image_dimensions(4096, 4096, 512 * 512)))
        self.assertEqual(deep_seek_image_tokens(target.width, target.height), 184)


class RequestPricingTest(unittest.TestCase):
    def _prices(self, model_id, blocks, connection=None, resolve_access=None):
        conn = connection or _connection([VISION_MODEL])
        return deep_seek_image_request_pricing(conn, model_id, resolve_access).price_images(blocks)

    def test_uncatalogued_model_is_text_only(self):
        image = _ref("photo", 1920, 1080)
        prices = self._prices("unlisted", [_block(image)])
        self.assertEqual(prices[0].visualTokens, 0)
        self.assertIn("image omitted because this model accepts text only", prices[0].text)

    def test_catalogued_text_only_model(self):
        conn = _connection([DeepSeekCatalogModel(id="text-only")])
        image = _ref("photo", 1920, 1080)
        prices = self._prices("text-only", [_block(image)], connection=conn)
        self.assertEqual(prices[0].visualTokens, 0)
        self.assertIn("image omitted", prices[0].text)

    def test_retained_image_priced_by_projected_dimensions(self):
        image = _ref("photo", 1920, 1080)
        prices = self._prices("vision", [_block(image)])
        self.assertEqual(prices[0].visualTokens, 968)
        self.assertIn("request preview 1708x961px", prices[0].text)

    def test_per_side_capped_pricing(self):
        for width, height, tokens in [(8192, 1, 832), (1, 8192, 1024)]:
            image = _ref("thin", width, height)
            prices = self._prices("vision", [_block(image)])
            self.assertEqual(prices[0].visualTokens, tokens, f"{width}x{height}")

    def test_portrait_projection_changes_grid(self):
        image = _ref("portrait", 1224, 1429)
        prices = self._prices("vision", [_block(image)])
        self.assertEqual(prices[0].visualTokens, 992)
        self.assertIn("1187x1386px", prices[0].text)

    def test_offloaded_and_access(self):
        first = _ref("first", 800, 800)
        second = _ref("second", 800, 800)
        prices = self._prices("vision", [_block(first, True), _block(second)],
                              resolve_access=lambda ref: {"readonlyPath": "/world/attachments/photo.png"})
        self.assertEqual(prices[0].visualTokens, 0)
        self.assertIn("image omitted to fit request image limits", prices[0].text)
        self.assertEqual(prices[1].visualTokens, 422)
        self.assertIn("/world/attachments/photo.png", prices[1].text)


def _multipart_file_bytes(request: httpx.Request) -> bytes:
    content_type = request.headers.get("content-type", "")
    boundary = content_type.split("boundary=")[1].encode()
    for part in request.content.split(b"--" + boundary):
        if b'name="file"' in part:
            index = part.find(b"\r\n\r\n")
            data = part[index + 4:]
            if data.endswith(b"\r\n"):
                data = data[:-2]
            return data
    return b""


class _FilesServer:
    """内存 Files API（httpx.MockTransport handler）。"""

    def __init__(self):
        self.files = {}
        self.upload_count = 0
        self.deleted = []
        self.fail_upload_status = None
        self.fail_detail = ""
        self.quota_remaining = 0
        self.pages = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Messages Files API 夹具（客户端根为 messagesApiRoot(baseURL) → /v1/files）。"""
        path = request.url.path
        if not path.startswith("/v1/files"):
            return httpx.Response(404, json={"error": {"message": "not found"}})
        if request.method == "POST":
            if self.fail_upload_status is not None:
                return httpx.Response(self.fail_upload_status, json={
                    "error": {"message": self.fail_detail or "failed",
                              "code": "invalid_request_error", "type": "invalid_request_error"}})
            if self.quota_remaining > 0:
                self.quota_remaining -= 1
                return httpx.Response(400, json={
                    "error": {"message": "storage quota exceeded", "code": "quota"}})
            data = _multipart_file_bytes(request)
            self.upload_count += 1
            file_id = f"file-{self.upload_count}"
            self.files[file_id] = {"bytes": len(data), "created": 1000,
                                   "expires": 1000 + 3600, "filename": "dsh-owned"}
            return httpx.Response(200, json={
                "id": file_id, "type": "file", "mime_type": "image/png",
                "size_bytes": len(data), "created_at": "1970-01-01T00:16:40Z",
                "filename": "dsh-owned"})
        if request.method == "GET" and path == "/v1/files":
            data = [{
                "id": file_id, "type": "file", "mime_type": "image/png",
                "size_bytes": meta["bytes"],
                "created_at": "1970-01-01T00:16:40Z", "filename": meta["filename"],
            } for file_id, meta in self.files.items()]
            return httpx.Response(200, json={"data": data, "has_more": False,
                                             "first_id": None, "last_id": None})
        if request.method == "GET":
            file_id = path.rsplit("/", 1)[1]
            meta = self.files.get(file_id)
            if meta is None:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            return httpx.Response(200, json={
                "id": file_id, "type": "file", "mime_type": "image/png",
                "size_bytes": meta["bytes"], "created_at": "1970-01-01T00:16:40Z",
                "filename": meta["filename"]})
        file_id = path.rsplit("/", 1)[1]
        self.files.pop(file_id, None)
        self.deleted.append(file_id)
        return httpx.Response(200, json={"id": file_id, "type": "file_deleted"})


def _version(data=b"raw-bytes", variant="v1", media="image/png"):
    digest = hashlib.sha256(data).hexdigest()
    return RequestImageAttachment(
        variantId=ImageVariantId(f"sha256:{hashlib.sha256(variant.encode()).hexdigest()}"),
        attachment=ImageAttachmentRef(
            attachmentId=AttachmentId(f"sha256:{digest}"),
            mediaType=media, bytes=len(data), width=1, height=1),
        data=data, mediaType=media, bytes=len(data), width=1, height=1)


class UploadIndexTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="mini-upload-index-")
        self.path = os.path.join(self._tmp, "files-v3.json")
        self.index = DeepSeekUploadIndex(self.path)
        self.scope = deep_seek_file_scope("https://api.deepseek.com", "sk-test")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _record(self, variant="sha256:" + "a" * 64, file_id="file-1", expires=10_000):
        return DeepSeekUploadRecord(
            scope=self.scope,
            attachmentId="sha256:" + "b" * 64,
            variantId=variant, fileId=file_id, bytes=10,
            createdAt=1_000, expiresAt=expires)

    def test_commit_get_remove_clear(self):
        committed = self.index.commit(self._record(), now=2_000, refresh_margin_ms=0)
        self.assertTrue(committed.accepted)
        self.assertEqual(self.index.get(self.scope, "sha256:" + "a" * 64, 2_000, 0).fileId, "file-1")
        self.index.remove(self.scope, "sha256:" + "a" * 64, "file-1")
        self.assertIsNone(self.index.get(self.scope, "sha256:" + "a" * 64, 2_000, 0))

    def test_expired_record_not_reusable(self):
        self.index.commit(self._record(expires=1_500), now=1_000, refresh_margin_ms=0)
        self.assertIsNone(self.index.get(self.scope, "sha256:" + "a" * 64, 2_000, 0))

    def test_second_commit_loses_to_existing(self):
        first = self.index.commit(self._record(file_id="file-1"), now=1_000, refresh_margin_ms=0)
        second = self.index.commit(self._record(file_id="file-2"), now=1_000, refresh_margin_ms=0)
        self.assertFalse(second.accepted)
        self.assertEqual(second.record.fileId, "file-1")

    def test_invalid_document_reads_empty(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("not json")
        self.assertIsNone(self.index.get(self.scope, "sha256:" + "a" * 64, 1_000, 0))

    def test_duplicate_mappings_rejected(self):
        wire = {"formatVersion": 3, "records": [
            {"scope": str(self.scope), "attachmentId": "sha256:" + "b" * 64,
             "variantId": "sha256:" + "a" * 64, "fileId": "f1", "bytes": 1,
             "createdAt": 1, "expiresAt": 2},
            {"scope": str(self.scope), "attachmentId": "sha256:" + "b" * 64,
             "variantId": "sha256:" + "a" * 64, "fileId": "f2", "bytes": 1,
             "createdAt": 1, "expiresAt": 2},
        ]}
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(wire, handle)
        self.assertIsNone(self.index.get(self.scope, "sha256:" + "a" * 64, 1, 0))


class FilesClientTest(unittest.TestCase):
    """Messages Files API：/v1/files 路径、x-api-key 头、ISO 时间、after_id 游标。"""

    def _client(self, handler):
        return DeepSeekFilesClient(
            baseURL="https://api.deepseek.com", apiKey="sk-test",
            transport=httpx.MockTransport(handler))

    def test_upload_list_delete_roundtrip(self):
        server = _FilesServer()
        client = self._client(server.handler)

        async def run():
            uploaded = await client.upload(
                data=b"hello", mediaType="image/png", filename="dsh-x.png",
                expiresAfterSeconds=3600)
            self.assertEqual(uploaded.bytes, 5)
            page = await client.list(limit=10)
            self.assertTrue(page.hasMore is False)
            self.assertEqual(len(page.data), 1)
            await client.delete(uploaded.id)

        asyncio.run(run())
        self.assertEqual(server.deleted, ["file-1"])

    def test_error_mapping_and_quota_detection(self):
        server = _FilesServer()
        server.fail_upload_status = 429
        server.fail_detail = "storage quota exceeded"
        client = self._client(server.handler)

        async def run():
            with self.assertRaises(DeepSeekFilesError) as cm:
                await client.upload(data=b"x", mediaType="image/png",
                                    filename="x.png", expiresAfterSeconds=3600)
            self.assertEqual(cm.exception.code, "RATE_LIMIT")
            self.assertTrue(is_files_quota_error(cm.exception))

        asyncio.run(run())

    def test_upload_uses_v1_path_and_derives_expiry(self):
        captured = {}

        def handler(request):
            captured["path"] = request.url.path
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={
                "id": "file-ms", "type": "file", "mime_type": "image/png",
                "size_bytes": 3, "created_at": "1970-01-01T00:16:40Z",
                "filename": "n.png"})

        client = self._client(handler)
        result = asyncio.run(client.upload(
            data=b"abc", mediaType="image/png", filename="n.png",
            expiresAfterSeconds=3600))
        self.assertEqual(captured["path"], "/v1/files")
        self.assertIn("x-api-key", captured["headers"])
        self.assertEqual(result.bytes, 3)
        self.assertEqual(result.expiresAt, result.createdAt + 3600)

    def test_list_and_retrieve_and_delete(self):
        def handler(request):
            if request.method == "GET" and request.url.path == "/v1/files":
                return httpx.Response(200, json={
                    "data": [{"id": "f1", "type": "file", "mime_type": "image/png",
                              "size_bytes": 1, "created_at": "1970-01-01T00:00:01Z",
                              "filename": "a.png"}],
                    "has_more": True, "first_id": None, "last_id": "f1"})
            if request.method == "GET":
                return httpx.Response(200, json={
                    "id": "f1", "type": "file", "mime_type": "image/png",
                    "size_bytes": 1, "created_at": "1970-01-01T00:00:01Z",
                    "filename": "a.png"})
            return httpx.Response(200, json={"id": "f1", "type": "file_deleted"})

        client = self._client(handler)
        page = asyncio.run(client.list(limit=1))
        self.assertTrue(page.hasMore)
        self.assertEqual(str(page.lastId), "f1")
        object_ = asyncio.run(client.retrieve("f1"))
        self.assertEqual(object_.bytes, 1)
        asyncio.run(client.delete("f1"))

    def test_invalid_response_shapes_rejected(self):
        def handler(request):
            # Messages list requires data[] + boolean has_more; object tag is not required.
            return httpx.Response(200, json={"data": "not-a-list", "has_more": True})

        client = self._client(handler)
        with self.assertRaises(LlmFailure) as cm:
            asyncio.run(client.list())
        self.assertEqual(cm.exception.code, "INVALID_RESPONSE")


class FileStoreMessagesScopeTest(unittest.TestCase):
    def test_messages_scope_uses_v1_namespace(self):
        tmp = tempfile.mkdtemp(prefix="mini-scope-")
        try:
            server = _FilesServer()
            store = DeepSeekFileStore(
                index=DeepSeekUploadIndex(os.path.join(tmp, "files-v3.json")),
                now=lambda: 1_000_000, transport=httpx.MockTransport(server.handler))
            connection = DeepSeekFileConnection("https://api.deepseek.com", "sk-test")
            policy = DeepSeekFilePolicy(3600, 0, 10)
            result = asyncio.run(store.ensure_uploaded(_version(), connection, policy))
            self.assertEqual(
                str(result.record.scope),
                str(deep_seek_file_scope("https://api.deepseek.com/v1", "sk-test")))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_release_all_clears_namespace(self):
        tmp = tempfile.mkdtemp(prefix="mini-release-all-")
        try:
            server = _FilesServer()
            server.files["file-owned"] = {
                "bytes": 1, "created": 1, "expires": 9999, "filename": "dsh-a"}
            store = DeepSeekFileStore(
                index=DeepSeekUploadIndex(os.path.join(tmp, "files-v3.json")),
                now=lambda: 1_000_000, transport=httpx.MockTransport(server.handler))
            connection = DeepSeekFileConnection("https://api.deepseek.com", "sk-test")
            total = asyncio.run(store.release_all(connection))
            self.assertEqual(total, 1)
            self.assertEqual(server.files, {})
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class FileStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="mini-file-store-")
        self.index = DeepSeekUploadIndex(os.path.join(self._tmp, "files-v3.json"))
        self.server = _FilesServer()
        self.transport = httpx.MockTransport(self.server.handler)
        self.store = DeepSeekFileStore(index=self.index, now=lambda: 1_000_000,
                                       transport=self.transport)
        self.connection = DeepSeekFileConnection(
            baseURL="https://api.deepseek.com", apiKey="sk-test")
        self.policy = DeepSeekFilePolicy(
            expiresAfterSeconds=3600, refreshMarginSeconds=0, quotaCleanupBatch=10)
        self.version = _version()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_upload_then_reuse(self):
        async def run():
            first = await self.store.ensure_uploaded(self.version, self.connection, self.policy)
            self.assertTrue(first.uploaded)
            second = await self.store.ensure_uploaded(self.version, self.connection, self.policy)
            self.assertFalse(second.uploaded)
            self.assertEqual(first.record.fileId, second.record.fileId)

        asyncio.run(run())
        self.assertEqual(self.server.upload_count, 1)

    def test_concurrent_calls_share_one_upload(self):
        async def run():
            results = await asyncio.gather(*[
                self.store.ensure_uploaded(self.version, self.connection, self.policy)
                for _ in range(5)
            ])
            ids = {result.record.fileId for result in results}
            self.assertEqual(len(ids), 1)

        asyncio.run(run())
        self.assertEqual(self.server.upload_count, 1)

    def test_quota_recovery_reclaims_owned_then_uploads(self):
        self.server.files["file-old"] = {
            "bytes": 1, "created": 1, "expires": 9999, "filename": "dsh-old"}
        self.server.quota_remaining = 1
        result = asyncio.run(
            self.store.ensure_uploaded(self.version, self.connection, self.policy))
        self.assertTrue(result.uploaded)
        self.assertIn("file-old", self.server.deleted)

    def test_invalidate_and_release(self):
        async def run():
            first = await self.store.ensure_uploaded(self.version, self.connection, self.policy)
            await self.store.invalidate(self.version, first.record.fileId, self.connection)
            self.assertIsNone(self.index.get(
                first.record.scope, str(self.version.variantId), 1_000_000, 0))
            second = await self.store.ensure_uploaded(self.version, self.connection, self.policy)
            self.assertTrue(second.uploaded)
            released = await self.store.release(self.version, self.connection, self.policy)
            self.assertTrue(released)

        asyncio.run(run())

    def test_oversized_image_rejected(self):
        big = _version(data=b"x" * (32 * 1024 * 1024 + 1))
        with self.assertRaises(LlmFailure) as cm:
            asyncio.run(self.store.ensure_uploaded(big, self.connection, self.policy))
        self.assertEqual(cm.exception.code, "INVALID_REQUEST")


class RequestFilesTest(unittest.TestCase):
    def test_file_id_classification(self):
        self.assertTrue(provider_rejected_file_id("file not found"))
        self.assertTrue(provider_rejected_file_id("invalid file_id"))
        self.assertFalse(provider_rejected_file_id("image too large"))

    def test_detail_names_file_id_word_boundary(self):
        self.assertTrue(detail_names_file_id("file abc123 expired", "abc123"))
        self.assertFalse(detail_names_file_id("file xabc123y invalid", "abc123"))

    def test_stale_mappings_exact_preferred(self):
        from miniharness.llm.deepseek_files.request_files import _UsedRequestFile
        first = _UsedRequestFile(_version(variant="a"), "file-a", ImageWireLocation(1, 1))
        second = _UsedRequestFile(_version(variant="b"), "file-b", ImageWireLocation(1, 2))
        exact = stale_mappings([first, second], "file file-b expired")
        self.assertEqual([f.fileId for f in exact], ["file-b"])
        all_files = stale_mappings([first, second], "expired")
        self.assertEqual(len(all_files), 2)

    def test_diagnostic_names_single_image(self):
        from miniharness.llm.deepseek_files.request_files import _UsedRequestFile
        used = [_UsedRequestFile(_version(variant="a"), "file-a", ImageWireLocation(1, 1))]
        text = normalized_image_diagnostic(used, "bad image", "unsupported image")
        self.assertIn("message 1, image 1", text)
        self.assertIn("PNG, JPEG, WebP, and GIF", text)

    def test_resolve_failure_wraps_transport_error(self):
        import shutil
        tmp = tempfile.mkdtemp(prefix="mini-rf-")
        try:
            server = _FilesServer()
            server.fail_upload_status = 500
            server.fail_detail = "internal error"
            store = DeepSeekFileStore(
                index=DeepSeekUploadIndex(os.path.join(tmp, "files-v3.json")),
                now=lambda: 1_000_000,
                transport=httpx.MockTransport(server.handler))
            connection = DeepSeekFileConnection("https://api.deepseek.com", "sk")
            request_files = RequestFiles(
                store, connection, DeepSeekFilePolicy(3600, 0, 10),
                1000, None, lambda: None)
            with self.assertRaises(FileResolutionFailure):
                asyncio.run(request_files.resolve(_version(), ImageWireLocation(1, 1)))
            self.assertFalse(asyncio.run(request_files.retry("file not found")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FileScopeTest(unittest.TestCase):
    def test_scope_is_deterministic_digest_and_normalizes_trailing_slash(self):
        first = deep_seek_file_scope("https://api.deepseek.com/", "sk-test")
        second = deep_seek_file_scope("https://api.deepseek.com", "sk-test")
        self.assertEqual(str(first), str(second))
        self.assertEqual(len(str(first)), 64)
        self.assertNotEqual(str(first), str(deep_seek_file_scope("https://other", "sk-test")))
        self.assertNotEqual(str(first), str(deep_seek_file_scope("https://api.deepseek.com", "sk-other")))


class ModelInfoTest(unittest.TestCase):
    def test_uncatalogued_is_text_only(self):
        info = model_info(_connection([]), "deepseek-official", "deepseek-chat")
        self.assertEqual(info["input_modalities"], ["text"])
        self.assertEqual(info["model"], "deepseek-chat")

    def test_catalogued_image_model(self):
        info = model_info(_connection([VISION_MODEL]), "deepseek-official", "vision")
        self.assertEqual(info["input_modalities"], ["text", "image"])
        self.assertEqual(info["reasoning"]["defaultEffort"], "high")

    def test_thinking_disabled_off_only(self):
        conn = DeepSeekConnectionOptions(
            baseURL="x",
            defaults=RequestDefaults(thinking="disabled"),
            models=(VISION_MODEL,))
        info = model_info(conn, "p", "vision")
        self.assertEqual([effort["id"] for effort in info["reasoning"]["efforts"]], ["off"])


if __name__ == "__main__":
    unittest.main()
