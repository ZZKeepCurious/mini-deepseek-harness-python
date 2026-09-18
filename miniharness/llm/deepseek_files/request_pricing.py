"""DeepSeek 路由的 provider 侧请求图定价。

对应 dsh 真实源码：packages/llm/llm-deepseek/src/common/request-pricing.ts。

按已公布 vision-token 记账给每个 retained surface occurrence 在其 per-model
请求目标下定价，offloaded occurrence 按其占位文本定价；由 token meter 同步
消费（LlmAdapter.image_request_pricing），provider usage 仍是完成请求的权威。
"""
from __future__ import annotations

from ...attachment.projection import (
    long_edge_dimensions,
    request_image_dimensions,
)
from ...attachment.types import ImageRequestTarget
from ..content import (
    offloaded_image_text,
    request_image_handle_text,
    text_only_image_text,
)
from ..protocol import LlmImageRequestPrice, LlmImageRequestPricing
from .image_tokens import deep_seek_image_tokens, deep_seek_request_image_dimensions

__all__ = [
    "DEFAULT_LOW_DETAIL_IMAGE_PIXEL_BUDGET",
    "DEFAULT_MAX_IMAGES_PER_REQUEST",
    "DEFAULT_MAX_REQUEST_FILES_BYTES",
    "DEFAULT_REQUEST_IMAGE_MAX_BYTES",
    "REQUEST_IMAGE_MAX_DIMENSION",
    "deep_seek_image_request_pricing",
    "resolve_request_image_max_bytes",
    "resolve_request_image_target",
]

#: 每个请求累积 file-referenced image 字节的缺省上界。
DEFAULT_MAX_REQUEST_FILES_BYTES = 128 * 1024 * 1024
#: provider 单请求图片计数上限。
DEFAULT_MAX_IMAGES_PER_REQUEST = 600
#: 匹配 provider low-detail 图片输入的总像素预算。
DEFAULT_LOW_DETAIL_IMAGE_PIXEL_BUDGET = 512 * 512
#: 一张确定性请求图的编码字节目标；无阶梯质量满足时取最小阶梯输出。
DEFAULT_REQUEST_IMAGE_MAX_BYTES = 2 * 1024 * 1024
#: provider 对携带 15 张以上图片请求的单边上限；对所有请求图施加，使图片数不改变投影。
REQUEST_IMAGE_MAX_DIMENSION = 4096


def resolve_request_image_max_bytes(model) -> int:
    """一条 DeepSeek 模型路由对每张请求图施加的编码字节目标（上游同名函数）。"""
    return (model.imageMaxBytes if model.imageMaxBytes is not None
            else DEFAULT_REQUEST_IMAGE_MAX_BYTES)


def _read_dimensions(source) -> tuple[int, int]:
    if isinstance(source, dict):
        return int(source["width"]), int(source["height"])
    return int(source.width), int(source.height)


def resolve_request_image_target(model, source) -> ImageRequestTarget:
    """一条 DeepSeek 模型路由为一张源图选择的确定性请求目标（上游同名函数）。"""
    width, height = _read_dimensions(source)
    budget = (DEFAULT_LOW_DETAIL_IMAGE_PIXEL_BUDGET
              if model.imagePixelBudget == "low" else model.imagePixelBudget)
    projected = (deep_seek_request_image_dimensions(width, height)
                 if budget is None
                 else request_image_dimensions(width, height, int(budget)))
    if max(projected.width, projected.height) > REQUEST_IMAGE_MAX_DIMENSION:
        capped = long_edge_dimensions(width, height, REQUEST_IMAGE_MAX_DIMENSION)
    else:
        capped = projected
    return ImageRequestTarget(width=capped.width, height=capped.height,
                              maxBytes=resolve_request_image_max_bytes(model))


def _text_only_price(block) -> LlmImageRequestPrice:
    return LlmImageRequestPrice(0, text_only_image_text(_attachment(block)))


def _attachment(block):
    return block.attachment if not isinstance(block, dict) else block.get("attachment") or {}


def _offloaded(block) -> bool:
    return (block.get("offloaded") if isinstance(block, dict)
            else getattr(block, "offloaded", False)) is True


class _DeepSeekImageRequestPricing(LlmImageRequestPricing):
    def __init__(self, connection, catalog_model, resolve_access) -> None:
        self._connection = connection
        self._model = catalog_model
        self._resolve_access = resolve_access

    def price_images(self, images: list) -> list:
        if self._model is None or "image" not in (self._model.inputModalities or ()):
            return [_text_only_price(block) for block in images]
        priced: list = []
        for block in images:
            ref = _attachment(block)
            access = self._resolve_access(ref) if self._resolve_access is not None else None
            if _offloaded(block):
                priced.append(LlmImageRequestPrice(0, offloaded_image_text(ref, access)))
            else:
                target = resolve_request_image_target(self._model, ref)
                priced.append(LlmImageRequestPrice(
                    deep_seek_image_tokens(target.width, target.height),
                    request_image_handle_text(ref, target.to_dict(), access)))
        return priced


def deep_seek_image_request_pricing(connection, model: str, resolve_access=None) -> LlmImageRequestPricing:
    """从已校验连接快照为一条 DeepSeek 路由构建请求图定价（上游同名函数）。

    未编目与 text-only 模型把每个 occurrence 定成确定性文本替换；image-capable
    模型把 offloaded occurrence 定成占位文本，retained occurrence 按其投影请求
    尺寸计价。handle/占位文本经与序列化器相同的 access 解析构建。
    """
    catalog_model = next((entry for entry in connection.models if entry.id == model), None)
    return _DeepSeekImageRequestPricing(connection, catalog_model, resolve_access)
