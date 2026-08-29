"""纯请求投影几何：attachment provider 与 provider 侧请求计价共享。

对应 dsh 真实源码：packages/attachment/attachment/src/request-projection.ts
（alpha.1 新增，2026-08-24，上游 commit 30704dc1df）。rc.2 中此函数内联在
attachment-local/src/request-image.ts；alpha.1 抽到 seam 包供规范化缩边与
provider 侧请求计价共用。
"""
from __future__ import annotations

__all__ = ["request_image_dimensions"]


def request_image_dimensions(
    width: int,
    height: int,
    max_pixels: int,
) -> tuple[int, int]:
    """硬性总像素预算内的纵横保持整数尺寸（上游 requestImageDimensions 纯函数）。

    内收取整；小图不放大。上游 spec 定值：4096×4096 → 640,000 预算 = (800,
    800)；4096×2048 → (1130, 565)；3840×2160 → (1066, 600)；竖版 2160×3840
    → (600, 1066)；2×4 预算 5 → (1, 2)（整数纵横取整跨过预算时继续内收）。
    """
    scale = min(1.0, (max_pixels / (width * height)) ** 0.5)
    if scale == 1:
        return width, height
    if width >= height:
        projected_width = max(1, int(width * scale))
        projected_height = max(1, round(projected_width * height / width))
        while projected_width * projected_height > max_pixels and projected_width > 1:
            projected_width -= 1
            projected_height = max(1, round(projected_width * height / width))
        return projected_width, projected_height
    projected_height = max(1, int(height * scale))
    projected_width = max(1, round(projected_height * width / height))
    while projected_width * projected_height > max_pixels and projected_height > 1:
        projected_height -= 1
        projected_width = max(1, round(projected_height * width / height))
    return projected_width, projected_height
