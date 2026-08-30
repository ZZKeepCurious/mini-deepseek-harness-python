"""web 传输层：单条 `/api/remote.mux` WebSocket（对齐 `packages/api/gateway`）。

载体契约（逐条对应上游 stream-server.ts / index.ts）：
  * 单一路径 `/api/remote.mux`（REMOTE_STREAM_MUX_PATH）承载所有 Remote 流，
    对应 `create_mux_websocket` 的 Gateway `RemoteStreamMuxConnection`。
  * 客户端文本帧两型（`parse_remote_stream_client_message`）：

      {type:'open', streamId, endpoint, payload}   —— 打开一个新的下游流
      {type:'cancel', streamId}                    —— 取消一条已打开流

    binary 消息（非文本帧）→ close 1003（协议错）；JSON/形状非法 → close 1008。
    重复 open（同 streamId 已活跃）→ close 1008（非法 open）。
  * 每条 open 立即转给 `GatewayStreams.open_stream`（gateway 域分发）；流内每
    value 发一个 `item` 帧（streamId + value）；流结束发 `end`；`$events` 的
    ready/emit/waterfall/cancel 都是 item value（帧内已带 type）。open 内抛错
    → 该流先发 `error` 帧再 `end`，不关 WS（与其它流隔离）；错误帧自身发送失败
    → close 1011。
  * 心跳：上游 30s Ping；mini 简化省缺（Starlette WebSocket 无 Ping API，见
    verified-diffs §3.4）。

属性：`RemoteStreamMuxConnection` 持有 active 流的任务集合，dispose 全量取消。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from .stream_protocol import (
    REMOTE_STREAM_MUX_PATH,
    StreamProtocolError,
    parse_remote_stream_client_message,
)

__all__ = ["RemoteStreamMuxConnection", "REMOTE_STREAM_MUX_PATH"]

DROP_CODE = 1008
PROTOCOL_CODE = 1003


def _error_frame(stream_id: str, code: str, message: str,
                 details: dict | None = None) -> dict:
    return {"type": "error", "streamId": stream_id,
            "error": {"code": code, "message": message, "details": details or {}}}


class RemoteStreamMuxConnection:
    """一条 `/api/remote.mux` 连接的流生命周期（打开/取消/写入）。

    @param gateway - `GatewayStreams`（endpoint 分发 + $events 注册表 + 审批桥）。
    @param ws - 一个鸭子类型 websocket：提供 `receive()`（得到
        {'type':'websocket.receive', text|bytes} 或 {'type':'websocket.disconnect'}）、
        `send_text(str)`。
    """

    def __init__(self, gateway: Any, ws: Any):
        self.gateway = gateway
        self.ws = ws
        self._streams: dict[str, asyncio.Task] = {}
        self._closed = False

    # ---------- 驱动循环 ----------

    async def run(self) -> None:
        """消费客户端帧直至断开；清理所有 active 流。"""
        try:
            while not self._closed:
                message = await self._receive()
                if message is None:
                    break
                await self._dispatch(message["text"])
        finally:
            self._closed = True
            self._close_all()

    async def _receive(self) -> dict | None:
        raw = await self.ws.receive()
        kind = raw.get("type") if isinstance(raw, dict) else None
        if kind == "websocket.disconnect":
            return None
        if kind == "websocket.receive":
            if "bytes" in raw:
                await self._close(PROTOCOL_CODE)
                return None
            return {"text": raw.get("text", "")}
        return None

    async def _dispatch(self, text: str) -> None:
        try:
            frame = parse_remote_stream_client_message(text)
        except StreamProtocolError:
            await self._close(DROP_CODE)
            return
        if frame["type"] == "open":
            await self._open(frame)
        else:
            self._cancel(frame["streamId"])

    # ---------- open / cancel ----------

    async def _open(self, frame: dict) -> None:
        stream_id = frame["streamId"]
        if stream_id in self._streams:
            await self._close(DROP_CODE)
            return
        try:
            stream = self.gateway.open_stream(frame["endpoint"], frame["payload"])
        except Exception as error:  # noqa: BLE001 - open 内抛错折 error 帧（流内隔离）
            await self._send_text(json.dumps(
                _error_frame(stream_id, _failure_code(error), str(error))))
            await self._send_text(json.dumps({"type": "end", "streamId": stream_id}))
            return
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._pump(stream_id, stream))
        self._streams[stream_id] = task
        task.add_done_callback(lambda _t: self._streams.pop(stream_id, None))

    async def _pump(self, stream_id: str, stream) -> None:
        try:
            async for value in stream:
                frame = {"type": "item", "streamId": stream_id}
                if value is not None:
                    frame["value"] = value
                await self._send_text(json.dumps(frame))
            await self._send_text(json.dumps({"type": "end", "streamId": stream_id}))
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 流中途失败折 error 帧后 end
            try:
                await self._send_text(json.dumps(
                    _error_frame(stream_id, _failure_code(error), str(error))))
                await self._send_text(json.dumps({"type": "end", "streamId": stream_id}))
            except Exception:  # noqa: BLE001 - 错误帧发送失败 → close 1011
                await self._close(1011)

    def _cancel(self, stream_id: str) -> None:
        task = self._streams.pop(stream_id, None)
        if task is not None:
            task.cancel()

    def _close_all(self) -> None:
        tasks = list(self._streams.values())
        self._streams.clear()
        for task in tasks:
            task.cancel()

    # ---------- 底层写 ----------

    async def _send_text(self, text: str) -> None:
        if self._closed:
            return
        try:
            await self.ws.send_text(text)
        except Exception:  # noqa: BLE001 - 连接不可写
            self._closed = True
            raise

    async def _close(self, code: int) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.ws.close(code=code)
        except Exception:  # noqa: BLE001 - close 尽力而为
            pass
        self._close_all()

    def dispose(self) -> None:
        self._closed = True
        self._close_all()


def _failure_code(error: Any) -> str:
    """把 open/流内异常折成 RPC 码（TypertGatewayError→internal，abort→cancelled）。"""
    if getattr(error, "code", None) in (
            "arguments-invalid", "session-not-found", "internal", "cancelled"):
        return error.code
    if getattr(error, "code", None):
        return error.code
    if getattr(error, "name", None) == "AbortError" or isinstance(
            error, (asyncio.CancelledError,)):
        return "cancelled"
    return "internal"


def serve_websocket(gateway: Any, websocket: Any) -> "RemoteStreamMuxConnection":
    """把一条已接受的 WebSocket 会话交给 mux 连接（server.py 调用）。"""
    conn = RemoteStreamMuxConnection(gateway, websocket)
    return conn
