"""attachment 领域：不可变二进制附件（图片）的类型与常量。

对应 dsh 真实源码：packages/attachment/attachment/src/types.ts（已核实）。

与上游一致：
  * ImageAttachmentRef：{attachmentId, mediaType, bytes, width, height, name?}
    ——attachmentId 是不透明存储标识（sha256 内容寻址），绝不是文件系统路径；
  * ImageMediaType：image/png | image/jpeg | image/webp | image/gif；
  * ImageAttachmentLimits：maxImageBytes / maxImagesPerMessage /
    maxMessageImageBytes / maxImagePixels / mediaTypes；
  * SaveImageAttachment：{data, mediaType, name?}。

载体简化：上游以 TS 类型 + z(schemastery) 模式承载，mini 用 dataclass +
校验函数承载（stdlib 载体简化，见 WRITING-STYLE §4.1）。语义一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "IMAGE_MEDIA_TYPES",
    "AttachmentId",
    "ImageAttachmentLimits",
    "ImageAttachmentRef",
    "ImageMediaType",
    "SaveImageAttachment",
]

ImageMediaType = str


@dataclass(frozen=True)
class AttachmentId:
    """不透明存储标识：sha256 内容寻址（对齐上游 AttachmentId brand）。"""

    value: str

    def __str__(self) -> str:
        return self.value


IMAGE_MEDIA_TYPES: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
)


@dataclass(frozen=True)
class ImageAttachmentRef:
    """一个不可变图片对象的持久元数据。"""

    attachmentId: AttachmentId
    mediaType: str
    bytes: int
    width: int
    height: int
    name: str | None = None

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
        return d


@dataclass(frozen=True)
class SaveImageAttachment:
    """提交待校验并持久化的图片：编码字节 + 声明媒体类型 + 可选显示名。"""

    data: bytes
    mediaType: str
    name: str | None = None


@dataclass(frozen=True)
class ImageAttachmentLimits:
    """部署解析的图片策略：单张字节数 / 单消息张数 / 单消息总字节数 / 总像素。"""

    maxImageBytes: int = 5 * 1024 * 1024
    maxImagesPerMessage: int = 20
    maxMessageImageBytes: int = 100 * 1024 * 1024
    maxImagePixels: int = 40_000_000
    mediaTypes: tuple[str, ...] = field(default_factory=lambda: IMAGE_MEDIA_TYPES)
