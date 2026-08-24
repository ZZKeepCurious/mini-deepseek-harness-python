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
    IMAGE_TOO_LARGE,
    IMAGE_TOO_MANY_PIXELS,
    IMAGES_TOO_LARGE,
    IMAGE_TYPE_MISMATCH,
    INVALID_ATTACHMENT_REF,
    INVALID_IMAGE,
    INVALID_IMAGE_BASE64,
    TOO_MANY_IMAGES,
    UNSUPPORTED_IMAGE_TYPE,
    AttachmentError,
    AttachmentStore,
    ImageAttachmentRef,
    ImageRequestPolicy,
    LocalAttachmentStore,
    SaveImageAttachment,
    encoded_alpha_is_compatible,
)
from miniharness.attachment.image import detect_image, probe_image
from miniharness.attachment.normalization import NormalizationPolicy
from miniharness.attachment.request_image import (
    request_image_dimensions,
    request_image_variant_id,
)
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
        # 规范化字节上限成立（独立安全上限）
        tight = LocalAttachmentStore(
            root=self._tmp,
            normalization_policy=NormalizationPolicy(maxDimension=2048, maxBytes=200),
        )
        small = tight.save_image(_save(tight, data=_png_bytes(16, 16)))
        self.assertLessEqual(small.bytes, 200)

    def test_unencodable_within_cap_raises_image_too_large(self):
        policy = NormalizationPolicy(maxDimension=2048, maxBytes=8)
        store = LocalAttachmentStore(root=self._tmp, normalization_policy=policy)
        with self.assertRaises(AttachmentError) as cm:
            store.save_image(_save(store, data=_png_bytes(16, 16)))
        self.assertEqual(cm.exception.code, IMAGE_TOO_LARGE)

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

    def test_impossible_byte_budget_raises_model_request_wording(self):
        with self.assertRaises(AttachmentError) as cm:
            self.store.read_image_request(
                self.ref, ImageRequestPolicy(maxPixels=10_000, maxBytes=2)
            )
        self.assertEqual(cm.exception.code, IMAGE_TOO_LARGE)
        self.assertEqual(
            str(cm.exception),
            "Image cannot be encoded within the model-request byte budget.",
        )

    def test_request_image_dimensions_algorithm(self):
        self.assertEqual(request_image_dimensions(10, 10, 1000), (10, 10))  # 不放大
        w, h = request_image_dimensions(100, 50, 2500)
        self.assertLessEqual(w * h, 2500)
        self.assertEqual(h, w // 2)  # 纵横保持
        lw, lh = request_image_dimensions(2, 100, 100)
        self.assertLessEqual(lw * lh, 100)


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


if __name__ == "__main__":
    unittest.main()