"""附件存储与图片受理测试（attachment 族，第 12 章扩展）。

验证：save_images 批量校验顺序、内容寻址去重、read_image 完整性复验、
错误码全集、尺寸限制、像素限制、媒体类型白名单与声明一致性；rc.2 增量：
规范化管线（直通/降采样/originalDimensions）、canonical base64 受理、
请求图版本（variantId 确定身份、缓存、尺寸投影、策略准入、seam 缺省拒绝）。
"""
import base64
import io
import os
import tempfile
import unittest

from PIL import Image

from miniharness.attachment import (
    ATTACHMENT_CORRUPT,
    ATTACHMENT_NOT_FOUND,
    ATTACHMENT_PROJECTION_UNSUPPORTED,
    IMAGE_DIMENSION_TOO_LARGE,
    IMAGE_TOO_MANY_PIXELS,
    IMAGES_TOO_LARGE,
    IMAGE_TYPE_MISMATCH,
    INVALID_ATTACHMENT_REF,
    INVALID_FILE_BASE64,
    INVALID_IMAGE,
    INVALID_IMAGE_BASE64,
    TOO_MANY_IMAGES,
    UNSUPPORTED_IMAGE_TYPE,
    AttachmentError,
    AttachmentStore,
    EncodedFileAttachment,
    FileAttachmentRef,
    ImageAttachmentRef,
    ImageRequestPolicy,
    LocalAttachmentStore,
    SaveFileAttachment,
    SaveFileStreamAttachment,
    SaveImageAttachment,
    encoded_alpha_is_compatible,
)
from miniharness.attachment.image import detect_image, probe_image
from miniharness.attachment.normalization import NormalizationPolicy
from miniharness.attachment.projection import request_image_dimensions
from miniharness.attachment.request_image import request_image_variant_id
from miniharness.attachment.types import ImageAttachmentLimits

# 1x1 PNG（base64，Pillow 可完整解码的有效光栅）
_ONE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC"
)


def _save(store, data=_ONE_PNG, media_type="image/png", name=None):
    return SaveImageAttachment(data=data, mediaType=media_type, name=name)


def _png_bytes(width: int, height: int, mode: str = "RGB", flat=None) -> bytes:
    """生成无元数据的干净测试光栅；flat 给定时为纯色，否则水平渐变。"""
    image = Image.new(mode, (width, height))
    if flat is None:
        pixels = image.load()
        for x in range(width):
            for y in range(height):
                if mode == "RGB":
                    pixels[x, y] = ((x * 8) % 256, (y * 8) % 256, 128)
                else:
                    pixels[x, y] = ((x * 8) % 256, (y * 8) % 256, 128, 255)
    else:
        image.paste(flat, (0, 0, width, height))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class TestDetectImage(unittest.TestCase):
    def test_detect_png(self):
        detected = detect_image(_ONE_PNG)
        self.assertEqual(detected.media_type, "image/png")
        self.assertEqual((detected.width, detected.height), (1, 1))

    def test_detect_rejects_garbage(self):
        with self.assertRaises(AttachmentError) as cm:
            detect_image(b"not-an-image")
        self.assertEqual(cm.exception.code, INVALID_IMAGE)

    def test_detect_limits_pixels(self):
        with self.assertRaises(AttachmentError) as cm:
            detect_image(_ONE_PNG, max_pixels=0)
        self.assertEqual(cm.exception.code, IMAGE_TOO_MANY_PIXELS)

    def test_detect_limits_dimension(self):
        # rc.2：单边像素上限 → IMAGE_DIMENSION_TOO_LARGE
        with self.assertRaises(AttachmentError) as cm:
            detect_image(_png_bytes(8, 4), max_dimension=4)
        self.assertEqual(cm.exception.code, IMAGE_DIMENSION_TOO_LARGE)

    def test_detect_rejects_broken_pixel_stream(self):
        # 头部合法但像素流损坏：Pillow 全量解码可检出（旧 stdlib 头解析放行）
        broken = bytearray(_ONE_PNG)
        broken[38] ^= 0xFF  # IDAT zlib 载荷内
        with self.assertRaises(AttachmentError) as cm:
            detect_image(bytes(broken))
        self.assertEqual(cm.exception.code, INVALID_IMAGE)

    def test_probe_is_header_only(self):
        detected = probe_image(_ONE_PNG)
        self.assertEqual(detected.media_type, "image/png")

    def test_encoded_alpha_compatibility(self):
        # WebP 允许省略全不透明平面；其余增删不兼容
        self.assertTrue(encoded_alpha_is_compatible(True, "image/webp", False))
        self.assertTrue(encoded_alpha_is_compatible(False, "image/png", False))
        self.assertFalse(encoded_alpha_is_compatible(False, "image/webp", True))
        self.assertIs(encoded_alpha_is_compatible(None, "image/jpeg", False), True)


class TestNormalization(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="mini-attachment-test-")
        self.store = LocalAttachmentStore(root=self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_clean_small_source_passes_through_byte_identically(self):
        data = _png_bytes(4, 4)
        ref = self.store.save_image(_save(self.store, data=data))
        stored = self.store.read_image(ref)
        self.assertEqual(stored.data, data)
        self.assertIsNone(ref.originalDimensions)
        self.assertEqual((ref.width, ref.height), (4, 4))

    def test_oversized_long_edge_is_normalized_with_original_dimensions(self):
        # alpha.1：规制改为总像素预算（maxPixels）→ 长边封顶（maxDimension）。
        # 64×32 在缺省总像素预算内，故按 maxDimension=8 封顶 → (8,4)。
        policy = NormalizationPolicy(maxDimension=8, maxBytes=1_000_000)
        store = LocalAttachmentStore(root=self._tmp, normalization_policy=policy)
        data = _png_bytes(64, 32)
        ref = store.save_image(_save(store, data=data))
        self.assertEqual((ref.width, ref.height), (8, 4))
        self.assertIsNotNone(ref.originalDimensions)
        self.assertEqual(
            (ref.originalDimensions.width, ref.originalDimensions.height), (64, 32)
        )
        stored = store.read_image(ref)
        detected = detect_image(stored.data)
        self.assertEqual(detected.media_type, ref.mediaType)

    def test_unreachable_byte_target_keeps_smallest_ladder_output(self):
        # alpha.1：规范化把 maxBytes 当作编码字节目标，每个阶梯质量都超限时
        # 保留最小阶梯输出（不再抛 IMAGE_TOO_LARGE）。
        policy = NormalizationPolicy(maxDimension=2048, maxBytes=8)
        store = LocalAttachmentStore(root=self._tmp, normalization_policy=policy)
        ref = store.save_image(_save(store, data=_png_bytes(16, 16)))
        self.assertGreater(ref.bytes, 8)
        # 产物仍是合法规范化图片（8-bit sRGB、无元数据、尺寸不变或缩小）
        stored = store.read_image(ref)
        detected = detect_image(stored.data)
        self.assertEqual(detected.media_type, ref.mediaType)
        self.assertFalse(detected.carries_metadata)
        self.assertLessEqual((ref.width, ref.height), (16, 16))

    def test_metadata_carrier_is_reencoded_without_original_dimensions_leak(self):
        from PIL import PngImagePlugin
        meta = PngImagePlugin.PngInfo()
        meta.add_text("comment", "hello")
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4)).save(buffer, format="PNG", pnginfo=meta)
        data = buffer.getvalue()
        ref = self.store.save_image(_save(self.store, data=data))
        # 携带元数据 → 不允许直通；产物无元数据且尺寸不变
        self.assertEqual((ref.width, ref.height), (4, 4))
        stored = self.store.read_image(ref)
        self.assertFalse(detect_image(stored.data).carries_metadata)


class TestAdmitEncodedImages(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="mini-attachment-test-")
        self.store = LocalAttachmentStore(root=self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _encoded(self, data, media_type="image/png", name=None):
        from miniharness.attachment import EncodedImageAttachment
        return EncodedImageAttachment(mediaType=media_type, data=data, name=name)

    def test_happy_path_returns_refs_in_order(self):
        from miniharness.attachment import admit_encoded_images
        refs = admit_encoded_images(self.store, [
            self._encoded(base64.b64encode(_png_bytes(2, 2)).decode()),
            self._encoded(base64.b64encode(_png_bytes(3, 3)).decode(), name="shot.png"),
        ])
        self.assertEqual(len(refs), 2)
        self.assertEqual([r.width for r in refs], [2, 3])
        self.assertEqual(refs[1].name, "shot.png")

    def test_non_canonical_base64_rejected(self):
        from miniharness.attachment import admit_encoded_images
        raw = base64.b64encode(_png_bytes(2, 2))
        padded = raw.decode() + "="  # 非 canonical 追加
        with self.assertRaises(AttachmentError) as cm:
            admit_encoded_images(self.store, [self._encoded(padded)])
        self.assertEqual(cm.exception.code, INVALID_IMAGE_BASE64)


class TestAdmitPromptContent(unittest.TestCase):
    """子代理 prompt 内容图像拒绝门（align 上游 subagent/control.ts admitPromptContent）。"""

    def test_non_image_blocks_pass_through_in_order(self):
        from miniharness.attachment import admit_prompt_content
        blocks = [{"type": "text", "text": "a"}, {"type": "tool-result", "content": "x"}]
        self.assertEqual(admit_prompt_content("child-1", blocks), blocks)

    def test_image_block_refused(self):
        from miniharness.attachment import SubagentImageUnsupportedError, admit_prompt_content
        with self.assertRaises(SubagentImageUnsupportedError) as cm:
            admit_prompt_content("child-1", [{"type": "text", "text": "a"},
                                             {"type": "image", "image": "base64..."}])
        # alpha.1：码统一 subagent/attachment-invalid
        self.assertEqual(cm.exception.code, "subagent/attachment-invalid")
        self.assertEqual(cm.exception.child_session_id, "child-1")

    def test_file_part_refused(self):
        from miniharness.attachment import SubagentFileUnsupportedError, admit_prompt_content
        with self.assertRaises(SubagentFileUnsupportedError) as cm:
            admit_prompt_content("child-1", [{"type": "text", "text": "a"},
                                             {"type": "file", "receiptId": "r1"}])
        self.assertEqual(cm.exception.code, "subagent/attachment-invalid")
        self.assertEqual(cm.exception.reason, "SUBAGENT_FILE_UNSUPPORTED")
        self.assertEqual(cm.exception.child_session_id, "child-1")

    def test_text_only_str_trivially_accepted(self):
        from miniharness.attachment import admit_prompt_content
        self.assertEqual(admit_prompt_content("c", []), [])


class TestRequestImage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="mini-attachment-test-")
        self.store = LocalAttachmentStore(root=self._tmp)
        self.ref = self.store.save_image(_save(self.store, data=_png_bytes(4, 4)))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_base_store_default_rejects_projection(self):
        with self.assertRaises(AttachmentError) as cm:
            AttachmentStore().read_image_request(
                self.ref, ImageRequestPolicy(maxPixels=1024, maxBytes=1024)
            )
        self.assertEqual(cm.exception.code, ATTACHMENT_PROJECTION_UNSUPPORTED)
        self.assertEqual(
            str(cm.exception),
            "The mounted attachment provider cannot derive model-request images.",
        )

    def test_policy_must_be_positive_integers(self):
        with self.assertRaises(AttachmentError) as cm:
            self.store.read_image_request(
                self.ref, ImageRequestPolicy(maxPixels=0, maxBytes=100)
            )
        self.assertEqual(cm.exception.code, INVALID_ATTACHMENT_REF)
        self.assertEqual(str(cm.exception), "Image request maxPixels must be a positive integer.")
        with self.assertRaises(AttachmentError) as cm:
            self.store.read_image_request(
                self.ref, ImageRequestPolicy(maxPixels=10, maxBytes=-1)
            )
        self.assertEqual(str(cm.exception), "Image request maxBytes must be a positive integer.")

    def test_passthrough_when_within_budgets(self):
        version = self.store.read_image_request(
            self.ref, ImageRequestPolicy(maxPixels=10_000, maxBytes=1_000_000)
        )
        self.assertEqual(version.data, _png_bytes(4, 4))
        self.assertEqual(version.mediaType, "image/png")
        self.assertEqual(version.depth, "uchar")
        self.assertEqual(version.space, "srgb")
        self.assertFalse(version.hasAlpha)
        self.assertEqual(str(version.variantId), str(
            request_image_variant_id(self.ref, ImageRequestPolicy(maxPixels=10_000, maxBytes=1_000_000))
        ))

    def test_variant_id_is_deterministic_and_policy_sensitive(self):
        policy = ImageRequestPolicy(maxPixels=10_000, maxBytes=1_000_000)
        other = ImageRequestPolicy(maxPixels=9_999, maxBytes=1_000_000)
        self.assertEqual(
            request_image_variant_id(self.ref, policy),
            request_image_variant_id(self.ref, policy),
        )
        self.assertNotEqual(request_image_variant_id(self.ref, policy), request_image_variant_id(self.ref, other))
        self.assertTrue(str(request_image_variant_id(self.ref, policy)).startswith("sha256:"))

    def test_cache_entry_written_and_reused(self):
        policy = ImageRequestPolicy(maxPixels=4, maxBytes=1_000_000)
        first = self.store.read_image_request(self.ref, policy)
        hash_hex = str(first.variantId)[len("sha256:"):]
        path = os.path.join(self._tmp, "request-images", hash_hex[:2], hash_hex)
        self.assertTrue(os.path.isfile(path))
        second = self.store.read_image_request(self.ref, policy)
        self.assertEqual(second.data, first.data)
        self.assertEqual(second.variantId, first.variantId)

    def test_pixel_budget_projects_dimensions_inward(self):
        version = self.store.read_image_request(
            self.ref, ImageRequestPolicy(maxPixels=2, maxBytes=1_000_000)
        )
        # 上游内收算法：4×4 预算 2px → (1,1)
        self.assertLessEqual(version.width * version.height, 2)
        self.assertEqual((version.width, version.height), (1, 1))
        self.assertEqual(
            request_image_dimensions(4, 4, 2), (1, 1)
        )

    def test_unreachable_byte_target_keeps_smallest_ladder_output(self):
        # alpha.1：请求图把 maxBytes 当编码字节目标，每个阶梯质量都超限时
        # 保留最小阶梯输出（不再抛 IMAGE_TOO_LARGE）。
        version = self.store.read_image_request(
            self.ref, ImageRequestPolicy(maxPixels=10_000, maxBytes=2)
        )
        self.assertGreater(version.bytes, 2)
        self.assertEqual(version.width, self.ref.width)
        self.assertEqual(version.height, self.ref.height)

    def test_opaque_route_is_jpeg_and_alpha_route_is_webp(self):
        # alpha.1：编码阶梯按 alpha 分流——不透明走 JPEG，带 alpha 走 WebP。
        opaque = self.store.read_image_request(
            self.ref, ImageRequestPolicy(maxPixels=4, maxBytes=1_000_000)
        )
        self.assertEqual(opaque.mediaType, "image/jpeg")
        # 带半透明 alpha 的 4×4 PNG 强制缩放 → WebP 阶梯且保留 alpha
        alpha_im = Image.new("RGBA", (4, 4))
        a_pixels = alpha_im.load()
        for x in range(4):
            for y in range(4):
                a_pixels[x, y] = ((x * 8) % 256, (y * 8) % 256, 128, (x + y) % 180)
        alpha_buffer = io.BytesIO()
        alpha_im.save(alpha_buffer, format="PNG")
        alpha_ref = self.store.save_image(_save(self.store, data=alpha_buffer.getvalue()))
        alpha_version = self.store.read_image_request(
            alpha_ref, ImageRequestPolicy(maxPixels=2, maxBytes=1_000_000)
        )
        self.assertEqual(alpha_version.mediaType, "image/webp")
        self.assertTrue(alpha_version.hasAlpha)

    def test_transform_version_is_v5_and_codes_for_ladder_are_stable(self):
        # alpha.1：变换版本 request-image-v4 → v5；阶梯与编码参数是 caches/上传
        # 索引键的一部分，任何变化都必须同时改版本号。
        from miniharness.attachment.encoding import (
            IMAGE_ENCODING_QUALITIES,
            WEBP_ENCODING_EFFORT,
        )
        from miniharness.attachment.request_image import REQUEST_IMAGE_TRANSFORM_VERSION
        self.assertEqual(REQUEST_IMAGE_TRANSFORM_VERSION, "request-image-v5")
        self.assertEqual(IMAGE_ENCODING_QUALITIES, (85, 75, 60))
        self.assertEqual(WEBP_ENCODING_EFFORT, 0)

    def test_invalid_cached_variant_is_regenerated(self):
        # alpha.1：读取缓存时按规范复验（uchar/srgb/尺寸不超投影/alpha 兼容），
        # 任何不符当作未命中重新生成（不按字节超限判失效）。
        policy = ImageRequestPolicy(maxPixels=4, maxBytes=1_000_000)
        first = self.store.read_image_request(self.ref, policy)
        hash_hex = str(first.variantId)[len("sha256:"):]
        path = os.path.join(self._tmp, "request-images", hash_hex[:2], hash_hex)
        with open(path, "wb") as fh:
            fh.write(b"not-a-real-image")
        second = self.store.read_image_request(self.ref, policy)
        self.assertEqual(second.data, first.data)
        self.assertEqual(second.variantId, first.variantId)

    def test_request_image_dimensions_algorithm(self):
        self.assertEqual(request_image_dimensions(10, 10, 1000), (10, 10))  # 不放大
        w, h = request_image_dimensions(100, 50, 2500)
        self.assertLessEqual(w * h, 2500)
        self.assertEqual(h, w // 2)  # 纵横保持
        lw, lh = request_image_dimensions(2, 100, 100)
        self.assertLessEqual(lw * lh, 100)

    def test_request_image_dimensions_matches_upstream_spec(self):
        # alpha.1 新增纯投影几何（seam 包 request-projection.ts）上游 spec 定值
        self.assertEqual(request_image_dimensions(4096, 4096, 640_000), (800, 800))
        self.assertEqual(request_image_dimensions(4096, 2048, 640_000), (1130, 565))
        self.assertEqual(request_image_dimensions(3840, 2160, 640_000), (1066, 600))
        self.assertEqual(request_image_dimensions(320, 240, 640_000), (320, 240))
        self.assertEqual(request_image_dimensions(2160, 3840, 640_000), (600, 1066))
        # 竖版取整跨过预算时继续内收
        self.assertEqual(request_image_dimensions(2, 4, 5), (1, 2))
        for width, height in [(4096, 4096), (4096, 2048), (3840, 2160), (2160, 3840)]:
            pw, ph = request_image_dimensions(width, height, 640_000)
            self.assertLessEqual(pw * ph, 640_000)


class TestLocalAttachmentStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="mini-attachment-test-")
        self.store = LocalAttachmentStore(root=self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_save_image_returns_content_addressed_ref(self):
        ref = self.store.save_image(_save(self.store))
        self.assertTrue(str(ref.attachmentId).startswith("sha256:"))
        self.assertEqual(ref.mediaType, "image/png")
        self.assertEqual(ref.bytes, len(_ONE_PNG))
        self.assertEqual((ref.width, ref.height), (1, 1))

    def test_same_bytes_deduplicate(self):
        ref1 = self.store.save_image(_save(self.store))
        ref2 = self.store.save_image(_save(self.store))
        self.assertEqual(ref1.attachmentId, ref2.attachmentId)

    def test_read_roundtrip(self):
        ref = self.store.save_image(_save(self.store))
        stored = self.store.read_image(ref)
        self.assertEqual(stored.data, _ONE_PNG)
        self.assertEqual(stored.ref, ref)

    def test_read_missing_raises_not_found(self):
        ref = ImageAttachmentRef(
            attachmentId="sha256:" + "0" * 64,
            mediaType="image/png",
            bytes=len(_ONE_PNG),
            width=1,
            height=1,
        )
        with self.assertRaises(AttachmentError) as cm:
            self.store.read_image(ref)
        self.assertEqual(cm.exception.code, ATTACHMENT_NOT_FOUND)

    def test_read_invalid_reference_rejected(self):
        bad = ImageAttachmentRef(
            attachmentId="not-a-sha", mediaType="image/png",
            bytes=1, width=1, height=1,
        )
        with self.assertRaises(AttachmentError) as cm:
            self.store.read_image(bad)
        self.assertEqual(cm.exception.code, INVALID_ATTACHMENT_REF)

    def test_corrupted_object_detected_on_read(self):
        ref = self.store.save_image(_save(self.store))
        sha = str(ref.attachmentId).removeprefix("sha256:")
        path = os.path.join(self._tmp, "objects", sha[:2], sha)
        # 发布后只读（0o400）：篡改前先清位（模拟真实篡改需写权限）
        os.chmod(path, 0o644)
        with open(path, "wb") as fh:
            fh.write(b"corrupt!")
        with self.assertRaises(AttachmentError) as cm:
            self.store.read_image(ref)
        self.assertEqual(cm.exception.code, ATTACHMENT_CORRUPT)

    def test_type_mismatch_rejected(self):
        with self.assertRaises(AttachmentError) as cm:
            self.store.save_image(_save(self.store, media_type="image/jpeg"))
        self.assertEqual(cm.exception.code, IMAGE_TYPE_MISMATCH)

    def test_unsupported_media_type_rejected(self):
        # 白名单是批次级校验（save_images），非白名单类型整批拒绝
        with self.assertRaises(AttachmentError) as cm:
            self.store.save_images(
                [_save(self.store, data=b"x" * 100, media_type="image/tiff")])
        self.assertEqual(cm.exception.code, UNSUPPORTED_IMAGE_TYPE)

    def test_too_many_images_rejected(self):
        with self.assertRaises(AttachmentError) as cm:
            self.store.save_images([_save(self.store)] * 21)
        self.assertEqual(cm.exception.code, TOO_MANY_IMAGES)

    def test_aggregate_bytes_limit_rejected(self):
        big = ImageAttachmentLimits(
            maxImageBytes=100_000_000,
            maxImagesPerMessage=20,
            maxMessageImageBytes=128,   # 单张 PNG ~68B，3 张必超
            maxImagePixels=40_000_000,
        )
        store = LocalAttachmentStore(root=self._tmp, limits=big)
        with self.assertRaises(AttachmentError) as cm:
            store.save_images([_save(store)] * 5)
        self.assertEqual(cm.exception.code, IMAGES_TOO_LARGE)

    def test_batch_validation_before_persist(self):
        # 第二批含坏类型：任何对象都不应落盘
        objects_dir = os.path.join(self._tmp, "objects")
        refs_before = (
            os.listdir(objects_dir) if os.path.isdir(objects_dir) else []
        )
        with self.assertRaises(AttachmentError):
            self.store.save_images([
                _save(self.store),
                _save(self.store, media_type="image/jpeg"),
            ])
        refs_after = (
            os.listdir(objects_dir) if os.path.isdir(objects_dir) else []
        )
        self.assertEqual(refs_before, refs_after)


class TestFileAttachment(unittest.TestCase):
    """verbatim 文件附件（alpha.1 file-store）：叶名清洗、内容寻址存取、
    去重、流式提交、wire 受理、seam 缺省拒绝与完整性验证。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="mini-attachment-test-")
        self.store = LocalAttachmentStore(root=self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_leaf_name_sanitization(self):
        from miniharness.attachment import file_leaf_name
        # 双分隔符路径成分剥离 + Windows 禁用字符换 _ + 设备名前缀 + 空退化
        self.assertEqual(file_leaf_name(r"C:\Users\bob\report?.txt"), "report_.txt")
        self.assertEqual(file_leaf_name("/srv/data/a/b.bin"), "b.bin")
        self.assertEqual(file_leaf_name("con"), "_con")
        self.assertEqual(file_leaf_name("aux.txt"), "_aux.txt")
        self.assertEqual(file_leaf_name(None), "file")
        self.assertEqual(file_leaf_name(""), "file")
        self.assertEqual(file_leaf_name(".."), "file")
        self.assertEqual(file_leaf_name("  name  "), "name")

    def test_save_read_roundtrip_and_host_path(self):
        from miniharness.attachment import stored_file_path
        ref = self.store.save_file(
            SaveFileAttachment(data=b"file-bytes", name="doc (v2).txt"))
        self.assertEqual(ref.bytes, 10)
        self.assertTrue(ref.attachmentId.value.startswith("sha256:"))
        path = self.store.file_host_path(ref)
        self.assertEqual(path, stored_file_path(self._tmp, ref))
        # 摘要目录 + 清洗显示名叶名（files/<sha2>/<sha>/<name>）
        self.assertIn(os.path.join("files", ref.attachmentId.value[7:9],
                                   ref.attachmentId.value[7:]), path)
        self.assertTrue(path.endswith("doc (v2).txt"))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"file-bytes")
        self.assertEqual(b"".join(self.store.read_file_stream(ref)), b"file-bytes")

    def test_content_addressed_dedup(self):
        first = self.store.save_file(SaveFileAttachment(data=b"same", name="a.txt"))
        second = self.store.save_file(SaveFileAttachment(data=b"same", name="b.bin"))
        self.assertEqual(first.attachmentId, second.attachmentId)
        # 别名路径不同（叶名不同），规范对象本体共享
        self.assertNotEqual(self.store.file_host_path(first),
                            self.store.file_host_path(second))

    def test_stream_save_matches_verbatim(self):
        streamed = self.store.save_file_stream(SaveFileStreamAttachment(
            data=iter([b"chunk-", b"two"]), name="s.bin"))
        direct = self.store.save_file(SaveFileAttachment(data=b"chunk-two", name="d.bin"))
        self.assertEqual(streamed.attachmentId, direct.attachmentId)
        self.assertEqual(b"".join(self.store.read_file_stream(streamed)), b"chunk-two")

    def test_read_missing_object(self):
        ref = self.store.save_file(SaveFileAttachment(data=b"x", name="x.bin"))
        missing = SaveFileAttachment(data=b"y", name="y.bin")
        never = self.store.save_file(missing)
        # 发布后只读（0o400）：删除前先清位（Windows 只读文件不可删）
        os.chmod(self.store.file_host_path(never), 0o644)
        os.unlink(self.store.file_host_path(never))
        with self.assertRaises(AttachmentError) as cm:
            list(self.store.read_file_stream(never))
        self.assertEqual(cm.exception.code, ATTACHMENT_NOT_FOUND)
        del ref

    def test_integrity_verification(self):
        ref = self.store.save_file(SaveFileAttachment(data=b"intact", name="i.bin"))
        bad_bytes = FileAttachmentRef(
            attachmentId=ref.attachmentId, name=ref.name, bytes=999)
        with self.assertRaises(AttachmentError) as cm:
            list(self.store.read_file_stream(bad_bytes))
        self.assertEqual(cm.exception.code, ATTACHMENT_CORRUPT)

    def test_admit_encoded_file_wire(self):
        # 空文件是合法零字节载荷；canonical base64 校验后委托 verbatim 提交
        empty = self.store.admit_encoded_file(EncodedFileAttachment(data=""))
        self.assertEqual(empty.bytes, 0)
        payload = base64.b64encode(b"wire-bytes").decode()
        ref = self.store.admit_encoded_file(
            EncodedFileAttachment(data=payload, name="w.bin"))
        self.assertEqual(b"".join(self.store.read_file_stream(ref)), b"wire-bytes")
        with self.assertRaises(AttachmentError) as cm:
            self.store.admit_encoded_file(EncodedFileAttachment(
                data=base64.b64encode(b"zz").decode() + "!", name="bad.bin"))
        self.assertEqual(cm.exception.code, INVALID_FILE_BASE64)

    def test_base_store_default_rejects_files(self):
        class Bare(AttachmentStore):
            pass
        with self.assertRaises(AttachmentError) as cm:
            Bare().save_file(SaveFileAttachment(data=b"x"))
        self.assertEqual(cm.exception.code, "ATTACHMENT_FILES_UNSUPPORTED")
        with self.assertRaises(AttachmentError) as cm:
            list(Bare().read_file_stream(FileAttachmentRef(
                attachmentId=empty_sha256_id(), name="f", bytes=0)))
        self.assertEqual(cm.exception.code, "ATTACHMENT_FILES_UNSUPPORTED")

    def test_is_attachment_error_membership(self):
        from miniharness.attachment import is_attachment_error
        class Bare(AttachmentStore):
            pass
        try:
            Bare().save_file(SaveFileAttachment(data=b"x"))
            self.fail("expected rejection")
        except AttachmentError as error:
            self.assertTrue(is_attachment_error(error))
        self.assertFalse(is_attachment_error(RuntimeError("plain")))


def empty_sha256_id():
    from miniharness.attachment import AttachmentId
    return AttachmentId("sha256:" + "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")


class TestPublishIntegrityAndSeams(unittest.TestCase):
    """发布完整性（digest-verified EEXIST + chmod 0o400）与宿主路径 seam
    （imageHostPath 契约缺省 None / local 覆盖）+ 结构化错误判定
    （上游 isAttachmentError 按 code 形状跨包兼容，2026-09-05 收口批）。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="mini-attachment-test-")
        self.store = LocalAttachmentStore(root=self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _object_path(self, sha_hex):
        return os.path.join(self._tmp, "objects", sha_hex[:2], sha_hex)

    def test_save_file_publishes_readonly_and_dedup_reverifies(self):
        ref = self.store.save_file(SaveFileAttachment(data=b"bytes", name="a.txt"))
        path = self.store.file_host_path(ref)
        sha = ref.attachmentId.value[7:]
        if os.name == "posix":
            mode = os.stat(self._object_path(sha)).st_mode & 0o777
            self.assertEqual(mode, 0o400)
        # 同名重存命中别名 EEXIST：摘要一致 → 幂等成功
        again = self.store.save_file(SaveFileAttachment(data=b"bytes", name="a.txt"))
        self.assertEqual(again.attachmentId, ref.attachmentId)

    def test_corrupted_alias_detected_on_dedup(self):
        ref = self.store.save_file(SaveFileAttachment(data=b"good", name="a.txt"))
        # 篡改既有别名字节（内容寻址只是概率性同内容，磁盘字节才是权威）；
        # 先清只读位（发布后 0o400）
        alias = self.store.file_host_path(ref)
        os.chmod(alias, 0o644)
        with open(alias, "wb") as fh:
            fh.write(b"evil")
        with self.assertRaises(AttachmentError) as cm:
            self.store.save_file(SaveFileAttachment(data=b"good", name="a.txt"))
        self.assertEqual(cm.exception.code, ATTACHMENT_CORRUPT)

    def test_save_image_publishes_readonly(self):
        ref = self.store.save_image(_save(self.store))
        path = self.store.image_host_path(ref)
        sha = ref.attachmentId.value[7:]
        self.assertEqual(path, self._object_path(sha))
        if os.name == "posix":
            mode = os.stat(path).st_mode & 0o777
            self.assertEqual(mode, 0o400)

    def test_image_host_path_seam_contract(self):
        # 契约缺省（非宿主文件后端）= None（上游基类 void ref → undefined，
        # 不做引用校验）；local 覆盖 = normalized 路径 + 引用校验
        self.assertIsNone(AttachmentStore().image_host_path(
            ImageAttachmentRef(attachmentId="sha256:" + "0" * 64,
                               mediaType="image/png", bytes=1, width=1, height=1)))
        ref = self.store.save_image(_save(self.store))
        self.assertTrue(self.store.image_host_path(ref).startswith(self._tmp))
        with self.assertRaises(AttachmentError) as cm:
            self.store.image_host_path(ImageAttachmentRef(
                attachmentId="sha256:zz", mediaType="image/png", bytes=1,
                width=1, height=1))
        self.assertEqual(cm.exception.code, INVALID_ATTACHMENT_REF)

    def test_is_attachment_error_is_structural(self):
        from miniharness.attachment import is_attachment_error, is_image_admission_error
        # duck-typed 跨包形状（非 AttachmentError 实例）按 code 识别
        class Foreign:
            code = "ATTACHMENT_CORRUPT"
        self.assertTrue(is_attachment_error(Foreign()))
        self.assertTrue(is_attachment_error(AttachmentError("x", ATTACHMENT_CORRUPT)))
        self.assertFalse(is_attachment_error(type("F2", (), {"code": "NOT_A_CODE"})()))
        self.assertFalse(is_attachment_error(ValueError("plain")))
        self.assertTrue(is_image_admission_error(
            type("F3", (), {"code": "TOO_MANY_IMAGES"})()))
        self.assertFalse(is_image_admission_error(Foreign))


if __name__ == "__main__":
    unittest.main()