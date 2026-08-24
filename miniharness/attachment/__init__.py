"""attachment 族聚合再导出：不可变二进制附件（图片）领域。

对应 dsh 真实源码：packages/attachment/{attachment, attachment-local}。

族内拆分：types（类型/常量）、error（失败码）、image（光栅探测，Pillow
权威解码/头部探测）、encoding（惰性编码候选执行）、normalization（provider
无关规范化管线）、request_image（variantId 确定身份的请求图缓存版本）、
admission（canonical base64 wire 受理入口）、store（内容寻址存储）。
载体差异（sharp → Pillow、同步载体无并发闸）见各模块 docstring 与
verified-diffs §3.9。
"""
from .admission import *  # noqa: F401,F403
from .encoding import *  # noqa: F401,F403
from .error import *  # noqa: F401,F403
from .image import *  # noqa: F401,F403
from .normalization import *  # noqa: F401,F403
from .request_image import *  # noqa: F401,F403
from .store import *  # noqa: F401,F403
from .types import *  # noqa: F401,F403

__all__ = [
    "ATTACHMENT_CORRUPT",
    "ATTACHMENT_NOT_FOUND",
    "ATTACHMENT_PROJECTION_UNSUPPORTED",
    "ATTACHMENT_READ_FAILED",
    "ATTACHMENT_WRITE_FAILED",
    "AttachmentError",
    "AttachmentId",
    "AttachmentStore",
    "DEFAULT_IMAGE_LIMITS",
    "Dimensions",
    "EncodedImageAttachment",
    "IMAGE_DIMENSION_TOO_LARGE",
    "IMAGE_TOO_LARGE",
    "IMAGE_TOO_MANY_PIXELS",
    "IMAGES_TOO_LARGE",
    "IMAGE_TYPE_MISMATCH",
    "INVALID_ATTACHMENT_REF",
    "INVALID_IMAGE",
    "INVALID_IMAGE_BASE64",
    "ImageAttachmentLimits",
    "ImageAttachmentRef",
    "ImageRequestPolicy",
    "ImageVariantId",
    "LocalAttachmentStore",
    "RequestImageAttachment",
    "SaveImageAttachment",
    "StoredImageAttachment",
    "TOO_MANY_IMAGES",
    "admit_encoded_images",
    "decode_base64",
    "detect_image",
    "is_image_admission_error",
    "probe_image",
]
