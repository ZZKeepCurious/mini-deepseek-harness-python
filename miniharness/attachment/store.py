"""本地附件存储：内容寻址（sha256）不可变对象存储。

对应 dsh 真实源码：packages/attachment/attachment/src/index.ts（AttachmentStore
seam）+ packages/attachment/attachment-local/src/store.ts（LocalAttachmentStore
实现）。

与上游一致：
  * save_images：先批量校验（张数 / 总字节 / 类型白名单 / 逐张解析 + 像素），
    全部通过才逐个持久化；返回按输入顺序的引用列表；
  * 存储是内容寻址：sha256 即 attachmentId（`sha256:<hex>`），同名去重；
  * read_image：按引用读取 + 完整性校验（字节数 / 格式 / 尺寸与引用一致）；
  * 错误码：TOO_MANY_IMAGES / IMAGES_TOO_LARGE / UNSUPPORTED_IMAGE_TYPE /
    IMAGE_TOO_LARGE / INVALID_IMAGE / IMAGE_TYPE_MISMATCH /
    IMAGE_TOO_MANY_PIXELS / INVALID_ATTACHMENT_REF / ATTACHMENT_NOT_FOUND /
    ATTACHMENT_CORRUPT / ATTACHMENT_WRITE_FAILED / ATTACHMENT_READ_FAILED。

载体简化：
  * 上游目录含 fsync 持久化链（ensureDurableDirectory / syncDirectory）；
    mini 以普通文件写（stdlib 载体简化，语义不变）；
  * 上游 store.ts 以 `link()` 原子发布 + EEXIST 去重竞态处理；mini 以
    "已存在则比对" 近似（单进程教学环境无并发发布）；
  * 默认 root 上游为 DSH_HOME/attachments/v1；mini 以显式 root（教学环境
    由装配方决定，默认进临时目录）。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass

from .error import (
    ATTACHMENT_CORRUPT,
    ATTACHMENT_NOT_FOUND,
    ATTACHMENT_READ_FAILED,
    ATTACHMENT_WRITE_FAILED,
    IMAGE_TOO_LARGE,
    IMAGES_TOO_LARGE,
    INVALID_ATTACHMENT_REF,
    INVALID_IMAGE,
    TOO_MANY_IMAGES,
    UNSUPPORTED_IMAGE_TYPE,
    AttachmentError,
)
from .image import assert_media_type_matches, detect_image, probe_image
from .types import (
    AttachmentId,
    ImageAttachmentLimits,
    ImageAttachmentRef,
    SaveImageAttachment,
)

__all__ = ["AttachmentStore", "LocalAttachmentStore", "StoredImageAttachment", "DEFAULT_IMAGE_LIMITS"]

DEFAULT_IMAGE_LIMITS = ImageAttachmentLimits()

_ID_PREFIX = "sha256:"


@dataclass(frozen=True)
class StoredImageAttachment:
    """已校验字节与引用（上游 StoredImageAttachment）。"""

    ref: ImageAttachmentRef
    data: bytes


class AttachmentStore:
    """不可变二进制附件服务（上游 AttachmentStore seam 的 mini 简化）。

    以鸭子类型承载（上游为 cordis Service）；实现提供 image_limits 属性与
    save_images / save_image / read_image。mini 保持与上游同名方法。
    """

    image_limits: ImageAttachmentLimits = DEFAULT_IMAGE_LIMITS

    def save_images(self, inputs: list[SaveImageAttachment]) -> list[ImageAttachmentRef]:
        raise NotImplementedError

    def save_image(self, input_: SaveImageAttachment) -> ImageAttachmentRef:
        raise NotImplementedError

    def read_image(self, ref: ImageAttachmentRef) -> StoredImageAttachment:
        raise NotImplementedError


def _object_path(root: str, sha256: str) -> str:
    return os.path.join(root, "objects", sha256[:2], sha256)


def _display_name(value: str | None) -> str | None:
    """清洗显示名：去掉双分隔符的本地路径成分 + 控制字符 + 超长截断。

    对齐上游 displayName（store.ts）：POSIX 宿主把 `\\` 当普通字符，不
    手动剥离会让 Windows 客户端完整本地路径泄漏进引用与会话日志。
    """
    if value is None:
        return None
    leaf = value[max(value.rfind("/"), value.rfind("\\")) + 1:]
    clean = "".join(ch for ch in leaf if not (0 <= ord(ch) < 0x20 or ord(ch) == 0x7F))
    clean = clean.strip()[:255]
    return clean if clean else None


class LocalAttachmentStore(AttachmentStore):
    """内容寻址本地附件存储（对齐 attachment-local 的 LocalAttachmentStore）。

    @param root: 存储根目录（显式传入；上游为 DSH_HOME/attachments/v1）。
    @param limits: 部署图片策略，缺省为 DEFAULT_IMAGE_LIMITS。
    """

    def __init__(
        self,
        root: str | None = None,
        limits: ImageAttachmentLimits | None = None,
    ):
        self.image_limits = limits or DEFAULT_IMAGE_LIMITS
        self.root = root if root is not None else tempfile.mkdtemp(prefix="mini-attachment-")

    def save_images(self, inputs: list[SaveImageAttachment]) -> list[ImageAttachmentRef]:
        limits = self.image_limits
        if len(inputs) > limits.maxImagesPerMessage:
            raise AttachmentError(
                "Image batch exceeds the configured image-count limit.", TOO_MANY_IMAGES
            )
        total = sum(len(i.data) for i in inputs)
        if total > limits.maxMessageImageBytes:
            raise AttachmentError(
                "Image batch exceeds the configured aggregate image-byte limit.",
                IMAGES_TOO_LARGE,
            )
        for input_ in inputs:
            if input_.mediaType not in limits.mediaTypes:
                raise AttachmentError(
                    f"Image type {input_.mediaType} is not accepted by this deployment.",
                    UNSUPPORTED_IMAGE_TYPE,
                )
        # 先全量校验再逐个持久化（上游 saveImages 同款：校验失败不启动写入）
        for input_ in inputs:
            self.validate_image(input_)
        return [self.save_image(i) for i in inputs]

    def validate_image(self, input_: SaveImageAttachment) -> None:
        if len(input_.data) > self.image_limits.maxImageBytes:
            raise AttachmentError(
                "Image exceeds the configured byte limit.", IMAGE_TOO_LARGE
            )
        detected = detect_image(input_.data, self.image_limits.maxImagePixels)
        assert_media_type_matches(detected, input_.mediaType)

    def save_image(self, input_: SaveImageAttachment) -> ImageAttachmentRef:
        if len(input_.data) > self.image_limits.maxImageBytes:
            raise AttachmentError(
                "Image exceeds the configured byte limit.", IMAGE_TOO_LARGE
            )
        detected = detect_image(input_.data, self.image_limits.maxImagePixels)
        assert_media_type_matches(detected, input_.mediaType)
        sha256 = hashlib.sha256(input_.data).hexdigest()
        path = _object_path(self.root, sha256)
        try:
            if not os.path.exists(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                # 原子发布近似：先写临时文件再改名（上游为 link 原子发布）
                tmp = path + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(input_.data)
                os.replace(tmp, path)
            else:
                with open(path, "rb") as fh:
                    if hashlib.sha256(fh.read()).hexdigest() != sha256:
                        raise AttachmentError(
                            "Stored attachment failed integrity verification.",
                            ATTACHMENT_CORRUPT,
                        )
        except AttachmentError:
            raise
        except OSError as e:
            raise AttachmentError(
                "Unable to persist image attachment.", ATTACHMENT_WRITE_FAILED
            ) from e
        name = _display_name(input_.name)
        return ImageAttachmentRef(
            attachmentId=AttachmentId(f"{_ID_PREFIX}{sha256}"),
            mediaType=detected.media_type,
            bytes=len(input_.data),
            width=detected.width,
            height=detected.height,
            **({"name": name} if name is not None else {}),
        )

    def read_image(self, ref: ImageAttachmentRef) -> StoredImageAttachment:
        sha256 = self._ensure_reference(ref)
        path = _object_path(self.root, sha256)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except FileNotFoundError as e:
            raise AttachmentError(
                "Attachment object is missing.", ATTACHMENT_NOT_FOUND
            ) from e
        except OSError as e:
            raise AttachmentError(
                "Unable to read image attachment.", ATTACHMENT_READ_FAILED
            ) from e
        if hashlib.sha256(data).hexdigest() != sha256:
            raise AttachmentError(
                "Stored attachment failed integrity verification.", ATTACHMENT_CORRUPT
            )
        # 读取路径只复验头部字段（上游 probeImage 同语义，不做像素解码）
        try:
            detected = probe_image(data)
        except AttachmentError as e:
            if e.code == INVALID_IMAGE:
                raise AttachmentError(
                    "Stored attachment metadata does not match its reference.",
                    ATTACHMENT_CORRUPT,
                ) from e
            raise
        if (
            detected.media_type != ref.mediaType
            or len(data) != ref.bytes
            or detected.width != ref.width
            or detected.height != ref.height
        ):
            raise AttachmentError(
                "Stored attachment metadata does not match its reference.",
                ATTACHMENT_CORRUPT,
            )
        return StoredImageAttachment(ref=ref, data=data)

    def _ensure_reference(self, ref: ImageAttachmentRef) -> str:
        value = str(ref.attachmentId)
        if not value.startswith(_ID_PREFIX) or len(value) != len(_ID_PREFIX) + 64:
            raise AttachmentError("Attachment reference is invalid.", INVALID_ATTACHMENT_REF)
        hexpart = value[len(_ID_PREFIX):]
        if not all(c in "0123456789abcdef" for c in hexpart):
            raise AttachmentError("Attachment reference is invalid.", INVALID_ATTACHMENT_REF)
        return hexpart
