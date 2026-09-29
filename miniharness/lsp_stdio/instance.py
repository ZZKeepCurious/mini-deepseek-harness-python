"""一个语言服务器实例：一条连接 + initialize 握手 + 串行可取消查询队列 +
瞬态 `didOpen`→request→`didClose` 生命周期 + 有界拆除。

一个实例拥有一个 `(provider id, 规范工作区)` 进程。查询经单一队列串行，使一个未能
止住服务端的取消可以终止它而不牵连同批其它工作；不同实例并行。对齐
packages/lsp/lsp-stdio/src/instance.ts。
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..lsp import LspError
from .abort import abort_error, abortable, signal_aborted
from .connection import LspConnection
from .translate import (
    negotiate_position_encoding,
    normalize_hover,
    normalize_locations,
    request_method,
    supports_operation,
    supports_transient_open,
)

__all__ = ["LspInstance"]

#: server→client 请求中本 host 以空结果确认（无动态注册）的方法。
_LIFECYCLE_NOOP_METHODS = frozenset({
    "window/workDoneProgress/create",
    "client/registerCapability",
    "client/unregisterCapability",
})

#: initialize 时宣告的客户端能力（UTF-16 位置、工作区文件夹/配置、markdown/plaintext
#: hover、definition/implementation 的 link 支持；无动态注册）。
_CLIENT_CAPABILITIES = {
    "general": {"positionEncodings": ["utf-16"]},
    "workspace": {"workspaceFolders": True, "configuration": True},
    "textDocument": {
        "synchronization": {"dynamicRegistration": False},
        "hover": {"contentFormat": ["markdown", "plaintext"]},
        "definition": {"linkSupport": True},
        "implementation": {"linkSupport": True},
        "references": {},
    },
}


class LspInstance:
    """一个已初始化的服务端进程（由 provider 池化，不直接导出为 provider）。"""

    def __init__(self, spec: dict, on_server_request: Any = None):
        self._spec = spec
        self._connection = LspConnection(spec, self._answer_server_request)
        self._capabilities: dict | None = None
        # 串行尾：下一个查询等它（对齐上游 Promise 链；绝不取消前一个）。
        self._tail: asyncio.Future | None = None
        self._disposed = False
        self._teardown: asyncio.Future | None = None
        self._process_closed = False
        self._ready: asyncio.Future | None = None

    async def start(self) -> None:
        """spawn 服务端并启动 initialize 握手。"""
        await self._connection.start()
        self._ready = asyncio.ensure_future(self._initialize())
        # 消费握手异常，避免未 await 时的 "Task exception was never retrieved"；
        # 首次查询 attach 真正的处理器时仍会重新抛出。
        self._ready.add_done_callback(
            lambda task: task.cancelled() or task.exception())
        asyncio.ensure_future(self._mark_process_closed())

    async def _mark_process_closed(self) -> None:
        await self._connection.wait_closed()
        self._process_closed = True

    @property
    def dead(self) -> bool:
        """同步存活判定：进程已关闭或实例已 disposed 即真。"""
        return self._process_closed or self._disposed or self._connection.failed

    def is_transport_failure(self, error: Any) -> bool:
        return self._connection.failed_with(error)

    async def query(self, request: dict, source: dict, signal: Any = None) -> dict:
        """经串行队列跑一次查询（对齐上游 query 的 Promise 链）。

        等前一个查询时也观察 abort：若前一个查询挂起，后一个查询仍能放弃等待。
        串行尾跟随**实际**前序工作，故放弃等待的调用方不会使队列去串行化。
        """
        loop = asyncio.get_running_loop()
        prior = self._tail
        run_done = loop.create_future()
        self._tail = run_done
        if prior is not None:
            try:
                await abortable(prior, signal)
            except Exception:  # noqa: BLE001 - 等待期取消：前序不动，本槽标记完成
                if not run_done.done():
                    run_done.set_result(None)
                raise
        try:
            result = await self._run_query(request, source, signal)
        except Exception as error:  # noqa: BLE001
            if not run_done.done():
                run_done.set_result(None)
            if self.is_transport_failure(error):
                await self._await_teardown_attempt()
            raise
        if not run_done.done():
            run_done.set_result(None)
        return result

    # ---------- 握手 ----------

    async def _initialize(self) -> None:
        result = await self._connection.request("initialize", {
            # 子进程 provider 可能在另一 PID 命名空间/机器；宿主 PID 会让服务器监控无关进程。
            "processId": None,
            "rootUri": self._spec["workspace_uri"],
            "workspaceFolders": [{"uri": self._spec["workspace_uri"], "name": "workspace"}],
            "capabilities": _CLIENT_CAPABILITIES,
            "initializationOptions": self._spec.get("initialization_options"),
        })
        capabilities = result["capabilities"]
        # 省略的编码缺省 utf-16；其它值是本 host 拒绝的协议错误。
        negotiate_position_encoding(capabilities.get("positionEncoding"))
        self._capabilities = capabilities
        await self._connection.notify("initialized", {})

    # ---------- 查询 ----------

    async def _run_query(self, request: dict, source: dict, signal: Any = None) -> dict:
        if self._disposed:
            raise LspError("LSP instance was disposed", "LSP_DISPOSED")
        if signal_aborted(signal):
            raise abort_error(signal)
        try:
            await abortable(self._ready, signal)
        except Exception as error:  # noqa: BLE001 - 握手中止/失败不得池化
            if not self.dead:
                await self._await_teardown_attempt()
            raise error
        capabilities = self._capabilities
        if capabilities is None:
            raise RuntimeError("LSP instance is not initialized")
        if not supports_operation(capabilities, request["operation"]):
            raise LspError(f'server does not support {request["operation"]}',
                           "LSP_UNSUPPORTED_OPERATION")
        if not supports_transient_open(capabilities.get("textDocumentSync")):
            raise LspError(
                "server does not support the transient textDocument/didOpen this host requires",
                "LSP_UNSUPPORTED_OPERATION")

        uri = source["file_url"]
        opened = False
        try:
            if signal_aborted(signal):
                raise abort_error(signal)
            try:
                await abortable(self._connection.notify("textDocument/didOpen", {
                    "textDocument": {"uri": uri, "languageId": request["languageId"],
                                     "version": 1, "text": source["text"]},
                }), signal)
            except Exception as error:  # noqa: BLE001 - 取消的写/断流使协议流不可用
                await self._await_teardown_attempt()
                raise error
            opened = True
            payload = await self._send_request(
                request["operation"], uri, request["position"], signal)
            return self._normalize(request["operation"], payload)
        finally:
            if opened and not self.dead:
                try:
                    await self._connection.notify(
                        "textDocument/didClose", {"textDocument": {"uri": uri}})
                except Exception:  # noqa: BLE001
                    await self._await_teardown_attempt()

    async def _send_request(self, operation: str, uri: str, position: dict,
                            signal: Any = None) -> Any:
        params: dict = {
            "textDocument": {"uri": uri},
            "position": {"line": position["line"], "character": position["character"]},
        }
        # findReferences 恒含声明：调用方无旗标，影响分析绝不省略定义点。
        if operation == "findReferences":
            params["context"] = {"includeDeclaration": True}
        request_id = self._connection.peek_next_id()
        send = asyncio.ensure_future(
            self._connection.request(request_method(operation), params))
        if signal is None:
            return await send
        return await self._race_abort(send, request_id, signal)

    async def _race_abort(self, send: asyncio.Future, request_id: int, signal: Any) -> Any:
        try:
            return await abortable(send, signal)
        except Exception as error:  # noqa: BLE001
            if not signal_aborted(signal):
                raise
            self._connection.cancel(request_id)
            # 有界等服务器确认取消；若未在宽限内结算，请求仍在跑 → 终止实例。
            grace = self._spec["kill_grace_ms"] / 1000
            done, _pending = await asyncio.wait({send}, timeout=grace)
            if not done:
                await self._await_teardown_attempt()
            raise error

    def _normalize(self, operation: str, payload: Any) -> dict:
        if operation == "hover":
            return {"kind": "hover", "hover": normalize_hover(payload)}
        return {"kind": "locations", "locations": normalize_locations(payload),
                "resolvedWorkspaceUri": self._spec["workspace_uri"]}

    async def _answer_server_request(self, method: str, params: Any) -> Any:
        if method == "workspace/configuration":
            record = params if isinstance(params, dict) else {}
            items = record.get("items")
            items = items if isinstance(items, list) else []
            return [self._spec.get("configuration") for _ in items]
        if method in _LIFECYCLE_NOOP_METHODS:
            return None
        if method == "workspace/applyEdit":
            raise RuntimeError("workspace/applyEdit is not permitted by this host")
        raise RuntimeError(f"unsupported server request: {method}")

    # ---------- 拆除 ----------

    def terminate(self) -> None:
        """同步终止服务端进程（供 effect 拆除路径立即回收子进程）。"""
        self._disposed = True
        self._connection.terminate()

    async def dispose(self) -> None:
        await self._start_teardown()

    def _start_teardown(self) -> asyncio.Future:
        self._disposed = True
        if self._teardown is None:
            self._teardown = asyncio.ensure_future(self._tear_down())
        return self._teardown

    async def _await_teardown_attempt(self) -> None:
        try:
            await self._start_teardown()
        except Exception:  # noqa: BLE001 - provider 层重新 await 并合并结果
            pass

    async def _tear_down(self) -> None:
        try:
            await asyncio.wait_for(
                self._graceful_shutdown(), timeout=self._spec["shutdown_timeout_ms"] / 1000)
        except Exception:  # noqa: BLE001 - 优雅关闭失败/超时；下面的强终止才是权威
            pass
        await self._force_terminate()

    async def _graceful_shutdown(self) -> None:
        await self._connection.request("shutdown", None)
        await self._connection.notify("exit", None)
        await self._connection.wait_closed()

    async def _force_terminate(self) -> None:
        self._connection.terminate()
        try:
            await asyncio.wait_for(self._connection.wait_closed(),
                                   timeout=self._spec["kill_grace_ms"] / 1000)
        except asyncio.TimeoutError:
            self._connection.kill()
            await self._connection.wait_closed()
        await self._connection.wait_for_managed_range_exit()
