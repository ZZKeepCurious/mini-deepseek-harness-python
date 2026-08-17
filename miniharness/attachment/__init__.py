"""attachment 族聚合再导出：不可变二进制附件（图片）领域。

对应 dsh 真实源码：packages/attachment/{attachment, attachment-local}。

族内拆分：types（类型/常量）、error（失败码）、image（光栅探测）、
store（内容寻址存储）。教学简化与载体差异见各模块 docstring 与
AGENTS.md 简化清单（sharp 解码 → stdlib 头部解析、fsync 持久化链 → 普通
文件写、link 原子发布 → 临时文件替换）。
"""
from .error import *  # noqa: F401,F403
from .image import *  # noqa: F401,F403
from .store import *  # noqa: F401,F403
from .types import *  # noqa: F401,F403

__all__ = [
    "ATTACHMENT_CORRUPT",
    "ATTACHMENT_NOT_FOUND",
    "ATTACHMENT_READ_FAILED",
    "ATTACHMENT_WRITE_FAILED",
    "AttachmentError",
    "AttachmentId",
    "AttachmentStore",
    "DEFAULT_IMAGE_LIMITS",
    "IMAGE_TOO_LARGE",
    "IMAGE_TOO_MANY_PIXELS",
"IMAGES_TOO_LARGE",
    "IMAGE_TYPE_MISMATCH",
    "INVALID_ATTACHMENT_REF",
    "INVALID_IMAGE",
    "INVALID_IMAGE_BASE64",
    "ImageAttachmentLimits",
    "ImageAttachmentRef",
    "LocalAttachmentStore",
    "SaveImageAttachment",
    "StoredImageAttachment",
    "TOO_MANY_IMAGES",
    "detect_image",
    "image_media_type",
    "is_image_admission_error",
    "probe_image",
]
