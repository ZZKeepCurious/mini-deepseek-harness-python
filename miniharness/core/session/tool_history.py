"""历史工具定义的状态重建（上游 packages/core/session/src/tool-history.ts）。

`ToolHistoryProjection` 与模型能力无关地折叠已提交的 `request/header` 与
`developer/message`：header 声明按 seq 索引，developer 消息里的 tool-addition
块按其引用的 `headerSeq` 解析出定义；tool-removal 只影响「可用名集合」，
历史定义仍保留。`snapshot()` 返回当前声明系列的初始声明 + 按序解析的更新——
供 `project_tool_updates` 按路由裁剪请求。

重置规则（上游 apply）：baseline 缺失、`reason==='series'`、`startsSeries`、
或保留名以**不同定义**重新声明时，声明系列重置到该 header。header 缺对应
更新记录（工具更新发射之前的旧会话）时，snapshot 回退为「当前声明、无更新」。
"""
from __future__ import annotations

import json
from types import MappingProxyType
from typing import Any

__all__ = ["ToolHistoryProjection"]


def _plain(value: Any) -> Any:
    """解冻只读代理/tuple 为可 JSON 序列化的普通结构。"""
    if isinstance(value, MappingProxyType):
        value = dict(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _same_definition(left: Any, right: Any) -> bool:
    """两个工具定义是否逐字节同构（上游 JSON.stringify 比较，键序敏感）。"""
    return json.dumps(_plain(left), ensure_ascii=False) == json.dumps(_plain(right), ensure_ascii=False)


class ToolHistoryProjection:
    """折叠 header 与 developer 消息，产出可路由投影的工具历史。"""

    def __init__(self) -> None:
        self._headers: dict[int, list[dict]] = {}
        self._declared: dict[str, dict] = {}
        self._active: tuple[dict, ...] = ()
        self._available: set[str] = set()
        self._baseline_seq: int | None = None
        self._history: dict = {"tools": [], "updates": []}

    def apply(self, event: dict) -> None:
        """按日志序消费一条事件（含恢复期的继承事件）。"""
        etype = event.get("type")
        if etype == "request/header":
            data = event.get("data") or {}
            tools = list((data.get("header") or {}).get("tools") or [])
            seq = event["seq"]
            self._headers[seq] = tools
            # 保留名不能在不改变定义的情况下被重声明而不重置前缀复用；未变的
            # 恢复名由其已记录的新增块重新提供。
            redeclared = any(
                tool.get("name") in self._declared
                and not _same_definition(self._declared[tool.get("name")], tool)
                for tool in tools
            )
            if (self._baseline_seq is None or data.get("reason") == "series"
                    or data.get("startsSeries") is True or redeclared):
                self._baseline_seq = seq
                self._declared = {tool.get("name"): tool for tool in tools}
                self._history = {"tools": tools, "updates": []}
                self._available = {tool.get("name") for tool in tools}
            self._active = tuple(tools)
        elif etype == "developer/message":
            data = event.get("data") or {}
            message = data.get("message") or {}
            header_seq = data.get("headerSeq")
            definitions = self._headers.get(header_seq) if header_seq is not None else None
            additions: list[dict] = []
            for block in message.get("content") or []:
                if block.get("type") != "tool-addition":
                    continue
                name = block.get("toolName")
                tool = next((item for item in (definitions or []) if item.get("name") == name), None)
                if tool is None:
                    raise ValueError(f"tool history: missing definition for {name}")
                additions.append(tool)
            for tool in additions:
                self._declared[tool.get("name")] = tool
            for block in message.get("content") or []:
                if block.get("type") == "tool-addition":
                    self._available.add(block.get("toolName"))
                elif block.get("type") == "tool-removal":
                    self._available.discard(block.get("toolName"))
            self._history = {
                "tools": self._history["tools"],
                "updates": [*self._history["updates"],
                            {"messageId": message.get("id"), "additions": additions}],
            }

    def snapshot(self) -> dict:
        """读一份不可变快照；后续事件不改变它。"""
        # 工具更新发射之前写入的会话有 header 但没有匹配的更新记录。
        if len(self._active) != len(self._available) or any(
                tool.get("name") not in self._available for tool in self._active):
            return {"tools": list(self._active), "updates": []}
        return {
            "tools": list(self._history["tools"]),
            "updates": [dict(update) for update in self._history["updates"]],
        }
