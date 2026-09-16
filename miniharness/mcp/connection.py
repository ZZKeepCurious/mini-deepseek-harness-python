"""连接 supervisor：为一个 MCP server 拥有客户端/传输代，保持 harness 工具
注册表与活跃代同步，连接断开时按有界指数退避重启 server。

对应 dsh 真实源码：packages/mcp/mcp-client/src/connection.ts。

一次 outage 共享一个尝试预算（`maxAttempts` 次连续失败，延迟自
`initialDelayMs` 起翻倍封顶 `maxDelayMs`）。超过稳定窗口（maxDelayMs 即
最长退避间隔）的连接结束了本次 outage，下一次断开的预算归零；崩溃循环的
server——即使连接短暂成功——仍耗尽上限而不会永远重启。预算耗尽注销该
server 的工具并停止；disposal（含 HMR）是唯一恢复途径。

载体差异（Python SDK 无 Client 实例/onclose）：transport 故障与
tools/list_changed 经 ClientSession 的 message_handler 观察；每个连接在
专属 asyncio 事件循环 + 守护线程中跑 SDK 的 async 生命周期（SDK 的
Protocol 与 asyncio transport 绑定 loop）。`_REGISTRY_LOCK` 串行化跨线程
的工具换代 swap 与注销（上游单线程事件循环无需锁；mini 补此线程闸）。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from mcp import ClientSession
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation, ToolListChangedNotification

from ..core.scope import Context
from .tools import ToolBridgeOptions, sync_tools
from .transport import create_http_parameters, create_stdio_parameters, pick_errlog
from .types import GENERATION_CLOSE_TIMEOUT_MS

logger = logging.getLogger("miniharness.mcp.connection")

#: 跨线程串行化工具注册换代与注销（mini 补充的线程闸，上游单线程无此约束）。
_REGISTRY_LOCK = threading.RLock()

#: 单次连接协商的读侧下限（initialize 等待，秒）。
_CONNECT_OPEN_TIMEOUT_S = 30.0

#: 握手 / 列表 RPC 超时（秒）。SDK stdio 传输在子进程退出时不投递 EOF
#: 事件（anyio drain 静默结束），故每项 RPC 必须有界。
_RPC_TIMEOUT_S = 5.0

#: 生存性心跳周期与单次 ping 超时（秒）。上游 dsh 自有传输在管道
#: 故障时投递 broken-pipe 异常——mcp-python-sdk stdio 传输不保证此行为；
#: 小型周期 ping 以有界延迟暴露静默死亡。
_LIFELINE_INTERVAL_S = 15.0
_LIFELINE_TIMEOUT_S = 3.0


class _BridgeAborted(RuntimeError):
    """调用被协作中止（exec.signal 置位）。"""


class _BridgeTimeout(RuntimeError):
    """调用超过 toolCallTimeoutMs 期限。"""


@dataclass
class _Generation:
    """一代连接：transport 上下文 + 会话 + 该代的信号位。"""

    seq: int
    cm: Any = None              # async context manager（stdio_client / streamable_http_client）
    read: Any = None
    write: Any = None
    session: ClientSession | None = None
    http_client: Any = None     # 仅 streamable-http 且显式 headers 时由本侧拥有
    lost: Any = field(default_factory=lambda: None)      # asyncio.Event：传输故障/关闭
    tools_dirty: Any = field(default_factory=lambda: None)  # asyncio.Event：tools/list_changed
    closed: bool = field(default=False)   # session+transport 已同任务关闭（幂等绞盘）
    closure_uncertain: bool = field(default=False)  # 关闭确认屏障未通过（fail closed）
    watchdog_task: Any = field(default=None)  # 心跳任务（同代生命周期）


def _call_tool_result_to_dict(result: Any) -> dict:
    """CallToolResult → 桥接侧 canonical dict（content/isError/structuredContent）。"""
    from .tools import _structured_to_dict

    data = result.model_dump(by_alias=True, exclude_none=True)
    out: dict[str, Any] = {"content": data.get("content") or [], "isError": bool(data.get("isError"))}
    if data.get("structuredContent") is not None:
        out["structuredContent"] = _structured_to_dict(data["structuredContent"])
    return out


class McpServerConnection:
    """一个有监督的 MCP server 连接的句柄。

    `start()` 后运行一个专属事件循环 + 守护线程管理连接代与重连；`ready`
    （concurrent.futures.Future）在首次连接尝试结算时解析为 `{}` 或
    `{'error': ...}`。`dispose()` 同步停止重连、关闭谈判中/活跃代、排空
    在途工作并注销本 server 全部工具。
    """

    def __init__(self, ctx: Context, config: dict, policy: dict):
        self._ctx = ctx
        self._config = config
        self._policy = policy
        self._server_name: str = config["serverName"]
        self._label = f"mcp-client({self._server_name})"
        self._opts = ToolBridgeOptions("contain", self._server_name, config["toolCallTimeoutMs"])
        self._startup_opts = ToolBridgeOptions(
            "throw" if config.get("failOnStartupError") else "contain",
            self._server_name,
            config["toolCallTimeoutMs"],
        )
        self._max_instruction_bytes = config.get("maxInstructionBytes", 32_768)
        self._tool_call_timeout_ms = config["toolCallTimeoutMs"]

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._drive_task: asyncio.Task | None = None
        self._stop_event = threading.Event()
        self._ready: concurrent.futures.Future = concurrent.futures.Future()
        self._disposed = False

        self._generation: _Generation | None = None
        self._state = "down"            # down | connecting | connected | stopped
        self._connected_at: float | None = None
        self._failed_attempts = 0
        self._attempts_run = 0
        self._first_attempt_error: Any = None
        self._retry_delay: float | None = None
        self._disposers: dict[str, Callable] = {}
        self._server_instructions = ""
        self._instructions_lock = threading.Lock()
        self._sync_chain: asyncio.Task | None = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """启动监督线程（幂等）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_supervisor, name=self._label, daemon=True)
        self._thread.start()

    @property
    def ready(self) -> concurrent.futures.Future:
        return self._ready

    def _run_supervisor(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._drive_task = loop.create_task(self._drive_wrapper())
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(self._final_shutdown())
            except Exception:  # noqa: BLE001 - 清理视为尽力而为
                logger.exception("%s: final shutdown failed", self._label)
            finally:
                try:
                    loop.run_until_complete(asyncio.sleep(0))
                except Exception:  # noqa: BLE001
                    pass
                loop.close()
                self._loop = None

    async def _drive_wrapper(self) -> None:
        try:
            await self._drive()
        except asyncio.CancelledError:
            pass
        except Exception as error:  # noqa: BLE001 - supervisor 永不因此逃逸
            logger.error("%s: supervisor crashed: %s", self._label, error)
        finally:
            self._settle_ready()
            try:
                self._loop.stop()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass

    async def _drive(self) -> None:
        async def _noop() -> None:
            return None

        self._sync_chain = asyncio.create_task(_noop())
        while not self._stop_event.is_set():
            outcome = await self._run_attempt(initial=(self._attempts_run == 0))
            self._settle_ready()
            if outcome == "stopped" or self._stop_event.is_set():
                break
            if self._retry_delay is not None:
                await self._sleep_retry(self._retry_delay)
                self._retry_delay = None
                continue
            break

    async def _sleep_retry(self, delay_ms: float) -> None:
        deadline = time.monotonic() + delay_ms / 1000
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(min(0.1, max(0.01, deadline - time.monotonic())))

    # ---------- 连接尝试 ----------

    def _is_current(self, generation: _Generation) -> bool:
        return (not self._disposed and self._generation is generation
                and self._state in ("connecting", "connected"))

    async def _run_attempt(self, initial: bool) -> str:
        """完整运行一代：协商 → 初始 sync → 联网监视 → 同任务清理。

        会话与传输都在本协程（drive 任务）内进入并退出——SDK 的 anyio
        task group cancel scope 只允许被进入它的任务退出（Windows 上跨任务
        退出会报 "different task" 并悬置 group）。dispose 取消 drive 时
        CancelledError 也流经本函数，finally 清理仍发生在同一任务。
        """
        self._attempts_run += 1
        generation = _Generation(seq=self._attempts_run,
                                 lost=asyncio.Event(), tools_dirty=asyncio.Event())
        self._generation = generation
        self._state = "connecting"
        async def _run() -> None:
            cm, read, write, http_client = await self._open_transport(generation)
            generation.cm, generation.read, generation.write = cm, read, write
            generation.http_client = http_client
            if self._stop_event.is_set():
                raise RuntimeError(f"{self._label}: stopping")
            session = ClientSession(
                read,
                write,
                message_handler=self._on_message,
                client_info=Implementation(name="dsh-mcp-client", version="0.0.1"),
            )
            generation.session = session
            try:
                await session.__aenter__()  # 启动 dispatcher（SDK 必须 before any request）
                await self._run_rpc(generation, session.initialize(), "initialize",
                                    timeout_s=_CONNECT_OPEN_TIMEOUT_S)
            except asyncio.TimeoutError as error:
                raise RuntimeError(f"{self._label}: MCP handshake timed out") from error
            if generation.lost.is_set():
                raise RuntimeError(f"{self._label}: transport closed during handshake")
            self._snapshot_instructions(session)
            await self._enqueue_sync(generation, self._startup_opts if initial else self._opts)
            self._state = "connected"
            self._connected_at = time.monotonic()
            self._settle_ready()  # 首次连接 + 初始 sync 完成即结算 ready
            if self._failed_attempts > 0:
                logger.info("%s: reconnected and re-synced tools (attempt %s/%s)",
                            self._label, self._failed_attempts, self._policy["maxAttempts"])
            generation.watchdog_task = asyncio.ensure_future(self._watchdog_loop(generation))
            await self._wait_for_life()
        try:
            try:
                await _run()
            finally:
                await self._settle_generation_close(generation)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 对齐上游 connectGeneration 从不抛
            self._first_attempt_error = self._first_attempt_error or error
            if self._is_current(generation):
                logger.warning("%s: connection attempt failed: %s", self._label, error)
            if self._is_current(generation) and generation.closure_uncertain:
                logger.error(
                    "%s: failed generation could not confirm transport closure — "
                    "reconnect stopped to avoid overlapping server processes; "
                    "reload the plugin or restart the Host to retry", self._label)
                self._state = "stopped"
                self._connected_at = None
                return "stopped"
            if self._state != "stopped":
                self._generation_down(lost_established=False)
            return "failed"
        stopped = self._stop_event.is_set() or self._state == "stopped"
        if not stopped and generation.lost.is_set() and self._generation is generation:
            self._generation_down(lost_established=True)
            stopped = self._state == "stopped"
        return "stopped" if stopped else "failed"

    async def _open_transport(self, generation: _Generation) -> tuple:
        """打开一代传输并进入其 async CM；返回 (cm, read, write, http_client)。"""
        if self._config["transport"] == "stdio":
            from .transport import create_stdio_parameters

            params = create_stdio_parameters(self._config)
            cm = stdio_client(params, pick_errlog())
            read, write = await cm.__aenter__()
            return cm, read, write, None
        params = create_http_parameters(self._config)
        headers = params.get("headers") or {}
        http_client = None
        if headers:
            import httpx

            http_client = httpx.AsyncClient(headers=headers, timeout=None)
        cm = streamable_http_client(params["url"], http_client=http_client)
        try:
            read, write = await cm.__aenter__()
        except Exception:
            if http_client is not None:
                await http_client.aclose()
            raise
        return cm, read, write, http_client

    async def _settle_generation_close(self, generation: _Generation) -> bool:
        """同任务关闭一代：watchdog → 会话 dispatcher → 传输 CM + http client → 确认屏障。

        幂等：一旦关闭（closed=True）不再重复（dispose/final-shutdown 也调用，
        但代关闭总是先发生在 drive 任务内）。
        """
        if generation.closed:
            return True
        generation.closed = True
        watchdog = generation.watchdog_task
        if watchdog is not None and not watchdog.done():
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
        session = generation.session
        try:
            if session is not None:
                await session.__aexit__(None, None, None)
        except Exception as error:  # noqa: BLE001 - 关闭尽力而为，仍走确认屏障
            logger.debug("%s: session exit: %s", self._label, error)
        cm = generation.cm
        http_client = generation.http_client
        if cm is None and http_client is None:
            return True  # 无重物在轨（如进程从未 spawn），无孤儿进程风险
        try:
            if cm is not None:
                await cm.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001 - 关闭是尽力而为，之后仍走确认屏障
            pass
        if http_client is not None:
            try:
                await http_client.aclose()
            except Exception:  # noqa: BLE001
                pass
        if generation.lost is None:
            return True
        deadline = time.monotonic() + GENERATION_CLOSE_TIMEOUT_MS / 1000
        while time.monotonic() < deadline:
            if generation.lost.is_set():
                return True
            await asyncio.sleep(0.03)
        generation.closure_uncertain = True
        return False

    def _snapshot_instructions(self, session: ClientSession) -> None:
        """归因 server instructions 快照 + 字节上限检查（对齐上游验证）。"""
        server_text = (getattr(session, "instructions", None) or "").strip()
        instructions = f"### MCP server: {self._server_name}\n\n{server_text}" if server_text else ""
        if len(instructions.encode("utf-8")) > self._max_instruction_bytes:
            raise RuntimeError(
                f"{self._label}: server instructions exceed maxInstructionBytes "
                f"({self._max_instruction_bytes})")
        with self._instructions_lock:
            self._server_instructions = instructions

    def instructions(self) -> str:
        """当前归因 instructions（线程安全读）。"""
        with self._instructions_lock:
            return self._server_instructions

    # ---------- 断开与重连 ----------

    def _on_generation_down(self, lost_established: bool) -> None:
        self._state = "down"
        uptime = None
        if lost_established and self._connected_at is not None:
            uptime = time.monotonic() - self._connected_at
        self._connected_at = None
        if not self._policy["enabled"]:
            message = (
                "connection lost and reconnect is disabled — registered tools will fail "
                "until an HMR reload or Host restart" if lost_established else
                "connection failed and reconnect is disabled — no tools were registered; "
                "reload the plugin or restart the Host to connect")
            logger.error("%s: %s", self._label, message)
            self._state = "stopped"
            return
        if uptime is not None and uptime >= self._policy["maxDelayMs"] / 1000:
            self._failed_attempts = 0
        self._failed_attempts += 1
        if self._failed_attempts > self._policy["maxAttempts"]:
            self._give_up()
            return
        delay_ms = min(self._policy["maxDelayMs"],
                       self._policy["initialDelayMs"] * 2 ** (self._failed_attempts - 1))
        action = "connection lost; reconnecting" if lost_established else "connection failed; retrying"
        logger.warning("%s: %s in %sms (attempt %s/%s)",
                       self._label, action, int(delay_ms),
                       self._failed_attempts, self._policy["maxAttempts"])
        self._retry_delay = float(delay_ms)

    def _give_up(self) -> None:
        """预算耗尽：排队注销全部工具（不能与在途 sync 的 swap 竞态）。"""
        previous = self._sync_chain

        async def _run() -> None:
            try:
                if previous is not None:
                    await previous
            except Exception:  # noqa: BLE001 - 链尾必须存活
                pass
            with _REGISTRY_LOCK:
                for dispose in self._disposers.values():
                    dispose()
                self._disposers = {}
            with self._instructions_lock:
                self._server_instructions = ""
            logger.error(
                "%s: giving up after %s consecutive failed reconnect attempts — tools "
                "unregistered; reload the plugin or restart the Host to reconnect",
                self._label, self._policy["maxAttempts"])

        self._state = "stopped"
        self._append_chain(_run())

    def _generation_down(self, lost_established: bool) -> None:
        self._on_generation_down(lost_established)

    async def _wait_for_life(self) -> None:
        """保持联网：服务 tools/list_changed 重同步，直到代丢失或 stop。

        代下线后的结算（生成关停倒计时 + 预算）由 drive 在清理后统一做。
        """
        generation = self._generation
        while not self._stop_event.is_set() and generation is not None:
            if generation.lost.is_set():
                break
            if generation.tools_dirty.is_set():
                generation.tools_dirty.clear()
                self._enqueue_sync(generation, self._opts)
            try:
                await asyncio.wait_for(
                    asyncio.gather(generation.lost.wait(), generation.tools_dirty.wait()),
                    timeout=0.2)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 轮询超时后重查条件
                pass

    async def _run_rpc(self, generation: _Generation, coro: Coroutine, op: str,
                       timeout_s: float = _RPC_TIMEOUT_S) -> Any:
        """运行握手期 RPC，与代丢失信号竞速；任一边先到即终止。

        SDK stdio 传输在子进程死亡时不结束在途 RPC（drain 静默消费 EOF），
        必须以代丢失信号抢先中止，否则 initialize/list_tools 会挂到超时。
        """
        rpc = asyncio.ensure_future(coro)
        lost = asyncio.ensure_future(generation.lost.wait())
        done, pending = await asyncio.wait(
            {rpc, lost}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED)
        if pending:
            for fut in pending:
                fut.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if lost in done:
            raise RuntimeError(f"{self._label}: transport closed during {op}")
        if rpc in done:
            return rpc.result()
        raise RuntimeError(f"{self._label}: {op} RPC timed out")

    async def _watchdog_loop(self, generation: _Generation) -> None:
        """周期性 ping 检测服务存活。

        SDK stdio 传输在子进程退出时不投递 EOF/异常（poll 循环静默结束，
        见 mcp/client/stdio.py stdout_reader）。上游 dsh 自有运输在管道
        故障时投递 broken-pipe 异常 → on_message(exception) 立即置位 lost。
        Python SDK 的 StdioTransport 不保证此行为，故以 ping 有界超时弥补
        （carrier 差异，见 verified-diffs）。
        """
        while not self._stop_event.is_set() and self._is_current(generation):
            await asyncio.sleep(_LIFELINE_INTERVAL_S)
            if not self._is_current(generation) or generation.lost.is_set():
                break
            session = generation.session
            if session is None:
                break
            try:
                await asyncio.wait_for(session.send_ping(), timeout=_LIFELINE_TIMEOUT_S)
            except Exception:  # noqa: BLE001 - 任何失败视为连接丧失
                logger.warning("%s: lifeline ping failed (server likely dead)", self._label)
                generation.lost.set()
                break

    async def _on_message(self, message: Any) -> None:
        """ClientSession message_handler：观察传输故障与 tools/list_changed。

        在 supervisor loop 线程内联调用（dispatcher 通知循环），只做非阻塞
        信号置位。
        """
        generation = self._generation
        if generation is None:
            return
        if isinstance(message, Exception):
            generation.lost.set()
        elif isinstance(message, ToolListChangedNotification):
            generation.tools_dirty.set()

    # ---------- 同步链 ----------

    def _append_chain(self, awaitable: Any) -> None:
        """把任务接到同步链尾（链尾存活于失败）。"""
        if self._sync_chain is None:
            self._sync_chain = asyncio.ensure_future(awaitable)
            return
        task = asyncio.ensure_future(awaitable)

        async def _tail() -> None:
            try:
                await task
            except Exception:  # noqa: BLE001 - 链尾必须存活
                pass

        self._sync_chain = asyncio.create_task(_tail())

    def _enqueue_sync(self, generation: _Generation, opts: ToolBridgeOptions) -> Coroutine:
        """排队一次 syncTools；返回可等待的结果 task（初期要感知启动语义）。

        `_run` 只等待调用时刻之前的链尾，再把自己作为新链尾挂上（上游
        `syncToolsCurrent` 的 prev-chain 语义）；否则链尾包含自身会死锁。
        """
        previous = self._sync_chain

        async def _run() -> None:
            if previous is not None:
                try:
                    await previous
                except Exception:  # noqa: BLE001 - 前尾失败不影响本次
                    pass
            if not self._is_current(generation):
                return
            try:
                with _REGISTRY_LOCK:
                    disposers = await sync_tools(self, self._ctx, opts, self._disposers)
                if self._is_current(generation):
                    self._disposers = disposers
            except Exception as error:  # noqa: BLE001 - contain/throw 语义在 opts
                if opts.registrationFailure == "throw" and self._is_current(generation):
                    raise
                logger.error("%s: tool sync failed: %s", self._label, error)

        task = asyncio.create_task(_run())
        self._append_chain(task)
        return task

    # ---------- sync_tools 视角 ----------

    @property
    def server_capabilities(self) -> Any:
        """当前代的 server capabilities（disconnected → None）。"""
        generation = self._generation
        if generation is None or generation.session is None:
            return None
        return generation.session.server_capabilities

    async def list_tools(self) -> list:
        """当前代的工具列表（在 supervisor loop 线程内调用）。

        初次/重连 sync 在状态 `connecting` 下进行（state 到 `connected` 在
        sync 完成后才置位），故只要求当前代活跃即可。
        """
        generation = self._generation
        if generation is None or generation.session is None \
                or self._state not in ("connecting", "connected"):
            raise RuntimeError(f"{self._label}: server is disconnected")
        result = await self._run_rpc(generation, generation.session.list_tools(), "list_tools")
        return list(result.tools or [])

    # ---------- 工具调用 / 资源请求桥 ----------

    def _call_tool_async(self, name: str, args: dict) -> Coroutine:
        async def _run() -> Any:
            generation = self._generation
            if generation is None or generation.session is None or self._state != "connected":
                raise RuntimeError(f"{self._label}: server is disconnected")
            try:
                result = await generation.session.call_tool(name=name, arguments=args)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - 由执行侧规范化
                raise RuntimeError(f"{self._label}: MCP call failed: {error}") from error
            return _call_tool_result_to_dict(result)

        return _run()

    async def _run_on_loop(self, coro_factory: Callable[[], Coroutine], exec_: Any) -> Any:
        """在 supervisor loop 上运行一个协程，等待结果；中止/超时取消它。

        从任意线程可用：loop 绑定由 run_coroutine_threadsafe 保证；在运行中
        的事件循环内以轮询 await 等待而不阻塞（对齐上游 timeout 语义）。
        """
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError(f"{self._label}: server is disconnected")
        fut = asyncio.run_coroutine_threadsafe(coro_factory(), self._loop)
        timeout_s = self._tool_call_timeout_ms / 1000
        deadline = time.monotonic() + timeout_s
        while True:
            if fut.done():
                try:
                    return fut.result()
                except Exception as error:  # noqa: BLE001 - 异常原样交给执行侧
                    raise error
            signal = getattr(exec_, "signal", None)
            aborted = signal is not None and getattr(signal, "is_set", None) is not None \
                and signal.is_set()
            if aborted:
                fut.cancel()
                raise _BridgeAborted(f"{self._label}: MCP tool call aborted")
            if time.monotonic() >= deadline:
                fut.cancel()
                raise _BridgeTimeout(
                    f"{self._label}: MCP tool call timed out after {self._tool_call_timeout_ms} ms")
            await asyncio.sleep(0.03)

    def call_tool(self, name: str, args: dict, exec_: Any) -> Any:
        """桥入口（sync_tools 的 call 回调）：返回 canonical McpResult dict。"""
        return self._run_on_loop(lambda: self._call_tool_async(name, args), exec_)

    def resources_request(self, request: dict, exec_: Any) -> Any:
        """mcp-resources provider 的 request 桥。"""
        return self._run_on_loop(lambda: self._resource_async(request), exec_)

    async def _resource_async(self, request: dict) -> Any:
        generation = self._generation
        if generation is None or generation.session is None or self._state != "connected":
            raise RuntimeError(f"{self._label}: server is disconnected")
        session = generation.session
        method = request.get("method")
        cursor = request.get("cursor")
        try:
            if method == "resources/list":
                result = await session.list_resources({"cursor": cursor} if cursor is not None else None)
            elif method == "resources/templates/list":
                result = await session.list_resource_templates(
                    {"cursor": cursor} if cursor is not None else None)
            elif method == "resources/read":
                result = await session.read_resource({"uri": request["uri"]})
            else:  # 封闭工具有联：未知方法属编程错误
                raise AssertionError(f"unknown MCP resource method: {method}")
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 由执行侧规范化
            raise RuntimeError(f"{self._label}: MCP resource request failed: {error}") from error
        return result.model_dump(by_alias=True, exclude_none=True)

    # ---------- 结算 ----------

    def _settle_ready(self) -> None:
        if self._ready is None or self._ready.done() or self._attempts_run == 0:
            return
        if self._state == "connected":
            self._ready.set_result({})
        else:
            error = self._first_attempt_error or RuntimeError(f"{self._label}: initial connection failed")
            self._ready.set_result({"error": error})

    async def _final_shutdown(self) -> None:
        self._state = "stopped"
        generation = self._generation
        self._generation = None
        if generation is not None:
            watchdog = generation.watchdog_task
            if watchdog is not None and not watchdog.done():
                watchdog.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog
            try:
                await self._settle_generation_close(generation)
            except Exception:  # noqa: BLE001 - 尽力而为
                pass
        try:
            if self._sync_chain is not None:
                await asyncio.wait_for(self._sync_chain, timeout=5)
        except Exception:  # noqa: BLE001
            pass
        with _REGISTRY_LOCK:
            for dispose in self._disposers.values():
                dispose()
            self._disposers = {}
        with self._instructions_lock:
            self._server_instructions = ""

    def dispose(self) -> None:
        """同步停止监督线程：停止重连、关闭时代、排空在途、注销全部工具。"""
        if self._disposed:
            return
        self._disposed = True
        self._stop_event.set()
        loop = self._loop
        if loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._wake_drive(), loop).result(timeout=10)
            except Exception:  # noqa: BLE001 - loop 可能已停止
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=15)

    async def _wake_drive(self) -> None:
        task = self._drive_task
        if task is not None and not task.done():
            task.cancel()