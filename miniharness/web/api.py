"""web 会话服务：session 域 unary 方法（对齐 `packages/host/apiproxy/src/api/sessions.ts` + api-proxy.ts）。

方法集（迭代 1）：`host.describe` / `session.list` / `session.create` / `session.prompt`
/ `session.history` / `session.cancel` / `session.models`。每个方法都返回完整
`server-response` 帧（rpcId 回显请求的，不重铸）；业务错误一律经 `result.ok=false`
的 RpcError 表达，方法本身不抛业务异常（对齐上游 transport 层语义）。

契约（已逐条对照上游核实）：
  * `session.list` → `{items: SessionSummary[]}`，updatedAt 降序；updatedAt =
    max(createdAt, 最新 user/message 中 source.kind=='user' 事件 time)（api-proxy.ts:481-501）；
    blank = 无 `turn/start`（api-proxy.ts:476-478）；running 取所挂 agent 状态，
    冷会话恒 false；cwd/agentPreset/parentSessionId/origin 经 header.meta 透传。
  * `session.create` → `{sessionId, agentPreset?}`；预分配 sessionId 重试同 id+cwd
    返回同一会话、异 cwd → `session-conflict`（details {sessionId, requestedCwd,
    existingCwd}，rpc.schema.ts 同款）；缺省 id mint `session-<uuid>`（api-proxy.ts:2108）。
  * `session.prompt`：mode ∈ {queue, steer}；source = {kind:'user', rpcId, clientTimeZone?}
    （api-proxy.ts:2417-2421）；单文本块以 '/' 开头 → 命令注册表（未知 → `unknown-command`，
    状态错误 → `command-error`）；image 块需模型支持 + attachment 受理（迭代 1 无 → `attachment-error`）；
    clientTimeZone 非法 → `invalid-time-zone` {value}。返回 `{accepted:true}`。
  * `session.history` → `{events: HistoryEntry[], hasMore}`；HistoryEntry = {event, view?}
    （mini 无 view）；分页按 append 消息边界对齐：只数 surfaceOp=='append' 的
    user/message 与 assistant/message（api-proxy.ts:124,264-266），组起点经
    sourceEventSeqs 前扩（replacement 与其源消息同页），cut 以下全部返回，hasMore =
    cut>0；maxMessages 缺省 50；beforeSeq 严格小于锚。无投影注册表 → 不带 projections 块
    （sessions.ts:278 "A deployment without the registry serves histories without the block"）。
  * `session.cancel` → `{accepted:true}`，保留 pending inbox（agent.cancel keepInbox，FIFO 恢复）。
  * `session.models` → `{current, routable, groups, failures}`；current 取适配器
    resolve_model_info；routable = 单适配器恒真；groups 单 provider 单 model。
  * `host.describe` → `{version, cwd, provider, model, attachedSessions, canOpenPath}`，
    request payload 是空对象（host.schema.ts:11）；canOpenPath 恒 false（无原生桌面）。

迭代 1 简化（须在 AGENTS.md 标注）：
  * 无 workspace / projection / search / selectModel / rename / fork / attachment /
    updateQueue 方法；create 带 workspaceId 直接 `workspace-not-found`。
  * 会话仅内存（SessionStore 注册表），不落 JSONL；web 会话不跨进程恢复。
  * clientTimeZone 规范化仅接受 UTC 与 zoneinfo 可解析的 IANA 名，回显输入原样
    （上游 canonicalize 别名折叠，mini 无折叠）；Windows 无 tzdata 时非 UTC 全部拒绝。
  * agentPreset 存 header.meta 原样回显（上游从日志解析"会话实际运行"的组合）。
  * HistoryEntry 无 view 槽（mini 无 tool presenter 注册表）。
"""
from __future__ import annotations

import os
import uuid
from typing import Any

from ..commands import parse_command
from ..core.agent_loop.agent import AgentLoop
from ..core.scope import Context
from ..core.session import Session, create_message, text_block
from ..core.session_store import SessionStore
from ..core.tools import ToolRegistry
from ..core.version import __version__
from ..llm import LlmAdapter
from .envelope import rpc_error, rpc_result_ok

__all__ = [
    "WebApi",
    "canonical_client_time_zone",
    "DEFAULT_MAX_MESSAGES",
    "MESSAGE_TYPES",
]

#: history 尾页缺省窗口（api-proxy.ts DEFAULT_MAX_MESSAGES）
DEFAULT_MAX_MESSAGES = 50

#: 分页按 append 消息边界对齐：只数这两种 append 消息（api-proxy.ts:124）
MESSAGE_TYPES = frozenset({"user/message", "assistant/message"})


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


def _require_string(payload: dict, key: str, issues: list[str]) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    issues.append(f"missing or non-string field: {key}")
    return None


class WebApi:
    """web 会话服务核心：session 域 unary 方法 + 路由表。

    装配：ctx（根上下文）、adapter、tools。`ctx.sessions` 存在则复用，否则自建
    SessionStore（挂在根 ctx 上）。每个会话在 create 时挂一个常驻 AgentLoop
    （driver 模式），owner 作用域 = loop 自己的 scope，session/* 事件沿
    owner→根祖先链派发（mux 流订阅根即全量收）。
    """

    def __init__(self, ctx: Context, adapter: LlmAdapter, tools: ToolRegistry | None = None,
                 cwd: str | None = None):
        self.ctx = ctx
        self.adapter = adapter
        self.tools = tools if tools is not None else ToolRegistry(ctx)
        self.cwd = cwd or os.getcwd()
        try:
            self.store = ctx.inject("sessions")
        except KeyError:
            self.store = SessionStore(ctx)
            ctx.provide("sessions", self.store)
        self._agents: dict[str, AgentLoop] = {}

    # ---------- 路由 ----------

    ROUTES: dict[str, str] = {
        "host.describe": "describe",
        "session.list": "list_sessions",
        "session.create": "create_session",
        "session.prompt": "prompt",
        "session.history": "history",
        "session.cancel": "cancel",
        "session.models": "models",
    }

    def methods(self) -> frozenset[str]:
        return frozenset(self.ROUTES)

    def dispatch(self, method: str, rpc_id: str, payload: Any) -> dict | None:
        """按上游路由表派发；未知方法返回 None（载体层映射 404）。

        返回完整 server-response 帧。payload 非 dict 时按 bad-request 拒绝。
        """
        handler = self.ROUTES.get(method)
        if handler is None:
            return None
        if not isinstance(payload, dict):
            return self._bad_request(rpc_id, ["payload must be a JSON object"])
        return getattr(self, handler)(rpc_id, payload)

    # ---------- 装配 ----------

    def _attach(self, session: Session) -> AgentLoop:
        loop = AgentLoop(session, self.adapter, self.tools, self.ctx)
        detach = self.store.enter(session, owner_ctx=loop.ctx)
        try:
            self.store.announce(session)
        except Exception:
            detach()
            raise
        self._agents[session.session_id] = loop
        try:
            loop.start_driver()
        except RuntimeError:
            # 事件循环未运行：driver 首次 prompt 时惰性启动（离线装配兜底）
            pass
        return loop

    def _agent_for(self, session_id: str) -> tuple[Session, AgentLoop] | None:
        session = self.store.get(session_id)
        if session is None:
            return None
        loop = self._agents.get(session_id)
        if loop is None:
            loop = self._attach(session)
        return session, loop

    def _selection(self) -> dict:
        info = self.adapter.resolve_model_info()
        return {"provider": info.get("provider") or "unknown", "model": info.get("model") or "unknown"}

    # ---------- 响应构造 ----------

    def _ok(self, rpc_id: str, value: Any) -> dict:
        return {"type": "server-response", "rpcId": rpc_id, "result": rpc_result_ok(value)}

    def _err(self, rpc_id: str, code: str, message: str, details: dict | None = None) -> dict:
        return {"type": "server-response", "rpcId": rpc_id,
                "result": {"ok": False, "error": rpc_error(code, message, details)}}

    def _bad_request(self, rpc_id: str, issues: list[str]) -> dict:
        return self._err(rpc_id, "bad-request",
                         "payload failed validation",
                         {"issues": [{"path": [], "message": issue} for issue in issues]})

    # ---------- host ----------

    def describe(self, rpc_id: str, payload: dict) -> dict:
        selection = self._selection()
        return self._ok(rpc_id, {
            "version": __version__,
            "cwd": self.cwd,
            "provider": selection["provider"],
            "model": selection["model"],
            "attachedSessions": len(self._agents),
            "canOpenPath": False,
        })

    # ---------- session.list ----------

    @staticmethod
    def _list_metadata(session: Session) -> dict:
        """折叠 list 提示投影：blank + lastPromptAt（api-proxy.ts:481-489 同款）。"""
        blank = True
        last_prompt_at = None
        for event in session.events:
            if blank and event.get("type") == "turn/start":
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
        if meta.get("agentPreset") is not None:
            summary["agentPreset"] = meta["agentPreset"]
        return summary

    def list_sessions(self, rpc_id: str, payload: dict) -> dict:
        items = []
        for session in self.store.list():
            loop = self._agents.get(session.session_id)
            running = loop.status == "running" if loop is not None else False
            items.append(self._summary(session, running))
        items.sort(key=lambda item: item["updatedAt"], reverse=True)
        return self._ok(rpc_id, {"items": items})

    # ---------- session.create ----------

    def create_session(self, rpc_id: str, payload: dict) -> dict:
        workspace_id = payload.get("workspaceId")
        if workspace_id is not None:
            return self._err(rpc_id, "workspace-not-found",
                             f'workspace "{workspace_id}" not found',
                             {"workspaceId": workspace_id})
        issues: list[str] = []
        session_id = payload.get("sessionId")
        if session_id is not None and not (isinstance(session_id, str) and session_id):
            issues.append("sessionId must be a non-empty string")
        agent_preset = payload.get("agentPreset")
        if agent_preset is not None and not isinstance(agent_preset, str):
            issues.append("agentPreset must be a string")
        cwd = payload.get("cwd")
        if cwd is not None and not (isinstance(cwd, str) and os.path.isabs(cwd)):
            issues.append("cwd must be an absolute path")
        if issues:
            return self._bad_request(rpc_id, issues)
        cwd = cwd if cwd is not None else self.cwd

        if session_id is not None:
            existing = self.store.get(session_id)
            if existing is not None:
                existing_cwd = existing.meta.get("cwd")
                if existing_cwd == cwd:
                    return self._ok(rpc_id, self._create_result(session_id, self._preset_of(existing)))
                return self._err(rpc_id, "session-conflict",
                                 f'session "{session_id}" already exists with a different cwd',
                                 {"sessionId": session_id, "requestedCwd": cwd,
                                  "existingCwd": existing_cwd})
        if session_id is None:
            session_id = f"session-{uuid.uuid4()}"
        meta: dict[str, Any] = {"cwd": cwd}
        if agent_preset is not None:
            meta["agentPreset"] = agent_preset
        session = self.store.prepare(session_id, {"meta": meta})
        self._attach(session)
        return self._ok(rpc_id, self._create_result(session.session_id, self._preset_of(session)))

    @staticmethod
    def _create_result(session_id: str, agent_preset: str | None) -> dict:
        result: dict[str, Any] = {"sessionId": session_id}
        if agent_preset is not None:
            result["agentPreset"] = agent_preset
        return result

    @staticmethod
    def _preset_of(session: Session) -> str | None:
        return session.meta.get("agentPreset")

    # ---------- session.prompt ----------

    def prompt(self, rpc_id: str, payload: dict) -> dict:
        issues: list[str] = []
        session_id = _require_string(payload, "sessionId", issues)
        mode = payload.get("mode")
        if mode not in ("queue", "steer"):
            issues.append("mode must be one of 'queue', 'steer'")
        content = payload.get("content")
        if not isinstance(content, list) or not content:
            issues.append("content must be a non-empty array of PromptContentPart")
        else:
            for part in content:
                if not isinstance(part, dict) or part.get("type") not in ("text", "image"):
                    issues.append("each content part must be a text or image object")
                elif part.get("type") == "text" and not isinstance(part.get("text"), str):
                    issues.append("text part must carry a string text")
        if issues:
            return self._bad_request(rpc_id, issues)

        client_time_zone = payload.get("clientTimeZone")
        canonical_time_zone = None
        if client_time_zone is not None:
            canonical_time_zone = canonical_client_time_zone(client_time_zone)
            if canonical_time_zone is None:
                return self._err(rpc_id, "invalid-time-zone",
                                 "clientTimeZone must be UTC or a valid IANA Area/Location name",
                                 {"value": client_time_zone})

        found = self._agent_for(session_id)
        if found is None:
            return self._err(rpc_id, "session-not-found",
                             f'session "{session_id}" not found (not attached)',
                             {"sessionId": session_id})
        loop = found[1]

        has_image = any(part.get("type") == "image" for part in content)
        if has_image:
            info = self.adapter.resolve_model_info()
            modalities = info.get("input_modalities") or ["text"]
            if "image" not in modalities:
                return self._err(rpc_id, "attachment-error",
                                 f'Model "{info.get("model") or "unknown"}" does not support image input.',
                                 {"reason": "MODEL_DOES_NOT_SUPPORT_IMAGES"})
            return self._err(rpc_id, "attachment-error",
                             "image prompt admission is not implemented in iteration 1 (no attachment service)",
                             {"reason": "ATTACHMENT_UNAVAILABLE"})

        # 单文本块以 '/' 开头 → 命令注册表（永不进模型；上游 sessions.ts:317-321）
        if len(content) == 1 and content[0].get("type") == "text":
            text = content[0].get("text")
            if isinstance(text, str) and text.startswith("/"):
                return self._route_command(loop, text, rpc_id)

        source: dict[str, Any] = {"kind": "user", "rpcId": rpc_id}
        if canonical_time_zone is not None:
            source["clientTimeZone"] = canonical_time_zone
        blocks = [text_block(part["text"]) for part in content]
        message = create_message("user", blocks, source)
        if mode == "steer":
            loop.steer(message)
        else:
            loop.followup(message)
        return self._ok(rpc_id, {"accepted": True})

    def _route_command(self, loop: AgentLoop, text: str, rpc_id: str) -> dict:
        try:
            commands = self.ctx.inject("commands")
        except KeyError:
            return self._err(rpc_id, "unknown-command",
                             "no command registry is composed in this deployment", {})
        name, _ = parse_command(text)
        if name is None:
            return self._err(rpc_id, "unknown-command",
                             f'no command named by "{text}"', {"command": text})
        result = commands.dispatch(loop, text)
        if result is None:
            return self._err(rpc_id, "unknown-command",
                             f'no command named "/{name}"', {"command": text})
        if result["kind"] == "error":
            return self._err(rpc_id, "command-error", result["text"], {})
        command: dict[str, Any] = {"kind": "success"}
        if result["text"]:
            command["text"] = result["text"]
        return self._ok(rpc_id, {"accepted": True, "command": command})

    # ---------- session.history ----------

    @staticmethod
    def _paginate(events, before_seq: int | None, max_messages: int) -> tuple[list[dict], bool]:
        """消息边界分页（api-proxy.ts:247-282 同款）。"""
        window = list(events) if before_seq is None else [e for e in events if e["seq"] < before_seq]
        count = 0
        cut = 0
        for i in range(len(window) - 1, -1, -1):
            event = window[i]
            if event.get("type") not in MESSAGE_TYPES or event.get("surfaceOp") != "append":
                continue
            count += 1
            group_start = event["seq"]
            for source in event.get("sourceEventSeqs") or []:
                if source < group_start:
                    group_start = source
            if count >= max_messages:
                cut = group_start
                break
        page = [e for e in window if e["seq"] >= cut]
        return page, cut > 0

    def history(self, rpc_id: str, payload: dict) -> dict:
        issues: list[str] = []
        session_id = _require_string(payload, "sessionId", issues)
        before_seq = payload.get("beforeSeq")
        if before_seq is not None and (not isinstance(before_seq, int)
                                       or isinstance(before_seq, bool) or before_seq < 0):
            issues.append("beforeSeq must be a non-negative integer")
        max_messages = payload.get("maxMessages")
        if max_messages is not None and (not isinstance(max_messages, int)
                                         or isinstance(max_messages, bool) or max_messages <= 0):
            issues.append("maxMessages must be a positive integer")
        if issues:
            return self._bad_request(rpc_id, issues)

        session = self.store.get(session_id)
        if session is None:
            return self._err(rpc_id, "session-not-found",
                             f'session "{session_id}" not found',
                             {"sessionId": session_id})
        page, has_more = self._paginate(session.events, before_seq, max_messages or DEFAULT_MAX_MESSAGES)
        return self._ok(rpc_id, {
            "events": [{"event": event} for event in page],
            "hasMore": has_more,
        })

    # ---------- session.cancel ----------

    def cancel(self, rpc_id: str, payload: dict) -> dict:
        issues: list[str] = []
        session_id = _require_string(payload, "sessionId", issues)
        if issues:
            return self._bad_request(rpc_id, issues)
        found = self._agent_for(session_id)
        if found is None:
            return self._err(rpc_id, "session-not-found",
                             f'session "{session_id}" not found (not attached)',
                             {"sessionId": session_id})
        found[1].cancel("user", keep_inbox=True)
        return self._ok(rpc_id, {"accepted": True})

    # ---------- session.models ----------

    def models(self, rpc_id: str, payload: dict) -> dict:
        issues: list[str] = []
        session_id = _require_string(payload, "sessionId", issues)
        if issues:
            return self._bad_request(rpc_id, issues)
        if self._agents.get(session_id) is None and self.store.get(session_id) is None:
            return self._err(rpc_id, "session-not-found",
                             f'session "{session_id}" not found (not attached)',
                             {"sessionId": session_id})
        selection = self._selection()
        provider = selection["provider"]
        model = selection["model"]
        return self._ok(rpc_id, {
            "current": {"provider": provider, "model": model},
            "routable": True,
            "groups": [{"id": provider, "name": provider,
                        "models": [{"id": model, "name": model}]}],
            "failures": [],
        })