"""一条经子进程 spawn 的语言服务器上的 JSON-RPC 端点。

拥有 id 关联、出站 request/notification、入站 server→client 请求：对
`workspace/configuration` 以静态配置作答，拒绝 `workspace/applyEdit`（本 host 从不应用
编辑或运行命令）。对 stderr 设界，把分帧/解码失败暴露为致命关闭，并经句柄暴露受管范围
终止。对齐 packages/lsp/lsp-stdio/src/connection.ts。

载体差异（登记）：上游经 `ctx.subprocess.spawn`（受管子进程 + 进程树 containment +
spill stderr）；mini 用 `asyncio.create_subprocess_exec` 直接 spawn（Popen 等价，无
OS 级受管范围/进程树 containment），stderr 以内存有界尾承载（同 §3.45 载体纪律）。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from .framing import MessageDecoder, encode_message

__all__ = ["LspConnection"]


class LspConnection:
    """绑定单个子进程的存活 JSON-RPC 端点。"""

    def __init__(self, spec: dict, on_server_request: Callable[[str, Any], Any]):
        self._spec = spec
        self._decoder = MessageDecoder(spec["max_message_bytes"])
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._close_reason: BaseException | None = None
        self._stderr_bytes = b""
        self._max_stderr_bytes = spec["max_stderr_bytes"]
        self._on_server_request = on_server_request
        self._proc: asyncio.subprocess.Process | None = None
        self._closed = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        """spawn 服务端并启动读端任务。"""
        self._proc = await asyncio.create_subprocess_exec(
            self._spec["command"], *self._spec["args"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._spec["cwd"],
            env=self._spec.get("env") or None,
        )
        self._tasks.append(asyncio.ensure_future(self._read_stdout()))
        self._tasks.append(asyncio.ensure_future(self._read_stderr()))
        self._tasks.append(asyncio.ensure_future(self._wait_exit()))

    async def _read_stdout(self) -> None:
        stdout = self._proc.stdout
        try:
            while True:
                chunk = await stdout.read(65536)
                if not chunk:
                    break
                try:
                    messages = self._decoder.push(chunk)
                except Exception as error:  # noqa: BLE001 - 分帧失败不可恢复
                    self._fail(error)
                    self.terminate()
                    return
                for message in messages:
                    self._dispatch(message)
        except Exception as error:  # noqa: BLE001
            self._fail(error)

    async def _read_stderr(self) -> None:
        stderr = self._proc.stderr
        try:
            while True:
                chunk = await stderr.read(65536)
                if not chunk:
                    break
                self._stderr_bytes = (self._stderr_bytes + chunk)[-self._max_stderr_bytes:]
        except Exception:  # noqa: BLE001 - stderr 是尽力诊断面
            pass

    async def _wait_exit(self) -> None:
        await self._proc.wait()
        reason: BaseException = RuntimeError(self._exit_message())
        if self._close_reason is None:
            self._close_reason = reason
        self._fail_all(self._close_reason)
        self._closed.set()

    def _exit_message(self) -> str:
        tail = self.stderr_tail.strip()
        return ("language server exited" if tail == ""
                else f"language server exited; stderr: {tail}")

    # ---------- 观察 ----------

    @property
    def stderr_tail(self) -> str:
        return self._stderr_bytes.decode("utf-8", "replace")

    @property
    def failed(self) -> bool:
        return self._close_reason is not None

    def failed_with(self, error: Any) -> bool:
        return self._close_reason is error

    @property
    def pid(self) -> int | None:
        return None if self._proc is None else self._proc.pid

    async def wait_closed(self) -> None:
        await self._closed.wait()

    # ---------- JSON-RPC ----------

    def peek_next_id(self) -> int:
        """下一次 `request()` 将使用的 id（供 instance 预置 cancel）。"""
        return self._next_id

    async def request(self, method: str, params: Any) -> Any:
        if self._close_reason is not None:
            raise self._close_reason
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write({"jsonrpc": "2.0", "id": request_id,
                               "method": method, "params": params})
        except Exception:  # noqa: BLE001 - _write 已把失败记在连接上并拒绝挂起请求
            pass
        return await future

    async def notify(self, method: str, params: Any) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def cancel(self, request_id: int) -> None:
        """为在途请求 id 发 `$/cancelRequest`（尽力而为；忽略写失败）。"""
        async def _send() -> None:
            try:
                await self._write({"jsonrpc": "2.0", "method": "$/cancelRequest",
                                   "params": {"id": request_id}})
            except Exception:  # noqa: BLE001
                pass

        asyncio.ensure_future(_send())

    def terminate(self) -> None:
        """终止服务端（幂等）。"""
        if self._proc is None or self._proc.returncode is not None:
            return
        try:
            self._proc.terminate()
        except ProcessLookupError:
            pass

    def kill(self) -> None:
        """强杀服务端（SIGTERM 宽限后升级；幂等）。"""
        if self._proc is None or self._proc.returncode is not None:
            return
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass

    async def wait_for_managed_range_exit(self, signal: Any = None) -> bool:
        """等自有受管范围清空。mini 无进程组受管范围：等价于等服务端进程退出。"""
        await self._closed.wait()
        return True

    # ---------- 内部 ----------

    async def _write(self, message: Any) -> None:
        if self._close_reason is not None:
            raise self._close_reason
        try:
            self._proc.stdin.write(encode_message(message))
            await self._proc.stdin.drain()
        except Exception as error:  # noqa: BLE001 - stdin 断裂 = 致命连接错误
            self._fail(error)
            raise

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        request_id = message.get("id")
        method = message.get("method")
        if isinstance(method, str) and isinstance(request_id, (int, str)):
            asyncio.ensure_future(
                self._handle_server_request(request_id, method, message.get("params")))
            return
        if isinstance(method, str):
            return  # server→client 通知（诊断/日志）：本 MVP host 忽略
        if isinstance(request_id, int):
            self._handle_response(request_id, message)

    async def _handle_server_request(self, request_id: Any, method: str, params: Any) -> None:
        try:
            result = await self._on_server_request(method, params)
            await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as error:  # noqa: BLE001
            try:
                await self._write({"jsonrpc": "2.0", "id": request_id,
                                   "error": {"code": -32601, "message": str(error)}})
            except Exception:  # noqa: BLE001
                pass

    def _handle_response(self, request_id: int, frame: dict) -> None:
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return
        error = frame.get("error")
        if isinstance(error, dict):
            future.set_exception(RuntimeError(error.get("message", "LSP error response")))
        else:
            future.set_result(frame.get("result"))

    def _fail(self, error: BaseException) -> None:
        if isinstance(error, asyncio.CancelledError):
            error = RuntimeError("LSP connection cancelled")
        if self._close_reason is None:
            self._close_reason = error
        self._fail_all(error)

    def _fail_all(self, error: BaseException) -> None:
        waiting = list(self._pending.values())
        self._pending.clear()
        for future in waiting:
            if not future.done():
                future.set_exception(error)
