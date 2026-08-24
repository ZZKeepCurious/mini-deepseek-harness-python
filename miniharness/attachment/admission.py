"""base64 图片上传的 wire 形态受理。

对应 dsh 真实源码：packages/attachment/attachment/src/admission.ts（rc.2
新增，已核实）。接受浏览器上传的每个 RPC 端点的共享入口。
"""
from __future__ import annotations

import base64

from .error import INVALID_IMAGE_BASE64, AttachmentError
from .types import (
    EncodedImageAttachment,
    ImageAttachmentRef,
    SaveImageAttachment,
)

__all__ = ["admit_encoded_images", "decode_base64"]


def decode_base64(data: str) -> bytes:
    """解码一个上传载荷，拒绝非 canonical 的 base64 形态（上游同款）。"""
    try:
        decoded = base64.b64decode(data, validate=True)
        if len(data) == 0 or base64.b64encode(decoded).decode("ascii") != data:
            raise ValueError("non-canonical")
    except Exception as error:
        raise AttachmentError(
            "Image upload is not canonical base64.", INVALID_IMAGE_BASE64
        ) from error
    return decoded


def _save_input(image: EncodedImageAttachment) -> SaveImageAttachment:
    return SaveImageAttachment(
        data=decode_base64(image.data),
        mediaType=image.mediaType,
        **({} if image.name is None else {"name": image.name}),
    )


def admit_encoded_images(
    attachments: object,
    images: list[EncodedImageAttachment],
) -> list[ImageAttachmentRef]:
    """受理一个 wire 图片批次：先对每个成员强制 canonical base64，再委托
    批量受理——张数与聚合字节限制、媒体类型与逐图校验、按序提交。

    @param attachments: 拥有批量策略的部署 attachment store。
    @param images: 调用方顺序的 base64 上传。
    @returns 与 `images` 同序的持久引用。
    """
    return attachments.save_images([_save_input(image) for image in images])
