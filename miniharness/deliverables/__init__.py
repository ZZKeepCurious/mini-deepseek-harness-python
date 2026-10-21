"""Deliverables 域（packages/deliverables/tool-present）。

`present` 工具把已存在的普通文件声明为最终交付，追加 durable
`deliverables/presented {turn, callId, files}` 事件供客户端渲染。
"""
from __future__ import annotations

from .present import register_present_tool

__all__ = ["register_present_tool"]