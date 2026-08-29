"""web 会话服务：session 域 unary + 流订阅工厂（对齐 `packages/api/session-controller`）。

方法集（alpha.1 已核实）：`session.list` / `session.search` / `session.create` /
`session.selectModel` / `session.modelCatalog` / `session.canOpenWorkspacePath` /
`session.openWorkspacePath` / `session.rename` / `session.fork` / `session.prompt` /
`session.attachment` / `session.updateQueue` / `session.cancel` / `session.page`；
流方法 `session.follow` / `session.control` 返回缓冲订阅对象（api 层同步可测，
事件循环泵由载体层 web/mux.py 驱动）。`host.describe` / `session.history` /
`session.models` 已从新契约消失，连同 apiproxy 阶段的命令路由一起移除。

契约已逐条对照上游源码核实（status/upstream/baseline.md D18-21）；错误分支的
消息文案与 details 形状来自 session-controller/src/{commands,agent,history,
control,list,catalog}.ts 的字面量。方法签名：`handler(payload) -> value`，
业务错误一律抛 `_Reject`（dispatch 折进 RpcResult 错误分支），方法本身不抛
业务异常（对齐上游 transport 层语义）。

mini 教学简化（须同步 verified-diffs §3.4 / AGENTS.md）：
  * 单适配器部署：modelCatalog / selectModel 直接导出适配器路由；selectModel
    的“记录”仅 advisory（回落恒单模型），不驱动真实路由。
  * 无 projection/ChunkRow 注册表：follow/control 的 projections 块空
    values{}（上游 “a deployment without the registry…” 同语义）；control 的
    queue 帧直接发 inbox 当前态（省略 asOfSeq 投影簿记，订阅方观察语义一致）。
  * follow/page 的 subagent 地址分支全部拒绝（mini 不驱动子代理会话，防御对齐）。
  * search 在 live 会话 surface 快照上扫描，不落全文索引。
  * 精确 details 附加字段按 mini 教学面导出（agent-preset-conflict /
    session-conflict 的消息文案对齐上游字面量，但携带 mini 的 requested* 路径字段）。
"""
from __future__ import annotations

import base64
import os
import uuid
from collections.abc import Mapping
from typing import Any

from ..attachment.admission import admit_encoded_images
from ..attachment.error import AttachmentError
from ..attachment.types import (
    AttachmentId,
    Dimensions,
    EncodedImageAttachment,
    ImageAttachmentRef,
)
from ..core.agent_loop.agent import AgentLoop
from ..core.scope import Context
from ..core.session import Session, thaw
from ..core.session.chunk_rows import pack_chunk_runs
from ..core.session.message import create_message, image_block, text_block
from ..core.session.surface import derive_event_message
from ..core.session_store import SessionStore
from ..core.tools import ToolRegistry
from ..llm import LlmAdapter
from .envelope import rpc_error, rpc_result_ok

__all__ = [
    "WebApi",
    "canonical_client_time_zone",
    "DEFAULT_MAX_MESSAGES",
    "MESSAGE_TYPES",
    "SESSION_SEARCH_RESULT_LIMIT",
    "SESSION_SEARCH_SNIPPET_LENGTH",
]

#: 尾页缺省窗口（session-controller history.ts DEFAULT_MAX_MESSAGES）
DEFAULT_MAX_MESSAGES = 50

#: 分页按 append 消息边界对齐：只数这两种 append 消息（history.ts）
MESSAGE_TYPES = frozenset({"user/message", "assistant/message"})

#: search 最多命中数 + 摘要长度（catalog.ts SESSION_SEARCH_* 常量）
SESSION_SEARCH_RESULT_LIMIT = 20
SESSION_SEARCH_SNIPPET_LENGTH = 240


def canonical_client_time_zone(value: str) -> str | None:
    """规范化浏览器 IANA 时区（对齐上游 canonicalClientTimeZone）。

    返回规范化字符串，非法（或本机时区库无法解析）返回 None。UTC 恒可用；
    其余要求 zoneinfo 能解析。教学简化：回显输入原样，不做别名折叠。
    """
    if not isinstance(value, str) or not value:
        return None
    if value == "UTC":
        return value
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(value)
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError / tzdata 缺失
        return None
    return value


def _utf16_len(value: str) -> int:
    """JS String.prototype.length 等价：按 UTF-16 码元计数。"""
    return len(value.encode("utf-16-le")) // 2


def _truncate_code_points(value: str, maximum: int) -> str:
    """按码点截断到 maximum（上游 truncateUnicodeCodePoints）。"""
    out = []
    for char in value:
        if len(out) == maximum:
            break
        out.append(char)
    return "".join(out)


class _Reject(Exception):
    """业务错误：dispatch 捕获后折进 RpcResult 错误分支。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        self.code = code
        self.message = message
        self.details = details or {}


def _require_id(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise _Reject("bad-request", f"{key} must be a non-empty string", {})
    return value


class _Subscription:
    """流订阅基类：帧缓冲 + 完成/失败 + 泵唤醒（同步、无 asyncio 依赖）。

    泵协议（web/mux.py 消费）：载体层先 `set_waiter(cb)` 挂唤醒，再 `pull()`
    逐帧消费直到返回 None；`done`/`error` 表达终态（帧先于终态消费完）。
    """

    def __init__(self):
        self._frames: list[dict] = []
        self._waiter = None
        self.done = False
        self.error: dict | None = None

    def pull(self) -> dict | None:
        if self._frames:
            return self._frames.pop(0)
        return None

    def set_waiter(self, callback: Any) -> None:
        self._waiter = callback

    def _push(self, frame: dict) -> None:
        if self.done:
            return
        self._frames.append(frame)
        self._wake()

    def _fail(self, code: str, message: str, details: dict | None = None) -> None:
        if self.done:
            return
        self.done = True
        self.error = {"code": code, "message": message, "details": details or {}}
        self._wake()

    def _end(self) -> None:
        if self.done:
            return
        self.done = True
        self._wake()

    def _wake(self) -> None:
        waiter = self._waiter
        self._waiter = None
        if waiter is not None:
            waiter()


class _FollowSubscription(_Subscription):
    """session.follow 流订阅（session-controller history.ts follow 同款）。

    构造即校验 + 快照；失败以 `error` 状态暴露（mux 转流 error 帧），不抛异常。
    切换跟随 session/event 总线，gap 检测 fail loud。
    """

    def __init__(self, api: "WebApi", request: dict):
        super().__init__()
        self.api = api
        self._dispose = None
        try:
            self._start(request)
        except _Reject as error:
            self._fail(error.code, error.message, error.details)

    def _start(self, request: dict) -> None:
        api = self.api
        max_messages = request.get("maxMessages")
        if max_messages is not None and (not isinstance(max_messages, int)
                                         or isinstance(max_messages, bool) or max_messages <= 0):
            raise _Reject("bad-request", "maxMessages must be a positive safe integer", {})
        session_id = api._address_target(request)
        session = self._session_or_reject(session_id)
        cursor = session.seq - 1
        page, has_more = api._paginate(list(session.events), None,
                                       max_messages or DEFAULT_MAX_MESSAGES)
        self._push({"type": "snapshot", "header": api._wire_header(session),
                    "cursor": cursor, "records": api._page_records(page),
                    "hasMore": has_more,
                    "projections": {"asOfSeq": cursor, "values": {}}})
        state: dict[str, Any] = {"next_seq": cursor + 1, "pending": []}

        def on_event(payload: dict) -> None:
            if self.done:
                return
            current = payload.get("session")
            event = payload.get("event")
            if getattr(current, "session_id", None) != session_id or not isinstance(event, Mapping):
                return
            state["pending"].append(event)
            self._drain(state)

        self._dispose = api.ctx.on("session/event", on_event, global_=True)

    def _drain(self, state: dict) -> None:
        pending = state["pending"]
        while pending and not self.done:
            event = pending.pop(0)
            if event["seq"] < state["next_seq"]:
                continue
            if event["seq"] != state["next_seq"]:
                self._fail("internal",
                           f"session event stream skipped seq {state['next_seq']}", {})
                return
            state["next_seq"] += 1
            self._push({"type": "event", "event": thaw(event)})

    def _session_or_reject(self, session_id: str) -> Session:
        session = self.api.store.get(session_id)
        if session is None:
            raise _Reject("session-not-found", f'session "{session_id}" not found',
                          {"sessionId": session_id})
        return session

    def close(self) -> None:
        if self._dispose is not None:
            self._dispose()
            self._dispose = None
        self._end()


class _ControlSubscription(_Subscription):
    """session.control 流订阅（control.ts 同款）：队列/作业/投影基线 + 增量。

    mini 无 ctx.jobs 服务 → jobs 恒空（如实）；无投影注册表 → values 恒 {}
    （“deployment without the registry” 语义）。基线队列只列挂接 agent 的会话。
    """

    def __init__(self, api: "WebApi"):
        super().__init__()
        self.api = api
        self._dispose = None
        self._start()

    def _start(self) -> None:
        api = self.api
        baseline: dict[str, Any] = {"queues": {}, "jobs": {}, "projections": {}}
        for session in api.store.list():
            agent = api._agents.get(session.session_id)
            items = api._queue_items(agent) if agent is not None and agent.session is session else []
            baseline["queues"][session.session_id] = items
            baseline["jobs"][session.session_id] = []
            baseline["projections"][session.session_id] = {
                "asOfSeq": session.seq - 1, "values": {}}
        self._push({"type": "baseline", "value": baseline})

        def on_event(payload: dict) -> None:
            if self.done:
                return
            current = payload.get("session")
            event = payload.get("event")
            if not isinstance(event, Mapping) or event.get("type") != "agent/inbox/spliced":
                return
            session_id = getattr(current, "session_id", None)
            if session_id is None:
                return
            agent = api._agents.get(session_id)
            if agent is None or agent.session is not current:
                return
            self._push({"type": "queue", "sessionId": session_id,
                        "items": api._queue_items(agent, event.get("data") or {})})

        self._dispose = api.ctx.on("session/event", on_event, global_=True)

    def close(self) -> None:
        if self._dispose is not None:
            self._dispose()
            self._dispose = None
        self._end()


class WebApi:
    """web 会话服务核心：session 域 unary 方法 + 流订阅工厂。

    装配：ctx（根上下文）、adapter、tools。`ctx.sessions` 存在则复用，否则自建
    SessionStore（挂在根 ctx 上）。每个会话在 create 时经 loop.publish() 挂一个
    常驻 AgentLoop（driver 模式），店成员资格归 loop 所有。`ctx.attachments`
    存在则启用真实图片受理（attachment/ 服务族），否则 image 提示以
    ATTACHMENT_UNAVAILABLE 拒绝（如实标注部署能力）。
    """

    def __init__(self, ctx: Context, adapter: LlmAdapter, tools: ToolRegistry | None = None,
                 cwd: str | None = None):
        self.ctx = ctx
        self.adapter = adapter
        self.tools = tools if tools is not None else ToolRegistry(ctx)
        self.cwd = cwd or os.getcwd()
        self.store = ctx.get("sessions")
        if self.store is None:
            self.store = SessionStore(ctx)
        self.attachments = ctx.get("attachments")
        self._agents: dict[str, AgentLoop] = {}
        # selectModel 记录（advisory：单适配器部署回落恒单模型，不驱动路由）
        self._selections: dict[str, dict] = {}
        # 审批桥：tools/ask 问询 → mux 帧 → POST /api/respond（stage ④ 换 $events 退役）
        from .approvals import ApprovalBridge
        self.approvals = ApprovalBridge(self)

    # ---------- 路由 ----------

    ROUTES: dict[str, str] = {
        "session.list": "list_sessions",
        "session.search": "search",
        "session.create": "create_session",
        "session.selectModel": "select_model",
        "session.modelCatalog": "model_catalog",
        "session.canOpenWorkspacePath": "can_open_workspace_path",
        "session.openWorkspacePath": "open_workspace_path",
        "session.rename": "rename",
        "session.fork": "fork",
        "session.prompt": "prompt",
        "session.attachment": "attachment",
        "session.updateQueue": "update_queue",
        "session.cancel": "cancel",
        "session.page": "page",
    }

    def methods(self) -> frozenset[str]:
        return frozenset(self.ROUTES)

    def dispatch(self, method: str, rpc_id_: str, payload: Any) -> dict | None:
        """按路由表派发；未知方法返回 None（载体层映射 404）。

        返回完整 server-response 帧。payload 非 dict 按 bad-request 拒绝。
        """
        handler = self.ROUTES.get(method)
        if handler is None:
            return None
        if not isinstance(payload, dict):
            return self._err(rpc_id_, "bad-request", "payload must be a JSON object", {})
        try:
            value = getattr(self, handler)(payload)
        except _Reject as error:
            return self._err(rpc_id_, error.code, error.message, error.details)
        return self._ok(rpc_id_, value)

    # ---------- 装配 ----------

    def _attach(self, session: Session) -> AgentLoop:
        loop = AgentLoop(session, self.adapter, self.tools, self.ctx)
        loop.publish()
        self._agents[session.session_id] = loop
        self.approvals.install(loop)
        try:
            loop.start_driver()
        except RuntimeError:
            # 事件循环未运行：driver 首次 prompt 时惰性启动（离线装配兜底）
            pass
        return loop

    def _agent_for(self, session_id: str) -> tuple[Session, AgentLoop] | None:
        """resolveAgent 同款：冷会话自动 resume（attach）。未知 → None。

        子代理所属会话 → agent-busy（上游 apiSessionSubagentOwnershipError）。
        """
        session = self.store.get(session_id)
        if session is None:
            return None
        if self._subagent_owned(session):
            raise _Reject("agent-busy",
                          f'session "{session_id}" is owned by subagent routing',
                          {"reason": "use subagent delivery for this child session"})
        loop = self._agents.get(session_id)
        if loop is None:
            loop = self._attach(session)
        return session, loop

    @staticmethod
    def _subagent_owned(session: Session) -> bool:
        return session.meta.get("origin") == "subagent"

    def _selection(self) -> dict:
        info = self.adapter.resolve_model_info()
        return {"provider": info.get("provider") or "unknown",
                "model": info.get("model") or "unknown"}

    # ---------- 响应构造 ----------

    def _ok(self, rpc_id_: str, value: Any) -> dict:
        return {"type": "server-response", "rpcId": rpc_id_,
                "result": rpc_result_ok(value)}

    def _err(self, rpc_id_: str, code: str, message: str, details: dict | None = None) -> dict:
        return {"type": "server-response", "rpcId": rpc_id_,
                "result": {"ok": False, "error": rpc_error(code, message, details)}}

    # ---------- 地址与守卫（session-address.ts + agent.ts guardSession） ----------

    def _address_target(self, request: dict) -> str:
        """校验 SessionAddress 并按 mini 域能力返回目标 sessionId；守卫照上游拒绝。"""
        address = request.get("address")
        if not isinstance(address, dict) or address.get("kind") not in ("session", "subagent"):
            raise _Reject("bad-request", "address must be a session or subagent address", {})
        if address["kind"] == "session":
            target = _require_id(address, "sessionId")
            session = self.store.get(target)
            if session is None:
                raise _Reject("session-not-found", f'session "{target}" not found',
                              {"sessionId": target})
            # header.cwd === undefined → session-not-found；子代理会话经 session
            # 地址访问 → agent-busy（防御对齐：mini 不生产此类场景）
            if session.meta.get("cwd") is None:
                raise _Reject("session-not-found", f'session "{target}" not found',
                              {"sessionId": target})
            if self._subagent_owned(session):
                raise _Reject("agent-busy",
                              f'session "{target}" is owned by subagent routing',
                              {"reason": "use subagent delivery for this child session"})
            return target
        parent_session_id = address.get("parentSessionId")
        child_session_id = address.get("childSessionId")
        if not (isinstance(parent_session_id, str) and parent_session_id
                and isinstance(child_session_id, str) and child_session_id):
            raise _Reject("bad-request",
                          "subagent address must carry parentSessionId and childSessionId", {})
        session = self.store.get(child_session_id)
        if session is None:
            raise _Reject("session-not-found", f'session "{child_session_id}" not found',
                          {"sessionId": child_session_id})
        if self._subagent_owned(session) and session.meta.get("parentSession") == parent_session_id:
            raise _Reject("subagent-catalog-diagnostic", "subagent descriptor is unavailable",
                          {"parentSessionId": parent_session_id,
                           "childSessionId": child_session_id, "reason": "unsupported"})
        raise _Reject("subagent-unauthorized", "subagent does not belong to the supplied parent",
                      {"childSessionId": child_session_id})

    # ---------- session.list ----------

    @staticmethod
    def _list_metadata(session: Session) -> dict:
        """折叠 list 提示投影：blank + lastPromptAt（上游 fold 同款）。"""
        blank = True
        last_prompt_at = None
        for event in session.events:
            if event.get("type") == "turn/start":
                blank = False
            if event.get("type") == "user/message":
                source = (event.get("data") or {}).get("source") or {}
                if source.get("kind") == "user":
                    last_prompt_at = event.get("time")
        return {"blank": blank, "lastPromptAt": last_prompt_at}

    def _summary(self, session: Session, running: bool) -> dict:
        metadata = self._list_metadata(session)
        meta = session.meta
        summary: dict[str, Any] = {
            "sessionId": session.session_id,
            "updatedAt": max(session.created_at, metadata["lastPromptAt"] or 0),
            "running": running,
            "blank": metadata["blank"],
        }
        if meta.get("parentSession") is not None:
            summary["parentSessionId"] = meta["parentSession"]
        if meta.get("origin") is not None:
            summary["origin"] = meta["origin"]
        if meta.get("cwd") is not None:
            summary["cwd"] = meta["cwd"]
        return summary

    def list_sessions(self, payload: dict) -> dict:
        items = []
        for session in self.store.list():
            loop = self._agents.get(session.session_id)
            running = loop.status == "running" if loop is not None else False
            items.append(self._summary(session, running))
        items.sort(key=lambda item: item["updatedAt"], reverse=True)
        return {"items": items}

    # ---------- session.search ----------

    def search(self, payload: dict) -> dict:
        query = payload.get("query")
        if not isinstance(query, str):
            raise _Reject("bad-request", "session search query must be a string", {})
        query = query.strip()
        if not query:
            raise _Reject("bad-request", "session search query must not be empty", {})
        if _utf16_len(query) > 500:
            raise _Reject("bad-request",
                          "session search query must contain at most 500 UTF-16 code units", {})
        if "\0" in query:
            raise _Reject("bad-request", "session search query must not contain NUL", {})
        items = []
        needle = query.lower()
        for session in self.store.list():
            snippet = self._search_snippet(session, needle)
            if snippet is not None:
                items.append({"sessionId": session.session_id,
                              "snippet": _truncate_code_points(snippet, SESSION_SEARCH_SNIPPET_LENGTH)})
        return {"items": items, "hasMore": False}

    def _search_snippet(self, session: Session, needle: str) -> str | None:
        """任一当前 surface 消息文本包含 needle → 返回整条文本（大小写不敏感）。"""
        for node in session.surface_nodes():
            message = derive_event_message(node)
            if message is None:
                continue
            texts = [block.get("text") for block in message.get("content") or []
                     if isinstance(block, Mapping) and block.get("type") == "text"
                     and isinstance(block.get("text"), str)]
            text = "".join(texts)
            if needle in text.lower():
                return text
        return None

    # ---------- session.create ----------

    def create_session(self, payload: dict) -> dict:
        workspace_id = payload.get("workspaceId")
        cwd = payload.get("cwd")
        if workspace_id is not None and cwd is not None:
            raise _Reject("bad-request", "session.create accepts workspaceId or cwd, not both", {})
        session_id = payload.get("sessionId")
        if session_id is not None and not (isinstance(session_id, str) and session_id):
            raise _Reject("bad-request", "sessionId must be a non-empty string", {})
        agent_preset = payload.get("agentPreset")
        if agent_preset is not None and not isinstance(agent_preset, str):
            raise _Reject("bad-request", "agentPreset must be a string", {})
        if cwd is not None and not (isinstance(cwd, str) and os.path.isabs(cwd)):
            raise _Reject("bad-request", "cwd must be an absolute path", {})
        if workspace_id is not None:
            raise _Reject("workspace-not-found", f'workspace "{workspace_id}" not found',
                          {"workspaceId": workspace_id})
        cwd = cwd if cwd is not None else self.cwd

        if session_id is not None and self.store.get(session_id) is not None:
            existing = self.store.get(session_id)
            if agent_preset is not None and existing.meta.get("agentPreset") != agent_preset:
                raise _Reject("agent-preset-conflict",
                              f'session "{session_id}" is bound to agent preset '
                              f'"{existing.meta.get("agentPreset")}", not "{agent_preset}"',
                              {"sessionId": session_id, "requestedPreset": agent_preset,
                               "existingPreset": existing.meta.get("agentPreset")})
            existing_cwd = existing.meta.get("cwd")
            if existing_cwd is None:
                raise _Reject("session-conflict",
                              f'session "{session_id}" records no cwd and cannot be adopted '
                              f'for "{cwd}"',
                              {"sessionId": session_id, "requestedCwd": cwd,
                               "existingCwd": existing_cwd})
            if existing_cwd != cwd:
                raise _Reject("session-conflict",
                              f'session "{session_id}" belongs to "{existing_cwd}", not "{cwd}"',
                              {"sessionId": session_id, "requestedCwd": cwd,
                               "existingCwd": existing_cwd})
            return self._create_result(session_id, self._preset_of(existing))
        if session_id is None:
            session_id = f"session-{uuid.uuid4()}"
        try:
            os.makedirs(cwd, exist_ok=True)
        except OSError as error:
            raise _Reject("internal",
                          f'failed to ensure project directory "{cwd}": {error}', {}) from error
        meta: dict[str, Any] = {"cwd": cwd}
        if agent_preset is not None:
            meta["agentPreset"] = agent_preset
        session = self.store.prepare(session_id, {"meta": meta})
        self._attach(session)
        return self._create_result(session.session_id, self._preset_of(session))

    @staticmethod
    def _create_result(session_id: str, agent_preset: str | None) -> dict:
        result: dict[str, Any] = {"sessionId": session_id}
        if agent_preset is not None:
            result["agentPreset"] = agent_preset
        return result

    @staticmethod
    def _preset_of(session: Session) -> str | None:
        return session.meta.get("agentPreset")

    # ---------- session.selectModel / modelCatalog ----------

    def select_model(self, payload: dict) -> dict:
        session_id = _require_id(payload, "sessionId")
        provider = payload.get("provider")
        model = payload.get("model")
        if not (isinstance(provider, str) and isinstance(model, str)):
            raise _Reject("bad-request", "provider and model must be strings", {})
        reasoning_effort = payload.get("reasoningEffort")
        if reasoning_effort is not None and not isinstance(reasoning_effort, str):
            raise _Reject("bad-request", "reasoningEffort must be a string", {})
        found = self._agent_for(session_id)
        if found is None:
            raise _Reject("session-not-found", f'session "{session_id}" not found',
                          {"sessionId": session_id})
        session, _loop = found
        selection = self._selection()
        if selection["provider"] != provider or selection["model"] != model:
            raise _Reject("model-unavailable",
                          f'no adapter serves provider "{provider}" model "{model}"',
                          {"provider": provider, "model": model})
        resolved: dict[str, Any] = {"provider": provider, "model": model}
        if reasoning_effort is not None:
            resolved["reasoningEffort"] = reasoning_effort
        self._selections[session.session_id] = resolved
        return {"selected": resolved}

    def model_catalog(self, payload: dict) -> dict:
        selection = self._selection()
        provider = selection["provider"]
        model = selection["model"]
        groups: list[dict] = []
        if model:
            groups.append({"id": provider, "name": provider,
                           "models": [{"id": model, "name": model}]})
        return {"default": selection, "routableProviders": [provider],
                "groups": groups, "failures": []}

    # ---------- workspace 路径 ----------

    def can_open_workspace_path(self, payload: dict) -> bool:
        return False

    def open_workspace_path(self, payload: dict) -> None:
        path = payload.get("path")
        if not isinstance(path, str) or not path:
            raise _Reject("bad-request", "session.openWorkspacePath requires a non-empty path", {})
        raise _Reject("internal",
                      "path open failed: no native desktop opener is available in this deployment",
                      {"path": path})

    # ---------- session.rename ----------

    def rename(self, payload: dict) -> None:
        session_id = _require_id(payload, "sessionId")
        title = payload.get("title")
        if title is not None and not isinstance(title, str):
            raise _Reject("bad-request", "title must be a string", {})
        if self._agent_for(session_id) is None:
            raise _Reject("session-not-found", f'session "{session_id}" not found',
                          {"sessionId": session_id})
        raise _Reject("internal",
                      "renaming is unavailable: this deployment mounts no session-title service",
                      {})

    # ---------- session.prompt ----------

    def prompt(self, payload: dict) -> dict:
        request_id = _require_id(payload, "requestId")
        session_id = _require_id(payload, "sessionId")
        mode = payload.get("mode")
        if mode not in ("queue", "steer"):
            raise _Reject("bad-request", "mode must be one of 'queue', 'steer'", {})
        content = payload.get("content")
        if not isinstance(content, list) or not content:
            raise _Reject("bad-request",
                          "content must be a non-empty array of PromptContentPart", {})
        for part in content:
            if not isinstance(part, dict) or part.get("type") not in ("text", "image"):
                raise _Reject("bad-request", "each content part must be a text or image object", {})
            if part["type"] == "text" and not isinstance(part.get("text"), str):
                raise _Reject("bad-request", "text part must carry a string text", {})
            if part["type"] == "image" and not (isinstance(part.get("mediaType"), str)
                                                and isinstance(part.get("data"), str)):
                raise _Reject("bad-request", "image part must carry mediaType and data", {})

        client_time_zone = payload.get("clientTimeZone")
        canonical_time_zone = None
        if client_time_zone is not None:
            canonical_time_zone = canonical_client_time_zone(client_time_zone)
            if canonical_time_zone is None:
                raise _Reject("invalid-time-zone",
                              "clientTimeZone must be UTC or a valid IANA Area/Location name",
                              {"value": client_time_zone})

        found = self._agent_for(session_id)
        if found is None:
            raise _Reject("session-not-found", f'session "{session_id}" not found',
                          {"sessionId": session_id})
        loop = found[1]

        refs: list[ImageAttachmentRef] | None = None
        try:
            if any(part["type"] == "image" for part in content):
                refs = self._admit_prompt_images(content)
            if refs is None:
                blocks = [text_block(part["text"]) for part in content]
            else:
                blocks = self._content_with_blocks(content, refs)
            source: dict[str, Any] = {"kind": "user", "rpcId": request_id}
            if canonical_time_zone is not None:
                source["clientTimeZone"] = canonical_time_zone
            message = create_message("user", blocks, source)
            if mode == "steer":
                loop.steer(message)
            else:
                loop.followup(message)
        except AttachmentError as error:
            raise _Reject("attachment-error", error.message,
                          {"reason": error.code}) from error
        except _Reject:
            raise
        except Exception as error:  # noqa: BLE001 - 其余 admit/落盘失败折叠 agent-busy
            raise _Reject("agent-busy", "prompt rejected",
                          {"reason": str(error)}) from error
        return {"accepted": True}

    def _admit_prompt_images(self, content: list) -> list[ImageAttachmentRef]:
        info = self.adapter.resolve_model_info()
        modalities = info.get("input_modalities") or ["text"]
        if "image" not in modalities:
            raise _Reject("attachment-error",
                          f'Model "{info.get("model") or "unknown"}" does not support image input.',
                          {"reason": "MODEL_DOES_NOT_SUPPORT_IMAGES"})
        if self.attachments is None:
            raise _Reject("attachment-error",
                          "image prompt admission is unavailable: this deployment mounts "
                          "no attachment service",
                          {"reason": "ATTACHMENT_UNAVAILABLE"})
        images = [EncodedImageAttachment(mediaType=part["mediaType"], data=part["data"],
                                         **({} if part.get("name") is None
                                            else {"name": part["name"]}))
                  for part in content if part["type"] == "image"]
        return admit_encoded_images(self.attachments, images)

    @staticmethod
    def _content_with_blocks(content: list, refs: list[ImageAttachmentRef]) -> list:
        blocks = []
        index = 0
        for part in content:
            if part["type"] == "image":
                blocks.append(image_block(refs[index].to_dict()))
                index += 1
            else:
                blocks.append(text_block(part["text"]))
        return blocks

    # ---------- session.updateQueue ----------

    def update_queue(self, payload: dict) -> dict:
        session_id = _require_id(payload, "sessionId")
        item_id = _require_id(payload, "itemId")
        action = payload.get("action")
        if not isinstance(action, dict) or action.get("kind") not in ("edit", "remove", "steer"):
            raise _Reject("bad-request", "action must be a queue edit, remove, or steer action", {})
        if action["kind"] == "edit":
            content = action.get("content")
            if not isinstance(content, list):
                raise _Reject("bad-request", "edit action must carry a content array", {})
            if any(not (isinstance(part, dict) and part.get("type") == "text") for part in content):
                raise _Reject("attachment-error", "queue edits accept text content only",
                              {"reason": "QUEUE_EDIT_NON_TEXT"})

        agent = self._agents.get(session_id)
        if agent is None or self._subagent_owned(agent.session):
            raise _Reject("queue-item-not-found", "queued item is no longer pending",
                          {"itemId": item_id})

        target = self._queue_target(agent, item_id)
        if target is None:
            raise _Reject("queue-item-not-found", "queued item is no longer pending",
                          {"itemId": item_id})

        kind = action["kind"]
        if kind == "steer" and (target != "next_turn" or agent.status != "running"):
            raise _Reject("steer-unavailable", "current turn no longer accepts steering",
                          {"itemId": item_id})

        message = next(m for m in getattr(agent.inbox, target) if m["id"] == item_id)
        if kind == "edit":
            agent.inbox.replace(item_id, {**message,
                                          "content": [text_block(part["text"])
                                                      for part in action["content"]]})
        elif kind == "remove":
            agent.inbox.remove(item_id)
        else:
            agent.inbox.remove(item_id)
            agent.steer(message)
        return {"accepted": True}

    def _queue_target(self, agent: AgentLoop, item_id: str) -> str | None:
        for target in ("next_turn", "next_step"):
            if any(m["id"] == item_id for m in getattr(agent.inbox, target)):
                return target
        return None

    # ---------- session.cancel ----------

    def cancel(self, payload: dict) -> dict:
        session_id = _require_id(payload, "sessionId")
        agent = self._agents.get(session_id)
        if agent is None:
            raise _Reject("session-not-found", f'session "{session_id}" not found (not attached)',
                          {"sessionId": session_id})
        if self._subagent_owned(agent.session):
            raise _Reject("agent-busy",
                          f'session "{session_id}" is owned by subagent routing',
                          {"reason": "use subagent delivery for this child session"})
        agent.cancel("user", keep_inbox=True)
        return {"accepted": True}

    # ---------- session.attachment ----------

    def attachment(self, payload: dict) -> dict:
        session_id = _require_id(payload, "sessionId")
        attachment_id = _require_id(payload, "attachmentId")
        session = self.store.get(session_id)
        if session is None:
            raise _Reject("session-not-found", f'session "{session_id}" not found',
                          {"sessionId": session_id})
        ref = self._referenced_image(session.events, attachment_id)
        if ref is None:
            raise _Reject("attachment-error", "Image is not referenced by this session.",
                          {"reason": "ATTACHMENT_NOT_REFERENCED"})
        if self.attachments is None:
            raise _Reject("attachment-error",
                          "attachment service is unavailable in this deployment",
                          {"reason": "ATTACHMENT_UNAVAILABLE"})
        try:
            stored = self.attachments.read_image(ref)
        except AttachmentError as error:
            raise _Reject("attachment-error", error.message,
                          {"reason": error.code}) from error
        except Exception as error:  # noqa: BLE001 - 载体层折叠
            raise _Reject("internal", "Unable to read image attachment.", {}) from error
        return {"attachment": stored.ref.to_dict(),
                "data": base64.b64encode(stored.data).decode("ascii")}

    def _referenced_image(self, events, attachment_id: str) -> ImageAttachmentRef | None:
        for event in events:
            found = self._image_in_event(event, attachment_id)
            if found is not None:
                return found
        return None

    def _image_in_event(self, event: dict, attachment_id: str) -> ImageAttachmentRef | None:
        data = event.get("data") or {}
        message = derive_event_message(event)
        found = self._image_in_blocks(
            message.get("content") if isinstance(message, Mapping) else None, attachment_id)
        if found is not None:
            return found
        for inserted in (data.get("inserted") or []):
            if isinstance(inserted, Mapping):
                found = self._image_in_blocks(inserted.get("content"), attachment_id)
                if found is not None:
                    return found
        chunk = data.get("chunk") or {}
        if event.get("type") == "assistant/chunk" and chunk.get("type") == "block-end":
            block = chunk.get("block")
            if isinstance(block, Mapping):
                return self._image_in_block(block, attachment_id)
        return None

    def _image_in_blocks(self, content: Any, attachment_id: str) -> ImageAttachmentRef | None:
        if not isinstance(content, (list, tuple)):
            return None
        for block in content:
            found = self._image_in_block(block, attachment_id)
            if found is not None:
                return found
        return None

    def _image_in_block(self, block: Any, attachment_id: str) -> ImageAttachmentRef | None:
        if not isinstance(block, Mapping):
            return None
        if block.get("type") == "image":
            attachment = block.get("attachment")
            if isinstance(attachment, Mapping) and str(attachment.get("attachmentId")) == attachment_id:
                return self._ref_from_dict(attachment)
            return None
        if block.get("type") == "tool-result":
            return self._image_in_blocks(block.get("content"), attachment_id)
        return None

    @staticmethod
    def _ref_from_dict(value: dict) -> ImageAttachmentRef:
        original = None
        if value.get("originalDimensions") is not None:
            original = Dimensions(**value["originalDimensions"])
        return ImageAttachmentRef(
            attachmentId=AttachmentId(value["attachmentId"]),
            mediaType=value["mediaType"],
            bytes=value["bytes"],
            width=value["width"],
            height=value["height"],
            name=value.get("name"),
            originalDimensions=original,
        )

    # ---------- session.fork ----------

    def fork(self, payload: dict) -> dict:
        session_id = _require_id(payload, "sessionId")
        at_seq = payload.get("atSeq")
        if at_seq is not None and (not isinstance(at_seq, int)
                                   or isinstance(at_seq, bool) or at_seq < 0):
            raise _Reject("bad-request", "atSeq must be a non-negative integer", {})
        source = self.store.get(session_id)
        if source is None:
            raise _Reject("session-not-found", f'session "{session_id}" not found',
                          {"sessionId": session_id})
        events = source.events
        last_seq = events[-1]["seq"] if events else -1
        boundary = self._fork_boundary(events, at_seq)
        if boundary is None:
            if at_seq is not None and at_seq <= last_seq:
                raise _Reject("fork-unavailable",
                              f'session "{session_id}" has not completed the turn '
                              f'containing event {at_seq}',
                              {"sessionId": session_id})
            raise _Reject("fork-unavailable",
                          f'session "{session_id}" has no completed turn to fork from',
                          {"sessionId": session_id})
        cut = boundary["seq"] + 1
        while cut < len(events) and events[cut].get("type") != "turn/start":
            cut += 1
        child_id = f"session-{uuid.uuid4()}"
        meta: dict[str, Any] = {"parentSession": source.session_id, "seedLength": cut}
        if source.meta.get("cwd") is not None:
            meta["cwd"] = source.meta["cwd"]
        if source.meta.get("agentPreset") is not None:
            meta["agentPreset"] = source.meta["agentPreset"]
        self.store.create(child_id, {"seed": list(events[:cut]), "meta": meta})
        return {"sessionId": child_id}

    @staticmethod
    def _fork_boundary(events, at_seq: int | None) -> dict | None:
        """最近完成的 turn（turn/end），atSeq 给定则要求锚定 seq >= atSeq 且 turn 已闭合。"""
        if at_seq is not None:
            anchored = next((e for e in events
                             if e.get("type") == "turn/end" and e["seq"] >= at_seq), None)
            if anchored is not None:
                return anchored
            if events and at_seq <= events[-1]["seq"]:
                return None
        return next((e for e in reversed(events) if e.get("type") == "turn/end"), None)

    # ---------- session.page ----------

    def page(self, payload: dict) -> dict:
        session_id = self._address_target(payload)
        session = self.store.get(session_id)
        source = list(session.events)
        source_cursor = source[-1]["seq"] if source else -1

        through_seq = payload.get("throughSeq")
        if not isinstance(through_seq, int) or isinstance(through_seq, bool) or through_seq < -1:
            raise _Reject("bad-request", "throughSeq must be an integer greater than or equal to -1",
                          {})
        if through_seq > source_cursor:
            raise _Reject("bad-request",
                          f"session page through seq {through_seq} is past cursor {source_cursor}",
                          {"sessionId": session_id})
        if 0 <= through_seq < len(source) and source[through_seq]["seq"] != through_seq:
            raise _Reject("internal", f"session log does not contain through seq {through_seq}",
                          {"sessionId": session_id})

        before_seq = payload.get("beforeSeq")
        if before_seq is not None and (not isinstance(before_seq, int)
                                       or isinstance(before_seq, bool) or before_seq < 0):
            raise _Reject("bad-request", "beforeSeq must be a non-negative safe integer", {})
        max_messages = payload.get("maxMessages")
        if max_messages is not None and (not isinstance(max_messages, int)
                                         or isinstance(max_messages, bool) or max_messages <= 0):
            raise _Reject("bad-request", "maxMessages must be a positive safe integer", {})

        page_events, has_more = self._paginate(source, before_seq, max_messages or DEFAULT_MAX_MESSAGES,
                                               through_seq)
        return {"records": self._page_records(page_events), "hasMore": has_more}

    @staticmethod
    def _paginate(events, before_seq: int | None, max_messages: int,
                  through_seq: int | None = None) -> tuple[list[dict], bool]:
        """消息边界分页（history.ts paginate 同款）。"""
        if through_seq is None:
            through_seq = events[-1]["seq"] if events else -1
        end = min(through_seq + 1, before_seq) if before_seq is not None else through_seq + 1
        count = 0
        cut = 0
        for index in range(end - 1, -1, -1):
            event = events[index]
            if event.get("type") not in MESSAGE_TYPES or event.get("surfaceOp") != "append":
                continue
            count += 1
            group_start = event["seq"]
            for source in event.get("sourceEventSeqs") or []:
                group_start = min(group_start, source)
            if count >= max_messages:
                cut = group_start
                break
        return list(events[cut:end]), cut > 0

    def _page_records(self, events) -> list[dict]:
        """records 条目：ChunkRow 打包串折 chunkrow 条目，其余事件原样（history.ts 同款）。

        日志事件是冻结的 mappingproxy/tuple，先解冻再打包（classify 与 JSON
        序列化都需要普通 dict）。"""
        records = []
        for row in pack_chunk_runs([thaw(event) for event in events]):
            rtype = row.get("type")
            if rtype in ("text-chunks", "reasoning-chunks", "tool-call-chunks"):
                records.append({"type": "chunks",
                                "event": {"type": f"chunkrow/{rtype}", "seq": row["seq0"],
                                          "time": row["time0"], "data": row["data"]}})
            else:
                records.append({"type": "event", "event": row})
        return records

    # ---------- 流订阅工厂 ----------

    def follow(self, request: dict) -> _FollowSubscription:
        """session.follow：快照 + 增量（历史 follow 同款）。"""
        return _FollowSubscription(self, request)

    def control(self) -> _ControlSubscription:
        """session.control：队列/作业/投影基线 + 增量。"""
        return _ControlSubscription(self)

    # ---------- 队列投影（control.ts queueItems 同款） ----------

    def _queue_items(self, agent: AgentLoop, splice: Mapping | None = None) -> list[dict]:
        """队列投影（control.ts queueItems 同款）。

        广播点在内存 mutation 之前，splice 事件携带的是 pre-splice 状态；
        重投影 splice 到 inbox 当前列表上得到 post-splice 快照（与既有
        web/streams.py _queue_snapshot 同模式）。
        """

        def project(target: str) -> list[dict]:
            messages = agent.inbox.next_turn if target == "next-turn" \
                else agent.inbox.next_step
            if splice is not None and splice.get("target") == target:
                start = splice.get("start", 0)
                removed = splice.get("removedCount", 0)
                before = messages[:start]
                after = messages[start + removed:]
                return before + list(splice.get("inserted", [])) + after
            return messages

        items = []
        for message in project("next-turn"):
            items.append(self._queued_item(message, "queued"))
        for message in project("next-step"):
            placement = "steering" if (message.get("source") or {}).get("kind") == "user" \
                else "context"
            items.append(self._queued_item(message, placement))
        return items

    @staticmethod
    def _queued_item(message: dict, placement: str) -> dict:
        item: dict[str, Any] = {"id": message["id"], "placement": placement,
                                "message": {"id": message["id"],
                                            "content": thaw(message.get("content") or [])}}
        source = message.get("source")
        if isinstance(source, Mapping) and source.get("kind") == "user" and source.get("rpcId"):
            item["rpcId"] = source["rpcId"]
        return item

    # ---------- wire 辅助 ----------

    @staticmethod
    def _wire_header(session: Session) -> dict:
        return {"id": session.session_id, "meta": dict(session.meta),
                "createdAt": session.created_at}