"""attachment 领域：不可变二进制附件（图片）的类型与常量。

对应 dsh 真实源码：packages/attachment/attachment/src/types.ts（rc.2 已核实，
alpha.1 仅改 ImageRequestPolicy.maxBytes 语义）。

与上游一致：
  * ImageAttachmentRef：{attachmentId, mediaType, bytes, width, height,
    name?, originalDimensions?}——attachmentId 是不透明存储标识（规范化字节
    的 sha256 内容寻址），绝不是文件系统路径；originalDimensions 只在规范
    化缩小时出现（EXIF 定向后的输入尺寸）；
  * ImageMediaType：image/png | image/jpeg | image/webp | image/gif；
  * ImageAttachmentLimits：maxImageBytes / maxImagesPerMessage /
    maxMessageImageBytes / maxImagePixels / maxImageDimension / mediaTypes；
  * SaveImageAttachment：{data, mediaType, name?}；
  * StoredImageAttachment：{ref, data}；
  * ImageRequestPolicy：{maxPixels, maxBytes}——按精确模型路由解析的请求图
    策略；alpha.1 起 maxBytes 是编码字节目标（阶梯每个质量都超限时保留最小
    阶梯输出，不再是独立拒绝上限）；RequestImageAttachment：{variantId,
    attachment, data, mediaType, bytes, width, height, depth:'uchar',
    space:'srgb', hasAlpha}——缓存键 variantId 覆盖 attachment id + policy +
    固定编码器参数。

载体简化：上游以 TS 类型 + z(schemastery) 模式承载，mini 用 dataclass +
校验函数承载。语义一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "DEFAULT_MAX_IMAGE_BYTES",
    "DEFAULT_MAX_IMAGE_DIMENSION",
    "DEFAULT_MAX_IMAGE_PIXELS",
    "DEFAULT_MAX_IMAGES_PER_MESSAGE",
    "DEFAULT_MAX_MESSAGE_IMAGE_BYTES",
    "IMAGE_MEDIA_TYPES",
    "AttachmentId",
    "Dimensions",
    "EncodedImageAttachment",
    "ImageAttachmentLimits",
    "ImageAttachmentRef",
    "ImageMediaType",
    "ImageRequestPolicy",
    "ImageVariantId",
    "RequestImageAttachment",
    "SaveImageAttachment",
]

ImageMediaType = str


@dataclass(frozen=True)
class AttachmentId:
    """不透明存储标识：规范化字节的 sha256 内容寻址（上游 brand）。"""

    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ImageVariantId:
    """一个请求图变换的不透明确定身份（attachmentId+policy+编码器参数）。"""

    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Dimensions:
    """像素尺寸对（originalDimensions 载体；上游内联 {width,height} 对象）。"""

    width: int
    height: int

    def to_dict(self) -> dict:
        return {"width": self.width, "height": self.height}


IMAGE_MEDIA_TYPES: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
)

# 上游 attachment-local/src/index.ts 缺省限额（rc.2）
DEFAULT_MAX_IMAGE_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_IMAGES_PER_MESSAGE = 20
DEFAULT_MAX_MESSAGE_IMAGE_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_IMAGE_PIXELS = 64_000_000
DEFAULT_MAX_IMAGE_DIMENSION = 8192


@dataclass(frozen=True)
class ImageAttachmentRef:
    """一个不可变规范化图片对象的持久元数据。"""

    attachmentId: AttachmentId
    mediaType: str
    bytes: int
    width: int
    height: int
    name: str | None = None
    originalDimensions: Dimensions | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "attachmentId": str(self.attachmentId),
            "mediaType": self.mediaType,
            "bytes": self.bytes,
            "width": self.width,
            "height": self.height,
        }
        if self.name is not None:
            d["name"] = self.name
        if self.originalDimensions is not None:
            d["originalDimensions"] = self.originalDimensions.to_dict()
        return d


@dataclass(frozen=True)
class SaveImageAttachment:
    """提交待校验并持久化的图片：编码字节 + 声明媒体类型 + 可选显示名。"""

    data: bytes
    mediaType: str
    name: str | None = None


@dataclass(frozen=True)
class EncodedImageAttachment:
    """随 wire 请求而来的 base64 图片上传（受理时对照解码字节验证声明）。"""

    mediaType: str
    data: str
    name: str | None = None


@dataclass(frozen=True)
class ImageAttachmentLimits:
    """部署解析的图片策略：单张字节 / 单消息张数 / 单消息总字节 / 总像素 / 单边像素。"""

    maxImageBytes: int = DEFAULT_MAX_IMAGE_BYTES
    maxImagesPerMessage: int = DEFAULT_MAX_IMAGES_PER_MESSAGE
    maxMessageImageBytes: int = DEFAULT_MAX_MESSAGE_IMAGE_BYTES
    maxImagePixels: int = DEFAULT_MAX_IMAGE_PIXELS
    maxImageDimension: int = DEFAULT_MAX_IMAGE_DIMENSION
    mediaTypes: tuple[str, ...] = field(default_factory=lambda: IMAGE_MEDIA_TYPES)


@dataclass(frozen=True)
class StoredImageAttachment:
    """引用与摘要验证后的已存图片字节（上游 StoredImageAttachment）。"""

    ref: ImageAttachmentRef
    data: bytes


@dataclass(frozen=True)
class ImageRequestPolicy:
    """按精确模型路由选择的请求图策略：纵横保持投影后的总像素 + 编码字节目标。"""

    maxPixels: int
    maxBytes: int


@dataclass(frozen=True)
class RequestImageAttachment:
    """从一个 provider 无关规范化附件派生的缓存请求版本。"""

    variantId: ImageVariantId
    attachment: ImageAttachmentRef
    data: bytes
    mediaType: str
    bytes: int
    width: int
    height: int
    depth: str = "uchar"
    space: str = "srgb"
    hasAlpha: bool = False
