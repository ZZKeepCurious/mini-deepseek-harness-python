"""附件存储与图片受理测试（attachment 族，第 12 章扩展）。

验证：save_images 批量校验顺序、内容寻址去重、read_image 完整性复验、
错误码全集、尺寸限制、像素限制、媒体类型白名单与声明一致性。
"""
import base64
import os
import tempfile
import unittest

from miniharness.attachment import (
    ATTACHMENT_CORRUPT,
    ATTACHMENT_NOT_FOUND,
    IMAGE_TOO_LARGE,
    IMAGE_TOO_MANY_PIXELS,
    IMAGES_TOO_LARGE,
    IMAGE_TYPE_MISMATCH,
    INVALID_ATTACHMENT_REF,
    INVALID_IMAGE,
    TOO_MANY_IMAGES,
    UNSUPPORTED_IMAGE_TYPE,
    AttachmentError,
    ImageAttachmentRef,
    LocalAttachmentStore,
    SaveImageAttachment,
)
from miniharness.attachment.image import detect_image, probe_image
from miniharness.attachment.types import ImageAttachmentLimits

# 1x1 PNG（base64，可被 stdlib 头部解析）
_ONE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAAF/gL+p5ocAAAAAElFTkSuQmCC"
)


def _save(store, data=_ONE_PNG, media_type="image/png", name=None):
    return SaveImageAttachment(data=data, mediaType=media_type, name=name)


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

    def test_probe_is_header_only(self):
        detected = probe_image(_ONE_PNG)
        self.assertEqual(detected.media_type, "image/png")


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