"""web 传输层：单条 `/api/remote.mux` WebSocket（对齐 `packages/api/gateway`）。

载体契约（逐条对应上游 stream-server.ts / index.ts）：
  * 单一路径 `/api/remote.mux`（REMOTE_STREAM_MUX_PATH）承载所有 Remote 流，
    对应 `create_mux_websocket` 的 Gateway `RemoteStreamMuxConnection`。
  * 客户端文本帧四型（`parse_remote_stream_client_message`）：

      {type:'open', streamId, endpoint, payload}   —— 打开一个新的下游流
      {type:'cancel', streamId}                    —— 取消一条已打开流
      {type:'item', streamId, value?}              —— rc.1 上行数据帧（无消费者则丢弃）
      {type:'end', streamId}                       —— rc.1 上行半关（无消费者则丢弃）

    binary 消息（非文本帧）→ close 1003（协议错）；JSON/形状非法 → close 1008。
    重复 open（同 streamId 已活跃）→ close 1008（非法 open）。
  * 每条 open 立即转给 `GatewayStreams.open_stream`（gateway 域分发）；流内每
    value 发一个 `item` 帧（streamId + value 恒在——null 是合法 wire 值）；正常
    结束发 `end`；open 内抛错或流内失败 → 该流发 `error` 帧即终态（不补 end，
    上游 stream-server.ts pump catch 同款），不关 WS（与其它流隔离）；错误帧
    自身发送失败 → close 1011。
  * 心跳：transport 级（不归本层）：launcher uvicorn 选项 `ws_ping_interval=2 /
    ws_ping_timeout=4` 对齐上游 gateway heartbeat（缺省 2s Ping + 连续 2 周期
    无 Pong terminate，见 verified-diffs §3.4）。

属性：`RemoteStreamMuxConnection` 持有 active 流的任务集合，dispose 全量取消。

上行（rc.1）：每条 open 先建自己的有界 `UplinkInbox`（`web/uplink.py`），
`item` 帧按整帧 UTF-8 字节入队、`end` 帧半关；违例（end 后 item → `gateway/protocol`、
超 `streamInboxBytes` → `gateway/uplink-overflow`）以 Remote failure 中止该流并发终态
`error` 帧（上游 pump 的 `control` + `remoteErrorOf(reason)` 分支）。inbox 在 open 之前
建好，客户端紧跟 open 发的 item 等在队列里而不是丢掉（上游 openStream 注释同款）。
`$events` 是网关自有流、无人读上行，open 时立即释放该 inbox（上游 openWireStream）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from .stream_protocol import (
    REMOTE_STREAM_MUX_PATH,
    StreamProtocolError,
    parse_remote_stream_client_message,
)
from .uplink import DEFAULT_STREAM_INBOX_BYTES, UplinkInbox

__all__ = ["RemoteStreamMuxConnection", "REMOTE_STREAM_MUX_PATH"]

DROP_CODE = 1008
PROTOCOL_CODE = 1003


def _error_frame(stream_id: str, code: str, message: str,
                 details: dict | None = None) -> dict:
    return {"type": "error", "streamId": stream_id,
            "error": {"code": code, "message": message, "details": details or {}}}


class _ActiveStream:
    """一条活跃逻辑流：自己的上行 inbox + 泵任务 + 终止原因。"""

    __slots__ = ("stream_id", "endpoint", "inbox", "task", "failure")

    def __init__(self, stream_id: str, endpoint: str, inbox: UplinkInbox):
        self.stream_id = stream_id
        self.endpoint = endpoint
        self.inbox = inbox
        self.task: asyncio.Task | None = None
        self.failure: BaseException | None = None


class RemoteStreamMuxConnection:
    """一条 `/api/remote.mux` 连接的流生命周期（打开/上行/取消/写入）。

    @param gateway - `GatewayStreams`（endpoint 分发 + $events 注册表 + 审批桥）。
    @param ws - 一个鸭子类型 websocket：提供 `receive()`（得到
        {'type':'websocket.receive', text|bytes} 或 {'type':'websocket.disconnect'}）、
        `send_text(str)`。
    @param stream_inbox_bytes - 单条逻辑流可缓冲的上行帧字节上限
        （上游 gateway Config `streamInboxBytes`，缺省 262144）。
    """

    def __init__(self, gateway: Any, ws: Any,
                 stream_inbox_bytes: int = DEFAULT_STREAM_INBOX_BYTES):
        self.gateway = gateway
        self.ws = ws
        self.stream_inbox_bytes = stream_inbox_bytes
        self._streams: dict[str, _ActiveStream] = {}
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
        kind = frame["type"]
        if kind == "open":
            await self._open(frame)
        elif kind == "item":
            stream = self._streams.get(frame["streamId"])
            if stream is not None:
                violation = stream.inbox.push(frame.get("value"), _frame_bytes(text))
                if violation is not None:
                    await self._fail_stream(stream, violation)
        elif kind == "end":
            stream = self._streams.get(frame["streamId"])
            if stream is not None:
                stream.inbox.end()
        else:
            self._cancel(frame["streamId"])

    # ---------- open / cancel ----------

    async def _open(self, frame: dict) -> None:
        stream_id = frame["streamId"]
        if stream_id in self._streams:
            await self._close(DROP_CODE)
            return
        endpoint = frame["endpoint"]
        # inbox 先于端点打开建好：客户端紧跟 open 发的 item 排队等待，不丢。
        active = _ActiveStream(stream_id, endpoint, self._new_inbox(endpoint))
        self._streams[stream_id] = active
        try:
            stream = self.gateway.open_stream(
                endpoint, frame["payload"], uplink=active.inbox)
        except Exception as error:  # noqa: BLE001 - open 内抛错折 error 帧（流内隔离；
            # 上游 pump catch 只发 error、不补 end——error 即该流的终态帧）
            self._streams.pop(stream_id, None)
            active.inbox.fail(error)
            await self._send_failure(stream_id, error)
            return
        task = asyncio.get_running_loop().create_task(self._pump(active, stream))
        active.task = task
        task.add_done_callback(lambda _t: self._streams.pop(stream_id, None))

    def _new_inbox(self, endpoint: str) -> UplinkInbox:
        return UplinkInbox(self.stream_inbox_bytes, endpoint)

    async def _pump(self, active: _ActiveStream, stream) -> None:
        stream_id = active.stream_id
        try:
            async for value in stream:
                # item 帧 value 恒在（上游 `{type,streamId,value}` 构造后由
                # JSON.stringify 丢 undefined；null 是合法 wire 值不丢）
                await self._send_text(json.dumps(
                    {"type": "item", "streamId": stream_id, "value": value}))
            await self._send_text(json.dumps({"type": "end", "streamId": stream_id}))
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 流中途失败折 error 帧（终态，
            # 不补 end——上游 stream-server.ts pump catch 同款）
            await self._send_failure(stream_id, error)
        finally:
            # 下行已定：之后的客户端帧改不了结局，释放上行（上游 pump 的
            # `inbox.fail(new Error('Remote stream ended'))`）。
            active.inbox.fail(_stream_ended())

    def _cancel(self, stream_id: str) -> None:
        """客户端 `cancel`：取消逻辑流并结束任何挂在 uplink 上的读（不发终态帧）。"""
        active = self._streams.pop(stream_id, None)
        if active is None:
            return
        active.inbox.fail(_stream_cancelled())
        if active.task is not None:
            active.task.cancel()

    async def _fail_stream(self, active: _ActiveStream | None,
                           error: BaseException) -> None:
        """上行违例：以该 failure 中止逻辑流并发终态 `error` 帧（上游 pump 的
        `control.signal.aborted` + `remoteErrorOf(reason)` 分支——违例是失败，
        普通取消不是，故只有这里发帧）。"""
        if active is None or active.failure is not None:
            return
        active.failure = error
        active.inbox.fail(error)
        task = active.task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._streams.pop(active.stream_id, None)
        await self._send_failure(active.stream_id, error)

    def _close_all(self) -> None:
        streams = list(self._streams.values())
        self._streams.clear()
        for active in streams:
            active.inbox.fail(_stream_socket_closed())
            if active.task is not None:
                active.task.cancel()

    # ---------- 底层写 ----------

    async def _send_failure(self, stream_id: str, error: Any) -> None:
        """发一条终态 `error` 帧；帧本身写不出去 → close 1011（物理代次失败）。"""
        try:
            await self._send_text(json.dumps(_error_frame(
                stream_id, _failure_code(error), str(error),
                getattr(error, "details", None))))
        except Exception:  # noqa: BLE001 - 错误帧发送失败 → close 1011
            await self._close(1011)

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


def _frame_bytes(text: str) -> int:
    """一条客户端帧的 UTF-8 字节长（上游 `Buffer.byteLength(text, 'utf8')`）。"""
    return len(text.encode("utf-8"))


def _stream_ended() -> RuntimeError:
    return RuntimeError("api gateway: Remote stream ended")


def _stream_cancelled() -> RuntimeError:
    return RuntimeError("api gateway: Remote stream cancelled")


def _stream_socket_closed() -> RuntimeError:
    return RuntimeError("api gateway: Remote stream socket closed")


def _failure_code(error: Any) -> str:
    """把 open/流内异常折成 RPC 码（TypertGatewayError→gateway/internal，abort→gateway/cancelled）。"""
    if getattr(error, "code", None) in (
            "gateway/arguments-invalid", "session/not-found", "gateway/internal",
            "gateway/cancelled"):
        return error.code
    if getattr(error, "code", None):
        return error.code
    if getattr(error, "name", None) == "AbortError" or isinstance(
            error, (asyncio.CancelledError,)):
        return "gateway/cancelled"
    return "gateway/internal"


def serve_websocket(gateway: Any, websocket: Any) -> "RemoteStreamMuxConnection":
    """把一条已接受的 WebSocket 会话交给 mux 连接（server.py 调用）。"""
    conn = RemoteStreamMuxConnection(gateway, websocket)
    return conn
