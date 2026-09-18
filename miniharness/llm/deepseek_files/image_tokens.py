"""DeepSeek vision-token 计量：provider 公布的图片 token 计算器逐字移植。

对应 dsh 真实源码：packages/llm/llm-deepseek/src/common/image-tokens.ts。

provider 把总像素低于 544×544 的图放大、对齐到 14px patch 网格、按轴 3:1
降采样成 token 单元，并把单图封顶在 1024 token（解预算内最大保持纵横比的
网格）。该配置无对齐 pad、无纵横比 clamp。真实 usage 仍是权威。
"""
from __future__ import annotations

import math
from typing import NamedTuple

from ...attachment.projection import ProjectedDimensions, long_edge_dimensions

__all__ = [
    "deep_seek_image_tokens",
    "deep_seek_request_image_dimensions",
]

PATCH_SIZE = 14
DOWNSAMPLE_RATIO = 3
MAX_IMAGE_TOKENS = 1024
MIN_PIXELS = 544 * 544
CELL_SIZE = PATCH_SIZE * DOWNSAMPLE_RATIO


def _int_div(value: int, divisor: int) -> int:
    return math.floor(value / divisor)


def _ceil_div(value: int, divisor: int) -> int:
    return math.floor((value + divisor - 1) / divisor)


class _GridResize(NamedTuple):
    gridHeight: int
    gridWidth: int
    bestHeight: int
    bestWidth: int
    numTokens: int


def _grid_tokens(grid_height: int, grid_width: int) -> int:
    return grid_height * (grid_width + 1) + 2


def _grid_cells(padded_length: int) -> int:
    return _ceil_div(_int_div(padded_length, PATCH_SIZE), DOWNSAMPLE_RATIO)


def _solve_resize_ratio(height: int, width: int, budget: int) -> _GridResize:
    aspect = height / width
    ideal_grid_width = math.sqrt((budget - 2) / aspect + 0.25) - 0.5
    ideal_grid_height = ideal_grid_width * aspect
    if ideal_grid_width < 1:
        solved_grid_width = 1
        solved_grid_height = _int_div(budget - 2, solved_grid_width + 1)
        best_width = solved_grid_width * CELL_SIZE
        best_height = solved_grid_height * CELL_SIZE
    elif ideal_grid_height < 1:
        solved_grid_height = 1
        solved_grid_width = _int_div(budget - 2, solved_grid_height) - 1
        best_width = solved_grid_width * CELL_SIZE
        best_height = solved_grid_height * CELL_SIZE
    else:
        solved_grid_width = math.trunc(ideal_grid_width)
        solved_grid_height = math.trunc(ideal_grid_height)
        scale = min(solved_grid_width * CELL_SIZE / width,
                    solved_grid_height * CELL_SIZE / height)
        best_width = math.trunc(width * scale / PATCH_SIZE) * PATCH_SIZE
        best_height = math.trunc(height * scale / PATCH_SIZE) * PATCH_SIZE
    grid_height = _grid_cells(best_height)
    grid_width = _grid_cells(best_width)
    return _GridResize(grid_height, grid_width, best_height, best_width,
                       _grid_tokens(grid_height, grid_width))


def _safe_resize(height: int, width: int,
                 padded_height: int, padded_width: int) -> _GridResize:
    grid_height = _grid_cells(padded_height)
    grid_width = _grid_cells(padded_width)
    direct = _GridResize(grid_height, grid_width, padded_height, padded_width,
                         _grid_tokens(grid_height, grid_width))
    if direct.numTokens <= MAX_IMAGE_TOKENS:
        return direct
    solved = _solve_resize_ratio(height, width, MAX_IMAGE_TOKENS)
    if solved.numTokens > MAX_IMAGE_TOKENS:
        raise ValueError(
            f"deepseek image tokens: no grid fits the token budget for {width}x{height}")
    return solved


def _resize_once(width: int, height: int) -> _GridResize:
    scaled_width = width
    scaled_height = height
    pixels = scaled_width * scaled_height
    if pixels < MIN_PIXELS and pixels > 0:
        scale = math.sqrt(MIN_PIXELS / pixels)
        scaled_width = math.trunc(scaled_width * scale)
        scaled_height = math.trunc(scaled_height * scale)
    padded_width = _ceil_div(scaled_width, PATCH_SIZE) * PATCH_SIZE
    padded_height = _ceil_div(scaled_height, PATCH_SIZE) * PATCH_SIZE
    return _safe_resize(scaled_height, scaled_width, padded_height, padded_width)


def _same_resize(a: _GridResize, b: _GridResize) -> bool:
    return (a.gridHeight == b.gridHeight and a.gridWidth == b.gridWidth
            and a.bestHeight == b.bestHeight and a.bestWidth == b.bestWidth
            and a.numTokens == b.numTokens)


def deep_seek_request_image_dimensions(width: int, height: int) -> ProjectedDimensions:
    """harness 发送的尺寸，使 provider 保留整图（上游 deepSeekRequestImageDimensions）。"""
    padded_width = _ceil_div(width, PATCH_SIZE) * PATCH_SIZE
    padded_height = _ceil_div(height, PATCH_SIZE) * PATCH_SIZE
    if _grid_tokens(_grid_cells(padded_height), _grid_cells(padded_width)) <= MAX_IMAGE_TOKENS:
        return ProjectedDimensions(width=width, height=height)
    solved = _solve_resize_ratio(height, width, MAX_IMAGE_TOKENS)
    return long_edge_dimensions(
        width, height, solved.bestWidth if width >= height else solved.bestHeight)


def deep_seek_image_tokens(width: int, height: int) -> int:
    """DeepSeek 为一张给定尺寸的请求图收取的 vision token 数（至多 1024）。"""
    result = _resize_once(width, height)
    for _ in range(1, 10):
        nxt = _resize_once(result.bestWidth, result.bestHeight)
        if _same_resize(nxt, result):
            return result.numTokens
        result = nxt
    raise ValueError(
        f"deepseek image tokens: resize did not converge for {width}x{height}")
