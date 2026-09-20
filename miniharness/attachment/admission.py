"""base64 上传的 wire 形态受理 + 子代理 prompt 内容的图像受理门。

对应 dsh 真实源码：packages/attachment/attachment/src/admission.ts（alpha.1
已核实）。接受浏览器上传的每个 RPC 端点的共享入口；
`admit_prompt_content`（模块级）对齐上游子代理受理路径
`packages/subagent/subagent/src/index.ts:440-448`：文本原样穿透、图片经
AttachmentStore 受理为 durable 引用（`AttachmentStore.admitPromptContent`，
见 store.py），受理失败经 `rejectPrompt`（control.ts:107-112）折为
`subagent/attachment-invalid`（reason = 底层 code）。
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
           "admit_prompt_content", "SubagentAttachmentInvalidError",
           "SubagentFileUnsupportedError"]


class SubagentAttachmentInvalidError(AttachmentError):
    """子代理 prompt 受理失败：上游 `rejectPrompt` 把 `AttachmentError` 与
    `SubagentError` 统一折为 `subagent/attachment-invalid`，`reason` 保留底层
    稳定 code（`control.ts:107-112`）。"""

    def __init__(self, child_session_id: str, message: str,
                 reason: str | None = None) -> None:
        super().__init__(message, "subagent/attachment-invalid")
        self.child_session_id = child_session_id
        self.reason = reason


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


def admit_prompt_content(child_session_id: str, content: list, *,
                         attachments: object | None = None,
                         model_supports_images: bool = True) -> list:
    """子代理 prompt 内容受理（对齐上游 subagent/index.ts:440-448 + rejectPrompt）。

    - `file` 部分：宿主边界拒收（上游浏览器客户端在路由前拒绝，
      session.ts:256-264，reason=`SUBAGENT_FILE_UNSUPPORTED`）；
    - 无 `image` 部分：文本与其它内容块原样按序通过，不发生任何存储操作；
    - 有 `image` 部分：子模型须支持图片输入（否则
      reason=`MODEL_DOES_NOT_SUPPORT_IMAGES`），再经 attachment store 把每张
      上传图片受理为 durable 引用（`AttachmentStore.admit_prompt_content`）；
      store 缺席或受理失败折 `SubagentAttachmentInvalidError`
      （reason = 底层稳定 code）。
    """
    blocks = [block for block in content if isinstance(block, dict)]
    for block in blocks:
        if block.get("type") == "file":
            raise SubagentFileUnsupportedError(child_session_id)
    if all(block.get("type") != "image" for block in blocks):
        return list(content)
    if not model_supports_images:
        raise SubagentAttachmentInvalidError(
            child_session_id,
            "subagent model does not accept image input",
            "MODEL_DOES_NOT_SUPPORT_IMAGES",
        )
    if attachments is None:
        raise SubagentAttachmentInvalidError(
            child_session_id,
            "subagent image prompt requires an attachment store",
            None,
        )
    try:
        return attachments.admit_prompt_content(content)
    except AttachmentError as error:
        raise SubagentAttachmentInvalidError(
            child_session_id, error.message, error.code) from error


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
