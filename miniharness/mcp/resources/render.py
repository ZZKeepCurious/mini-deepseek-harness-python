"""资源工具输出渲染：MCP server 资源结果 → 模型可见文本。

对应 dsh 真实源码：packages/mcp/mcp-resources/src/render.ts。
JSON 序列化时把 blob 字段替换为二进制占位（原始 base64 留给程序化调用方）。
"""
from __future__ import annotations

import json
from typing import Any

__all__ = ["render_resource_result"]


def _mask_blobs(value: Any) -> Any:
    """递归把 {blob: base64str} 替换为二进制占位（对齐上游 replacer）。"""
    if isinstance(value, dict):
        return {
            key: (
                f"[binary resource: {len(item)} base64 characters; available to "
                "programmatic callers]"
                if key == "blob" and isinstance(item, str) else _mask_blobs(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_blobs(item) for item in value]
    return value


def render_resource_result(server: str, value: Any) -> str:
    """渲染一个资源请求结果（mcp-resources render 契约，双参）。"""
    rendered = json.dumps(_mask_blobs(value), indent=2, ensure_ascii=False, default=str)
    return f"MCP server: {server}\n{rendered}"