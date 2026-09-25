"""web 上行载体：单条逻辑流的有界 inbox（对齐 `packages/api/gateway/src/stream-server.ts` 的 UplinkInbox）。

客户端 `item` 帧先进这条流自己的 inbox，宿主方法经 `invocation.uplink()` 读它。
缓冲字节以 UTF-8 计（上游 `Buffer.byteLength(text, 'utf8')` = 整条帧的字节长），
超过 `streamInboxBytes` 即 `gateway/uplink-overflow`；客户端 `end` 之后再来 `item`
即 `gateway/protocol`。两种违例都以 Remote failure 的身份中止该逻辑流——载体把
失败原因交给 mux，mux 据此发终态 `error` 帧（上游 pump `control.signal.aborted`
且 `remoteErrorOf(reason)` 有值的分支）。

单一消费者：`__aiter__` 只允许取一次迭代器；只有一个挂起读。消费者停止读（释放）
后到达的帧直接丢弃，不再计字节（上游 `return()` + `push` 的 `closed` 早退）。
违例由 `push` 的返回值交给载体（上游是 `onViolation` 回调 + `abort.abort(error)`：
这里 `push` 同步判定，判定点与上游 `violate` 同一处），由 mux 中止该逻辑流并发终态
`error` 帧。

宿主方法侧是 `UplinkItems`：上游 `GatewayInvocation.uplink()` 返回的
`UplinkDecoder`（index.ts:1214-1285）——每项先过该方法声明的 codec（未声明则走
无损 JSON 校验，即 `SRC_JSON_CODEC`），被拒项以 `gateway/input-invalid`（`field` 为
`uplink`）中止整条逻辑流。
"""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from typing import Any

from .stream_protocol import is_remote_json_value

__all__ = [
    "DEFAULT_STREAM_INBOX_BYTES",
    "UplinkDecodeError",
    "UplinkInbox",
    "UplinkItems",
    "UplinkViolation",
]

#: 一条逻辑流可缓冲的上行帧字节上限（gateway Config `streamInboxBytes`
#: @default 262144，index.ts:137/148/203）。
DEFAULT_STREAM_INBOX_BYTES = 262_144


class UplinkViolation(RuntimeError):
    """上行违例：以 Remote failure 身份中止逻辑流（上游 `RemoteError` 的折叠）。

    @param code - `gateway/protocol`（end 后 item）或 `gateway/uplink-overflow`。
    @param endpoint - 该 inbox 归属的 endpoint（进 error 帧 details）。
    """

    def __init__(self, code: str, message: str, endpoint: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = {"endpoint": endpoint}


class UplinkInbox:
    """一条逻辑流的有界单消费者上行队列。

    @param max_bytes - 缓冲帧字节上限（`streamInboxBytes`）。
    @param endpoint - 归属 endpoint（违例 details）。
    """

    def __init__(self, max_bytes: int, endpoint: str):
        self._max_bytes = max_bytes
        self._endpoint = endpoint
        self._queue: deque[tuple[Any, int]] = deque()
        self._bytes = 0
        self._ended = False
        self._closed = False
        self._taken = False
        self._failure: BaseException | None = None
        self._pending = False
        self._wake = asyncio.Event()

    # ---------- 写入侧（mux 收到客户端帧时） ----------

    def push(self, value: Any, frame_bytes: int) -> UplinkViolation | None:
        """收一条 `item` 帧。

        @returns `end` 之后的 item → `gateway/protocol`、超限 → `gateway/uplink-overflow`；
        无违例返回 None。违例同时 `fail` 掉本队列（挂起读以它结束）。
        """
        if self._failure is not None or self._closed:
            return None
        if self._ended:
            return self._violate(UplinkViolation(
                "gateway/protocol",
                "api gateway: Remote stream uplink item after end",
                self._endpoint))
        if self._bytes + frame_bytes > self._max_bytes:
            return self._violate(UplinkViolation(
                "gateway/uplink-overflow",
                f"api gateway: Remote stream uplink exceeded {self._max_bytes} buffered bytes",
                self._endpoint))
        self._queue.append((value, frame_bytes))
        self._bytes += frame_bytes
        self._signal()
        return None

    def end(self) -> None:
        """客户端半关（`end` 帧）；幂等。"""
        if self._ended:
            return
        self._ended = True
        self._signal()

    def fail(self, error: BaseException) -> None:
        """以 `error` 结束消费者的下一次读；幂等，丢弃已缓冲帧。"""
        if self._failure is not None:
            return
        self._failure = error
        self._queue.clear()
        self._bytes = 0
        self._signal()

    def release(self) -> None:
        """消费者不再读：丢弃缓冲帧，之后到达的帧直接丢弃（上游 `return()`）。"""
        if self._closed:
            return
        self._closed = True
        self._queue.clear()
        self._bytes = 0
        self._signal()

    # ---------- 读取侧（宿主方法经 uplink() 迭代） ----------

    def __aiter__(self) -> "UplinkInbox":
        if self._taken:
            raise RuntimeError(
                "api gateway: Remote stream uplink inbox already has a consumer")
        self._taken = True
        return self

    async def __anext__(self) -> Any:
        while True:
            if self._closed:
                raise StopAsyncIteration
            if self._queue:
                value, frame_bytes = self._queue.popleft()
                self._bytes -= frame_bytes
                return value
            if self._failure is not None:
                raise self._failure
            if self._ended:
                raise StopAsyncIteration
            if self._pending:
                raise RuntimeError(
                    "api gateway: Remote stream uplink inbox has one pending read")
            self._pending = True
            self._wake.clear()
            try:
                await self._wake.wait()
            finally:
                self._pending = False

    # ---------- 内部 ----------

    def _violate(self, error: UplinkViolation) -> UplinkViolation:
        self.fail(error)
        return error

    def _signal(self) -> None:
        self._wake.set()


class UplinkDecodeError(RuntimeError):
    """上行项未过该方法的 codec：以 `gateway/input-invalid` 中止逻辑流。

    上游 `decode()`（index.ts:1493-1514）把 codec 失败折成这一码，消息带
    `typert gateway: <endpoint>: ` 前缀、`wire field "uplink" failed boundary
    validation` 诊断，details 为 `{endpoint, field}`（`TypertGatewayError`
    构造，`packages/typert/protocol/src/errors.ts:162+`）；mux 据此发终态
    `error` 帧。

    @param endpoint - 该次调用归属的 endpoint。
    """

    def __init__(self, endpoint: str):
        super().__init__(
            f'typert gateway: {endpoint}: wire field "uplink" failed '
            "boundary validation")
        self.code = "gateway/input-invalid"
        self.message = str(self)
        self.details = {"endpoint": endpoint, "field": "uplink"}


class UplinkItems:
    """方法侧读到的上行项迭代器：逐项过 codec，单消费者（上游 `UplinkDecoder`）。

    @param inbox - 该逻辑流的有界 inbox（载体写入侧）；`None` 表示调用方不是
        wire 载体、没有上行源，读立即结束（上游 `request.uplink ??
        EMPTY_ASYNC_ITERABLE`，index.ts:712）。
    @param endpoint - 该次调用归属的 endpoint（codec 失败进 details）。
    @param codec - 该方法声明的上行 codec：接收原始项、返回解码值，非法即抛；
        `None` 表示未声明，走无损 JSON 校验（上游 `SRC_JSON_CODEC`）。
    """

    def __init__(self, inbox: UplinkInbox | None, endpoint: str,
                 codec: Callable[[Any], Any] | None = None):
        self._inbox = inbox
        self._endpoint = endpoint
        self._codec = codec
        self._taken = False
        self._closed = False

    def __aiter__(self) -> "UplinkItems":
        if self._taken:
            raise RuntimeError(
                f"typert gateway: {self._endpoint}: invocation.uplink() is "
                "available once per call")
        self._taken = True
        return self

    async def __anext__(self) -> Any:
        if self._closed or self._inbox is None:
            raise StopAsyncIteration
        value = await self._inbox.__anext__()
        try:
            return self._decode(value)
        except UplinkDecodeError as error:
            # codec 失败即中止整条逻辑流（上游 `abort(failure)` 后重抛）。
            self._inbox.fail(error)
            raise
        except Exception as error:  # noqa: BLE001 - 非 codec 失败也按本流中止
            failure = UplinkDecodeError(self._endpoint)
            self._inbox.fail(failure)
            raise failure from error

    def close(self) -> None:
        """方法不再读上行：丢弃缓冲项，之后到达的帧直接丢弃（上游 `return()`）。"""
        if self._closed:
            return
        self._closed = True
        if self._inbox is not None:
            self._inbox.release()

    def _decode(self, value: Any) -> Any:
        if self._codec is None:
            if is_remote_json_value(value):
                return value
            raise UplinkDecodeError(self._endpoint)
        try:
            decoded = self._codec(value)
        except UplinkDecodeError:
            raise
        except Exception as error:  # noqa: BLE001 - 描述符 codec 的失败同折此码
            raise UplinkDecodeError(self._endpoint) from error
        if not is_remote_json_value(decoded):
            # 上游 decode() 在 codec 之后仍跑一次 assertJsonValue：解码结果本身
            # 必须是无损 JSON 值。
            raise UplinkDecodeError(self._endpoint)
        return decoded
