"""第 12 章：ACP 最小子集 —— 自动化专用 Agent Client Protocol 服务。

对应 dsh 真实源码：packages/acp/acp（apply + codec.ts）。

上游语义（已核实，index.ts + codec.ts）：
  * 自动化专用：只承载 prompt 文本、已提交的 assistant 文本、取消、一次性
    权限决策；呈现与人机交互留在 web 表面。
  * initialize → {protocolVersion, agentInfo:{name:'deepseek-harness-acp',
    version}, agentCapabilities:{promptCapabilities:{image:false,audio:false,
    embeddedContext:false}}, authMethods:[]}（本桥不宣称富媒体能力）。
  * newSession：cwd 必须绝对路径（否则 invalid params）、additionalDirectories
    非空拒绝、mcpServers 非空拒绝；mint sessionId。
  * prompt：session 必须存在；已有 inflight 拒绝（"a prompt is already in
    flight for this session"）；只支持 text 与 resource_link 内容（其余
    invalid params）；空 prompt 拒绝；等待 whole-agent idle 结算：
    turnless → 'cancelled'，max-tokens → 'end_turn'（README：非终局不是
    prompt 级 stop reason），其余 turn/end 映射 stopReason；turn/end
    kind='error' → 立即以 "turn failed" 拒绝。
  * 会话更新只发已提交的 assistant text（image 块渲染为
    "[image attachment <id>]"），chunk/reasoning/tools/plan 不上线。
  * cancel：未知 session no-op；否则取消 agent 并结算 'cancelled'。
  * 审批桥：仅当 callId 存在时提供二选一（allow-once → 'allowed-once'，
    reject-once → 'rejected'，cancelled → 'cancelled'）；callId 缺失 → next()。
  * 错误码：invalid params / internal error（JSON-RPC -32602 / -32603）。

载体简化：上游 async（whenIdle 等待 + stream 通知）；mini 同步——prompt
直接跑完整回合后返回 stopReason，stream 通知以记录列表承载。
"""
from __future__ import annotations

import os
import uuid
from typing import Any, Callable

from .bus import Context
from .llm import FakeLlmAdapter
from .session import Session, text_block, create_message
from .tools import ToolRegistry

STOP_REASON_MAP = {
    "completed": "end_turn",
    "max-tokens": "max_tokens",
    "aborted": "end_turn",      # cancelled 保留给显式 client 取消/销毁
    "interrupted": "cancelled",
    "blocked": "end_turn",
    "error": "end_turn",
}


def turn_end_to_stop_reason(reason: dict) -> str:
    """harness turn/end reason → ACP 终局词汇（codec.ts 同构映射）。"""
    return STOP_REASON_MAP.get(reason.get("kind"), "end_turn")


def acp_prompt_to_text(prompt: list | tuple) -> str:
    """ACP prompt 块 → 文本：text 逐字拼接，resource_link 渲染为显式引用。"""
    out: list[str] = []
    for block in prompt or []:
        if block.get("type") == "text":
            out.append(block.get("text", ""))
        elif block.get("type") == "resource_link":
            out.append(f"\n[resource_link name={block.get('name', '')!r} "
                       f"uri={block.get('uri', '')!r}]\n")
    return "".join(out)


def prompt_has_unsupported_content(prompt: list | tuple) -> bool:
    """是否携带 text/resource_link 之外的内容（image/audio/embedded 拒绝）。"""
    return any(b.get("type") not in ("text", "resource_link") for b in prompt or [])


class AcpRequestError(Exception):
    """ACP 请求错误：invalid params（-32602）或 internal error（-32603）。"""

    def __init__(self, code: int, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def invalid_params(detail: str) -> AcpRequestError:
    return AcpRequestError(-32602, detail)


def internal_error(detail: str) -> AcpRequestError:
    return AcpRequestError(-32603, detail)


class AcpServer:
    """自动化专用 ACP 服务（mini：内存会话 + 假模型 + 同步回合）。

    对齐上游 apply() 的方法集：initialize / authenticate / new_session /
    prompt / cancel；会话更新流以 updates 记录承载（简化标注）。
    """

    WIRE_NAME = "deepseek-harness-acp"

    def __init__(self, adapter: Any = None, provider: str = "fake",
                 model: str = "fake-model"):
        self._adapter = adapter or FakeLlmAdapter()
        self.provider = provider
        self.model = model
        self._sessions: dict[str, dict] = {}   # session_id -> {loop, cwd}
        self._closed = False
        self.updates: list[dict] = []          # 会话更新流（简化载体）
        self._answerer: Callable | None = None   # 审批决策注入（测试用）

    def set_answerer(self, fn: Callable | None) -> None:
        """注入审批决策函数：request → 'allow-once' | 'reject-once' | 'cancelled'。"""
        self._answerer = fn

    # ---------- 握手 ----------

    def initialize(self) -> dict:
        return {
            "protocolVersion": 1,
            "agentInfo": {"name": self.WIRE_NAME, "version": "0.0.1"},
            "agentCapabilities": {
                "promptCapabilities": {"image": False, "audio": False,
                                       "embeddedContext": False},
            },
            "authMethods": [],
        }

    def authenticate(self, params: dict | None = None) -> None:
        return None   # 无认证（authMethods 空）

    # ---------- 会话 ----------

    def new_session(self, cwd: str, additional_directories: list | None = None,
                    mcp_servers: list | None = None) -> dict:
        self._assert_open()
        if not os.path.isabs(cwd):
            raise invalid_params(f"cwd must be an absolute path: {cwd}")
        if additional_directories:
            raise invalid_params("additionalDirectories is not supported")
        if mcp_servers:
            raise invalid_params("mcpServers is not supported")
        session_id = str(uuid.uuid4())
        ctx = Context(name=f"acp:{session_id}")
        self._sessions[session_id] = {
            "loop": self._make_loop(session_id, ctx),
            "cwd": cwd,
            "ctx": ctx,
        }
        return {"sessionId": session_id}

    def _make_loop(self, session_id: str, ctx: Context):
        from .loop import AgentLoop
        return AgentLoop(Session(session_id), self._adapter,
                         ToolRegistry(ctx), ctx)

    # ---------- prompt ----------

    def prompt(self, session_id: str, prompt: list | tuple) -> dict:
        self._assert_open()
        record = self._sessions.get(session_id)
        if record is None:
            raise invalid_params(f"unknown session: {session_id}")
        if record.get("inflight"):
            raise invalid_params("a prompt is already in flight for this session")
        if prompt_has_unsupported_content(prompt):
            raise invalid_params("only text and resource_link prompt content is supported")
        text = acp_prompt_to_text(prompt)
        if text.strip() == "":
            raise invalid_params("empty prompt")
        record["inflight"] = True
        try:
            record["loop"].run(text)
        finally:
            record["inflight"] = False
        reason = self._last_turn_end(record["loop"])
        if reason is None or reason.get("kind") == "error":
            msg = (reason.get("error", {}).get("message", "unknown")
                   if reason else "no turn ended")
            raise internal_error(f"turn failed: {msg}")
        return {"stopReason": turn_end_to_stop_reason(reason)}

    def _last_turn_end(self, loop) -> dict | None:
        for event in reversed(loop.session.events):
            if event["type"] == "turn/end":
                return event["data"].get("reason")
        return None

    # ---------- cancel ----------

    def cancel(self, session_id: str) -> None:
        record = self._sessions.get(session_id)
        if record is None:
            return   # 未知 session no-op（上游同语义）
        record["loop"].cancel()

    # ---------- 审批桥 ----------

    def bridge_approval(self, request: dict, next_fn: Callable) -> str | None:
        """approval/request 监听器：仅 callId 存在时提供二选一决策。

        callId 缺失 → 调 next_fn 委派（上游 return next()）。
        """
        if request.get("callId") is None:
            return next_fn()
        decision = self._answer(request)
        if decision == "cancelled":
            return "cancelled"
        return "allowed-once" if decision == "allow-once" else "rejected"

    def _answer(self, request: dict) -> str:
        return self._answerer(request) if self._answerer else "allow-once"

    # ---------- 生命周期 ----------

    def close(self) -> None:
        self._closed = True
        self._sessions.clear()

    def _assert_open(self) -> None:
        if self._closed:
            raise internal_error("the ACP bridge has been disposed")

    @property
    def sessions(self) -> dict:
        return self._sessions