"""base64 上传的 wire 形态受理 + 子代理 prompt 内容的图像拒绝门。

对应 dsh 真实源码：packages/attachment/attachment/src/admission.ts（alpha.1
已核实）。接受浏览器上传的每个 RPC 端点的共享入口；
`admit_prompt_content`（模块级，图像拒绝门）对齐上游
`packages/subagent/subagent/src/control.ts:65-81` 历史 `admitPromptContent`
——alpha.1 起该门移为 AttachmentStore 实例方法（file 部分穿透），见
store.py；本模块保留 mini 子代理 seam 用的 refusal-only 形态。
"""
from __future__ import annotations

import base64

from .error import INVALID_FILE_BASE64, INVALID_IMAGE_BASE64, AttachmentError
from .types import (
    EncodedFileAttachment,
    EncodedImageAttachment,
    FileAttachmentRef,
    ImageAttachmentRef,
    SaveFileAttachment,
    SaveImageAttachment,
)

__all__ = ["admit_encoded_images", "admit_encoded_file", "decode_base64",
           "admit_prompt_content", "SubagentImageUnsupportedError",
           "SubagentFileUnsupportedError"]


class SubagentImageUnsupportedError(AttachmentError):
    """子代理 continuation 不接受图片（alpha.1：码统一 `subagent/attachment-invalid`）。"""

    def __init__(self, child_session_id: str) -> None:
        super().__init__(
            "subagent continuation does not accept images",
            "subagent/attachment-invalid",
        )
        self.child_session_id = child_session_id
        self.reason = None


class SubagentFileUnsupportedError(AttachmentError):
    """子代理 continuation 不接受文件（上游 client 侧
    session.ts:256-264：`subagent/attachment-invalid` + reason
    `SUBAGENT_FILE_UNSUPPORTED`，路由前拒绝）。"""

    def __init__(self, child_session_id: str) -> None:
        super().__init__(
            "subagent continuation does not accept files",
            "subagent/attachment-invalid",
        )
        self.child_session_id = child_session_id
        self.reason = "SUBAGENT_FILE_UNSUPPORTED"


def admit_prompt_content(child_session_id: str, content: list) -> list:
    """子代理 prompt 内容受理：除图片/文件外的所有内容块原样按序通过，
    遇到任何 `image` 块即抛 `SubagentImageUnsupportedError`、任何 `file`
    部分即抛 `SubagentFileUnsupportedError`（refusal-only，绝不上报任何
    attachment store，也绝不静默剥离）。

    对齐上游：图像受理已移为 AttachmentStore 实例方法（见 store.py，
    文件部分穿透）；子代理边界对文件一律拒收（上游 client 侧
    session.ts:256-264 路由前拒绝），图片经 store 受理失败同样折
    `subagent/attachment-invalid`（control.ts:110-112）。
    """
    out = []
    for block in content:
        if isinstance(block, dict):
            btype = block.get("type")
            if btype == "image":
                raise SubagentImageUnsupportedError(child_session_id)
            if btype == "file":
                raise SubagentFileUnsupportedError(child_session_id)
        out.append(block)
    return out


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


def _decode_file_base64(data: str) -> bytes:
    """解码一个文件上传载荷：canonical base64，空串是合法零字节载荷
    （上游 decodeCanonicalBase64(data, 'accept', 'INVALID_FILE_BASE64')）。"""
    try:
        decoded = base64.b64decode(data, validate=True)
        if len(data) > 0 and base64.b64encode(decoded).decode("ascii") != data:
            raise ValueError("non-canonical")
    except Exception as error:
        raise AttachmentError(
            "File upload is not canonical base64.", INVALID_FILE_BASE64
        ) from error
    return decoded


def admit_encoded_file(
    attachments: object,
    file: EncodedFileAttachment,
) -> FileAttachmentRef:
    """受理一个 wire 文件上传（上游 admitEncodedFile，alpha.1 新增）。

    先强制 canonical base64（空文件合法），再委托 store 的 verbatim 提交。
    """
    return attachments.save_file(SaveFileAttachment(
        data=_decode_file_base64(file.data),
        **({} if file.name is None else {"name": file.name}),
    ))
