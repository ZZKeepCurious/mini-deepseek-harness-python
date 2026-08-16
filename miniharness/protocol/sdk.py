"""第 7 章：SDK 线协议 —— newline-delimited JSON-RPC 2.0 信封 + 最小运行服务。

对应 dsh 真实源码：packages/sdk/protocol（JsonRpcLineTransport + types.ts）。

上游语义（已核实，transport.ts + README.md）：
  * 每行一个紧凑 JSON 帧。id+method → 请求；仅 id → 响应；仅 method → 通知。
  * 畸形 JSON 行忽略；无 request handler → -32601；handler 抛错 → -32603。
  * 错误响应以 JsonRpcResponseError 拒绝 pending（保留 wire code 与 data）。
  * request id = 'req_' + uuid（无连字符）；notify 的 params 可省略（不带成员）。
  * params 归一化：数组/标量折叠为 {}（objectParams）。
  * 通知无 handler 直接丢弃；close 拒绝全部 pending 而不销毁流。
  * wire 类型：请求 initialize（cwd/provider/model/maxTokens? → serverInfo
    {name:'deepseek-harness-sdk-runtime', version}）、session/prompt
    （sessionId/contentBlocks → messageId，未知 id 懒创建会话）、shutdown
    （→ {}）；通知 session.event / session.status / subagent.started /
    subagent.finished（mini 以内存仿真承载）。
  * serverInfo.name 是 wire 稳定标识 deepseek-harness-sdk-runtime。

载体简化：上游基于 Node 字节流 + async；mini 用"行馈送 + 内存输出 + 回调式
pending"的同步近似（request 返回 PendingRequest，feed 响应帧时 settle）——
帧分类、错误码、id 配对语义完整保留。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from ..core.scope import Context
from ..core.agent_loop.agent import AgentLoop
from ..llm import FakeLlmAdapter
from ..llm.retry import apply_retry_planner
from ..compaction import install_compaction
from ..jobs import install_jobs, register_job_tools
from ..skills import install_skills, register_skill_tools
from ..core.system_prompt import install_system_prompt
from ..core.session import Session
from ..core.tools import ToolRegistry


class JsonRpcResponseError(Exception):
    """JSON-RPC 错误响应，保留 wire code 与可选 data。"""

    def __init__(self, code: int | None, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class PendingRequest:
    """一次 request 的等待槽：feed 响应帧时 settle（同步模型近似 async await）。"""

    def __init__(self, id_: str):
        self.id = id_
        self.result: Any = None
        self.error: JsonRpcResponseError | None = None
        self.settled = False


def _object_params(params: Any) -> dict:
    """JSON-RPC params 归一化：数组/标量折叠为 {}（上游 objectParams）。"""
    if isinstance(params, dict):
        return params
    return {}


class JsonRpcLineTransport:
    """newline-delimited JSON-RPC 2.0 端点（内存线）。

    feed(line) 投喂入站帧；out_lines 收集出站帧（请求/响应/通知统一换行帧）。
    request() 发送并返回 PendingRequest；feed 到对应响应帧时 settle。
    """

    def __init__(self):
        self.out_lines: list[str] = []
        self._request_handler: Callable | None = None
        self._notification_handler: Callable | None = None
        self._pending: dict[str, PendingRequest] = {}
        self._closed = False

    # ---------- 出站 ----------

    def request(self, method: str, params: Any = None) -> PendingRequest:
        id_ = "req_" + uuid.uuid4().hex
        pending = PendingRequest(id_)
        self._pending[id_] = pending
        frame: dict = {"jsonrpc": "2.0", "id": id_, "method": method}
        if params is not None:
            frame["params"] = params
        self._write(frame)
        return pending

    def notify(self, method: str, params: Any = None) -> None:
        frame: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        self._write(frame)

    def flush(self) -> None:
        """等待先前帧写入：同步模型下以空行 barrier 表达语义（上游写空串）。"""
        self._write(None)

    def _write(self, frame: dict | None) -> None:
        if self._closed:
            raise RuntimeError("JSON-RPC transport closed")
        if frame is None:
            self.out_lines.append("")
            return
        self.out_lines.append(json.dumps(frame, ensure_ascii=False))

    # ---------- 入站 ----------

    def feed(self, line: str) -> None:
        """投喂一行入站帧：请求→handler（错误→-32603）；响应→settle pending；通知→handler。"""
        if not line.strip():
            return
        try:
            message = json.loads(line)
        except (ValueError, TypeError):
            return   # 畸形 JSON 行忽略（上游同语义）
        if not isinstance(message, dict):
            return
        id_ = message.get("id")
        method = message.get("method")
        if (isinstance(id_, (str, int)) and isinstance(method, str)):
            self._handle_request(id_, method, _object_params(message.get("params")))
        elif isinstance(id_, (str, int)):
            self._handle_response(id_, message)
        elif isinstance(method, str):
            if self._notification_handler is not None:
                self._notification_handler(method, _object_params(message.get("params")))

    def _handle_request(self, id_: str | int, method: str, params: dict) -> None:
        if self._request_handler is None:
            self._write({"jsonrpc": "2.0", "id": id_,
                         "error": {"code": -32601, "message": f"method not found: {method}"}})
            return
        try:
            result = self._request_handler(method, params)
            self._write({"jsonrpc": "2.0", "id": id_, "result": result})
        except Exception as error:
            self._write({"jsonrpc": "2.0", "id": id_,
                         "error": {"code": -32603, "message": str(error)}})

    def _handle_response(self, id_: str | int, frame: dict) -> None:
        pending = self._pending.pop(str(id_), None)
        if pending is None:
            return
        error = frame.get("error")
        if isinstance(error, dict):
            pending.error = JsonRpcResponseError(
                error.get("code") if isinstance(error.get("code"), int) else None,
                error.get("message") if isinstance(error.get("message"), str) else "JSON-RPC error",
                error.get("data"),
            )
        else:
            pending.result = frame.get("result")
        pending.settled = True

    # ---------- 生命周期 ----------

    def close(self) -> None:
        """关闭：拒绝全部 pending，之后拒绝一切写入。"""
        self._closed = True
        for pending in self._pending.values():
            pending.error = JsonRpcResponseError(None, "JSON-RPC transport closed")
            pending.settled = True
        self._pending.clear()

    def on_request(self, handler: Callable) -> None:
        self._request_handler = handler

    def on_notification(self, handler: Callable) -> None:
        self._notification_handler = handler


class SdkRuntime:
    """最小运行服务：initialize / session/prompt / shutdown（内存会话 + 假模型）。

    对应上游 HarnessSdkJsonRpcServer 的三个请求方法；通知（session.event 等）
    在 mini 中由 loop 事件钩子承载（简化标注，见文档）。
    """

    WIRE_NAME = "deepseek-harness-sdk-runtime"

    def __init__(self, adapter: Any = None):
        self._sessions: dict[str, AgentLoop] = {}
        self._adapter = adapter or FakeLlmAdapter()
        self._message_counter = 0
        self.cwd = "."
        self.provider = "fake"
        self.model = "fake-model"

    def handle(self, method: str, params: dict) -> Any:
        if method == "initialize":
            self.cwd = params.get("cwd", ".")
            self.provider = params.get("provider", "fake")
            self.model = params.get("model", "fake-model")
            return {"serverInfo": {"name": self.WIRE_NAME, "version": "0.0.1"}}
        if method == "session/prompt":
            session_id = params.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("sessionId required")
            loop = self._sessions.get(session_id)
            if loop is None:
                ctx = Context(name=f"sdk:{session_id}")
                apply_retry_planner(ctx)
                install_compaction(ctx)
                install_jobs(ctx)
                install_skills(ctx)
                install_system_prompt(ctx)
                reg = ToolRegistry(Context(name="sdk"))
                register_job_tools(reg, ctx.inject("jobs"))
                register_skill_tools(reg, ctx.inject("skills"))
                loop = AgentLoop(Session(session_id), self._adapter, reg, ctx)
                self._sessions[session_id] = loop
            blocks = params.get("contentBlocks")
            text = "".join(b.get("text", "") for b in blocks or []
                           if b.get("type") == "text")
            self._message_counter += 1
            message_id = f"msg-{self._message_counter}"
            loop.run(text)
            return {"messageId": message_id}
        if method == "shutdown":
            return {}
        raise ValueError(f"method not found: {method}")

    @property
    def sessions(self) -> dict:
        return self._sessions