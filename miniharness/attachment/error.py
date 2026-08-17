"""attachment 错误：稳定失败码 + 可纠正性的图片受理分类。

对应 dsh 真实源码：packages/attachment/attachment/src/error.ts（已核实）。

与上游一致：
  * AttachmentError 携带稳定 code（协议错误路由用），本实现以 code 字段
    （str）承载，不依赖类型层级（上游故意不复用 HarnessError 以避免
    attachment → llm 依赖环，mini 同理保持 attachment 零内部依赖）；
  * ImageAdmissionErrorCode 是调用方可纠正的受理失败集合；
  * is_image_admission_error 判定错误是否属于可纠正集合。

上游错误码全集（error.ts）：
  * 可纠正（image admission）：TOO_MANY_IMAGES / IMAGES_TOO_LARGE /
    UNSUPPORTED_IMAGE_TYPE / INVALID_IMAGE_BASE64 / INVALID_IMAGE /
    IMAGE_TYPE_MISMATCH / IMAGE_TOO_LARGE / IMAGE_TOO_MANY_PIXELS；
  * 存储：INVALID_ATTACHMENT_REF / ATTACHMENT_CORRUPT /
    ATTACHMENT_WRITE_FAILED / ATTACHMENT_NOT_FOUND / ATTACHMENT_READ_FAILED。
"""
from __future__ import annotations

__all__ = [
    "ATTACHMENT_CORRUPT",
    "ATTACHMENT_NOT_FOUND",
    "ATTACHMENT_READ_FAILED",
    "ATTACHMENT_WRITE_FAILED",
    "IMAGE_TOO_LARGE",
    "IMAGE_TOO_MANY_PIXELS",
    "IMAGES_TOO_LARGE",
    "INVALID_ATTACHMENT_REF",
    "INVALID_IMAGE",
    "INVALID_IMAGE_BASE64",
    "IMAGE_TYPE_MISMATCH",
    "AttachmentError",
    "TOO_MANY_IMAGES",
    "UNSUPPORTED_IMAGE_TYPE",
    "is_image_admission_error",
]

# 可纠正的图片受理失败（上游 IMAGE_ADMISSION_ERROR_CODES，error.ts）
IMAGE_ADMISSION_ERROR_CODES: frozenset[str] = frozenset({
    "TOO_MANY_IMAGES",
    "IMAGES_TOO_LARGE",
    "UNSUPPORTED_IMAGE_TYPE",
    "INVALID_IMAGE_BASE64",
    "INVALID_IMAGE",
    "IMAGE_TYPE_MISMATCH",
    "IMAGE_TOO_LARGE",
    "IMAGE_TOO_MANY_PIXELS",
})

# 各错误码常量（供 import 语义明确）
TOO_MANY_IMAGES = "TOO_MANY_IMAGES"
IMAGES_TOO_LARGE = "IMAGES_TOO_LARGE"
UNSUPPORTED_IMAGE_TYPE = "UNSUPPORTED_IMAGE_TYPE"
INVALID_IMAGE_BASE64 = "INVALID_IMAGE_BASE64"
INVALID_IMAGE = "INVALID_IMAGE"
IMAGE_TYPE_MISMATCH = "IMAGE_TYPE_MISMATCH"
IMAGE_TOO_LARGE = "IMAGE_TOO_LARGE"
IMAGE_TOO_MANY_PIXELS = "IMAGE_TOO_MANY_PIXELS"
INVALID_ATTACHMENT_REF = "INVALID_ATTACHMENT_REF"
ATTACHMENT_CORRUPT = "ATTACHMENT_CORRUPT"
ATTACHMENT_WRITE_FAILED = "ATTACHMENT_WRITE_FAILED"
ATTACHMENT_NOT_FOUND = "ATTACHMENT_NOT_FOUND"
ATTACHMENT_READ_FAILED = "ATTACHMENT_READ_FAILED"


class AttachmentError(Exception):
    """附件失败：携带稳定机器路由 code（协议错误路由用）。

    简化标注：上游 HarnessError 有 frozen failure 快照与 cause 链；本实现
    只保留 {code, message}（语义一致，格式不同）。
    """

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message
        self.code = code
        self.name = "AttachmentError"


def is_image_admission_error(error: object) -> bool:
    """是否属于调用方可纠正的图片受理失败（上游 isImageAdmissionError）。

    仅按 code 成员判定（上游按运行时成员集合判定，跨包兼容形状）。
    """
    return (
        isinstance(error, AttachmentError)
        and isinstance(error.code, str)
        and error.code in IMAGE_ADMISSION_ERROR_CODES
    )
