"""web 会话服务：session 域 unary + 流订阅工厂（对齐 `packages/api/session-controller`）。

方法集（alpha.1 已核实）：`session/list` / `session/search` / `session/create` /
`session/selectModel` / `session/modelCatalog` / `session/initializeDefaultModel` /
`session/canOpenWorkspacePath` / `session/openWorkspacePath` / `session/rename` /
`session/fork` / `session/prompt` / `session/attachment` / `session/updateQueue` /
`session/cancel` / `session/page`；流方法 `session/follow` / `session/control` 返回
缓冲订阅对象（api 层同步可测，事件循环泵由载体层 web/mux.py 驱动）。`host.describe` /
`session/history` / `session/models` 已从新契约消失，连同 apiproxy 阶段的命令路由一起移除。
本文件另承载 workspace / workspaceFiles / terminal / settings / credentials / job /
agentPresets / schedule / account / userQuestions / feedback / permissionPresets /
sessionReferenceResolver 等 Remote namespace 的 unary 方法面（各域见对应小节）。

契约已逐条对照上游源码核实（status/upstream/baseline.md D18-21）；错误分支的
消息文案与 details 形状来自 session-controller/src/{commands,agent,history,
control,list,catalog}.ts 的字面量。方法签名：`handler(payload) -> value`，
业务错误一律抛 `_Reject`（dispatch 折进 RpcResult 错误分支），方法本身不抛
业务异常（对齐上游 transport 层语义）。

mini 教学简化（须同步 verified-diffs §3.4 / AGENTS.md）：
  * 单适配器部署：modelCatalog / selectModel 直接导出适配器路由；selectModel
    的“记录”仅 advisory（回落恒单模型），不驱动真实路由。
  * 无 projection/ChunkRow 注册表：follow/control 的 projections values 由
    `telemetry/projection_values` 现场折叠为真实 sessionStats/tokenUsage 视图
    （因果固定单位现场折叠等价于注册表，见 verified-diffs §2.30）；control 的
    queue 帧直接发 inbox 当前态（省略 asOfSeq 投影簿记，订阅方观察语义一致）。
  * follow/page 的 subagent 地址分支全部拒绝（mini 不驱动子代理会话，防御对齐）。
  * search 在 live 会话 surface 快照上扫描，不落全文索引。
  * 精确 details 附加字段按 mini 教学面导出（agent-preset/conflict /
    session/conflict 的消息文案对齐上游字面量，但携带 mini 的 requested* 路径字段）。
"""
from __future__ import annotations

import base64
import os
import uuid
from collections.abc import Mapping
from typing import Any

from ..attachment.admission import admit_encoded_images
from ..attachment.error import AttachmentError
from ..attachment.file_uploads import install_file_uploads
from ..attachment.types import (
    AttachmentId,
    Dimensions,
    EncodedFileAttachment,
    EncodedImageAttachment,
    ImageAttachmentRef,
)
from ..core.agent_loop.agent import AgentLoop
from ..core.agent_loop.resident_loop import run_on_resident
from ..core.scope import Context
from ..core.session import SESSION_FORMAT_VERSION, Session, thaw
from ..core.session.message import create_message, file_block, image_block, text_block
from ..core.session.surface import derive_event_message
from ..core.session_store import SessionForkError, SessionStore
from ..core.agents import install_agents
from ..core.tools import ToolRegistry
from ..llm import LlmAdapter
from ..session_projection import install_session_projections
from ..telemetry import install_usage_stats, projection_values
from .args import (
    BoundaryReject,
    boundary_error_message,
    canonical_endpoint,
    validate_args,
)
from .envelope import RPC_ERROR_CODES, rpc_error, rpc_result_ok
from .inventory import build_inventory

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


def _is_safe_int(value: Any) -> bool:
    """JS Number.isSafeInteger 等价：int（非 bool）且 |v| <= 2**53-1。"""
    return (isinstance(value, int) and not isinstance(value, bool)
            and -9007199254740991 <= value <= 9007199254740991)


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
        raise _Reject("gateway/bad-request", f"{key} must be a non-empty string", {})
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
    """session.follow 流订阅（WebController.follow + observation 快照）。

    构造即校验 + 快照；失败以 `error` 状态暴露（mux 转流 error 帧），不抛异常。
    """

    def __init__(self, api: "WebApi", request: dict):
        super().__init__()
        self.api = api
        self._disposers: list[Any] = []
        try:
            self._start(request)
        except _Reject as error:
            self._fail(error.code, error.message, error.details)

    def _start(self, request: dict) -> None:
        from .assistant_stream_follow import bind_assistant_stream
        from .event_listener import emit_event_frame, expand_created_backfill
        from .follow import opening_for_session_follow
        from .history import address_id

        api = self.api
        from .follow import _validate_follow_address

        address = request.get("address")
        if address is None:
            raise _Reject("gateway/arguments-invalid",
                          'args fields do not match the descriptor: missing "address"', {})
        _validate_follow_address(address)
        session_id = address_id(address)
        want_assistant_stream = request.get("assistantStream") is True
        opening, next_seq = opening_for_session_follow(api, request)
        self._push(opening)
        state: dict[str, Any] = {"next_seq": next_seq}

        def on_event(payload: dict) -> None:
            if self.done:
                return
            current = payload.get("session")
            event = payload.get("event")
            if getattr(current, "session_id", None) != session_id or not isinstance(event, Mapping):
                return
            seq = event["seq"]
            expected = state["next_seq"]
            if seq < expected:
                return
            if seq != expected:
                self._fail("gateway/internal",
                           f"session event stream skipped seq {expected}", {})
                return
            state["next_seq"] = seq + 1
            self._push(emit_event_frame(event))

        def on_created(payload: dict) -> None:
            if self.done:
                return
            session = payload.get("session")
            if getattr(session, "session_id", None) != session_id:
                return
            frames, state["next_seq"] = expand_created_backfill(session, state["next_seq"])
            for frame in frames:
                self._push(frame)

        ctx = api.ctx
        self._disposers.extend([
            ctx.on("session/event", on_event, global_=True),
            ctx.on("session/created", on_created, global_=True),
        ])
        if want_assistant_stream:
            self._disposers.append(
                bind_assistant_stream(ctx, session_id, self._push))

    def close(self) -> None:
        for dispose in self._disposers:
            dispose()
        self._disposers.clear()
        self._end()


class _ObserveEventSubscription(_Subscription):
    """session/observeEvent：ready + 后续 durable 事件（无 snapshot）。"""

    def __init__(self, api: "WebApi", request: dict):
        super().__init__()
        self._disposers: list[Any] = []
        try:
            from .event_listener import emit_event_frame, expand_created_backfill
            from .history import address_id

            address = request.get("address")
            if not isinstance(address, dict) or address.get("kind") not in ("session", "subagent"):
                raise _Reject("gateway/bad-request",
                              "address must be a session or subagent address", {})
            session_id = address_id(address)
            self._push({"type": "ready"})
            state: dict[str, Any] = {"next_seq": None}

            def on_event(payload: dict) -> None:
                if self.done:
                    return
                current = payload.get("session")
                event = payload.get("event")
                if getattr(current, "session_id", None) != session_id or not isinstance(event, Mapping):
                    return
                seq = event["seq"]
                next_seq = state["next_seq"]
                if next_seq is not None and seq < next_seq:
                    return
                if next_seq is not None and seq != next_seq:
                    self._fail("gateway/internal",
                               f"session event stream skipped seq {next_seq}", {})
                    return
                state["next_seq"] = seq + 1
                self._push(emit_event_frame(event))

            def on_created(payload: dict) -> None:
                if self.done:
                    return
                session = payload.get("session")
                if getattr(session, "session_id", None) != session_id:
                    return
                try:
                    frames, next_seq = expand_created_backfill(session, state["next_seq"])
                except _Reject as error:
                    self._fail(error.code, error.message, error.details)
                    return
                for frame in frames:
                    self._push(frame)
                state["next_seq"] = next_seq

            ctx = api.ctx
            self._disposers = [
                ctx.on("session/event", on_event, global_=True),
                ctx.on("session/created", on_created, global_=True),
            ]
        except _Reject as error:
            self._fail(error.code, error.message, error.details)

    def close(self) -> None:
        for dispose in self._disposers:
            dispose()
        self._disposers = []
        self._end()


class _ObserveStateSubscription(_Subscription):
    """session/observeState：完整 passive state 替换帧。"""

    def __init__(self, api: "WebApi", request: dict):
        super().__init__()
        self._listener = None
        self._thread = None
        try:
            import threading

            from .session_passive import read_session_state_resolved
            from .state_listener import StateListenerSync

            session_id = request.get("sessionId")
            if not isinstance(session_id, str) or session_id == "":
                raise _Reject("gateway/bad-request", "sessionId must not be empty", {})
            self._listener = StateListenerSync(
                api, session_id,
                lambda: read_session_state_resolved(api, session_id))
            self._listener.start()
            self._thread = threading.Thread(target=self._pump_states, daemon=True)
            self._thread.start()
        except _Reject as error:
            self._fail(error.code, error.message, error.details)

    def _pump_states(self) -> None:
        listener = self._listener
        if listener is None:
            return
        try:
            for versioned in listener.iter_versioned_states():
                if self.done:
                    break
                if listener.is_current(versioned["revision"]):
                    self._push(versioned["state"])
        except _Reject as error:
            self._fail(error.code, error.message, error.details)
        finally:
            if not self.done:
                self._end()

    def close(self) -> None:
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._end()


class _ObserveProjectionsSubscription(_Subscription):
    """session/observeProjections：ready + 稀疏 projection 替换帧。"""

    def __init__(self, api: "WebApi", request: dict):
        super().__init__()
        self._dispose = None
        try:
            session_id = request.get("sessionId")
            if not isinstance(session_id, str) or session_id == "":
                raise _Reject("gateway/bad-request", "sessionId must not be empty", {})
            registry = api.ctx.get("sessionProjections")
            if registry is None:
                raise _Reject("session/projections-unavailable",
                              "Session projections are unavailable", {})
            self._push({"type": "ready"})

            def on_changed(session, key, value, seq) -> None:
                if self.done or session.session_id != session_id:
                    return
                self._push({"type": "projection", "key": key, "value": value, "seq": seq})

            self._dispose = registry.on_changed(on_changed)
        except _Reject as error:
            self._fail(error.code, error.message, error.details)

    def close(self) -> None:
        if self._dispose is not None:
            self._dispose()
            self._dispose = None
        self._end()


class _ControlSubscription(_Subscription):
    """session.control 流订阅（control.ts 同款）：投影基线 + projection 替换帧。

    基线只含 `projections`（上游 SessionControlBaseline）；`queues`/`jobs` 已从
    control wire 移除（其投影归 SessionQueuedItem/SessionJob，从 wire 删除）。
    无投影注册表 → 只放基线（`{asOfSeq, values}` 仍由现场折叠给出真实视图），
    无 live 替换帧（upstream control 的 `onChanged` 依赖注册表在场）。
    """

    def __init__(self, api: "WebApi"):
        super().__init__()
        self.api = api
        self._dispose = None
        self._start()

    def _start(self) -> None:
        api = self.api
        baseline: dict[str, Any] = {"projections": {}}
        for session in api.store.list():
            baseline["projections"][session.session_id] = {
                "asOfSeq": session.seq - 1,
                "values": projection_values(
                    session, api.ctx.get("usageStats"),
                    api.ctx.get("sessionProjections"))}
        self._push({"type": "baseline", "value": baseline})

        registry = api.ctx.get("sessionProjections")
        if registry is None:
            return

        def on_changed(session, key, value, seq) -> None:
            if self.done:
                return
            self._push({"type": "projection", "sessionId": session.session_id,
                        "key": key, "value": value, "seq": seq})

        self._dispose = registry.on_changed(on_changed)

    def close(self) -> None:
        if self._dispose is not None:
            self._dispose()
            self._dispose = None
        self._end()


def _wire_model_from_adapter_info(info: dict) -> dict | None:
    """适配器 `available_models` 行 → wire 模型行（modelCatalog / discoverModels）。"""
    model_id = info.get("model")
    if not isinstance(model_id, str) or model_id == "":
        return None
    row: dict[str, Any] = {"id": model_id, "name": info.get("name") or model_id}
    modalities = info.get("input_modalities")
    if modalities is not None:
        row["inputModalities"] = modalities
    badge = info.get("badge")
    if isinstance(badge, dict):
        row["badge"] = badge
    return row


class WebApi:
    """web 会话服务核心：session 域 unary 方法 + 流订阅工厂。

    装配：ctx（根上下文）、adapter、tools。`ctx.sessions` 存在则复用，否则自建
    SessionStore（挂在根 ctx 上）。每个会话在 create 时经 loop.publish() 挂一个
    常驻 AgentLoop（driver 模式），店成员资格归 loop 所有。`ctx.attachments`
    存在则启用真实图片受理（attachment/ 服务族），否则 image 提示以
    ATTACHMENT_UNAVAILABLE 拒绝（如实标注部署能力）。
    """

    def __init__(self, ctx: Context, adapter: LlmAdapter, tools: ToolRegistry | None = None,
                 cwd: str | None = None, roster: Any = None,
                 schedule_root: str | None = None):
        self.ctx = ctx
        self.adapter = adapter
        from ..seams.credentials_local import wire_llm_adapter_credentials

        wire_llm_adapter_credentials(ctx, adapter)
        self.tools = tools if tools is not None else ToolRegistry(ctx)
        self.cwd = cwd or os.getcwd()
        self.roster = roster
        self.store = ctx.get("sessions")
        if self.store is None:
            self.store = SessionStore(ctx)
        install_agents(ctx)
        # 上游 base 默认挂 agent-default-model（入口创建 Agent 时的部署默认）。
        # 生产入口的 profile 条目已装则复用；直接构造 WebApi（测试/嵌入）按
        # 适配器的当前路由补装，保存面仍是 no-op（无 profile entry）。
        if ctx.get("agentDefaultModel") is None:
            from ..core.agent_default_model import install_agent_default_model
            info = adapter.resolve_model_info()
            install_agent_default_model(
                ctx,
                provider=info.get("provider") or "unknown",
                model=info.get("model") or "unknown",
            )
        # 上游 base 在 host plane 挂 goal 域 + goal-round-driver（web-app 只把
        # command-goal/tool-goal 下放到 preset）。缺省装配保证 `goals/*` Remote
        # 与 preset 的 `goal` 工具有宿主服务可依。
        if ctx.get("goals") is None:
            from ..goal import install_goal_driver, install_goals
            install_goal_driver(ctx, install_goals(ctx))
        # 上游 base 挂 commands 服务（`/`命令面）；web-app 的预设负责具体命令。
        if ctx.get("commands") is None:
            from ..commands import install_commands
            install_commands(ctx)
        # 上游 base 挂 storage、web-app 挂 schedule + preset 的 tool-schedule。
        # 生产入口（launcher）显式给 root；测试/嵌入不传则保持不写用户主目录。
        if schedule_root is not None:
            from ..schedule import install_host_schedule
            install_host_schedule(ctx, root=schedule_root)
        # sessionProjections 注册表 + telemetry 投影单元：session/projections 读面与
        # session/control 的 projection 帧都要求注册表在场（对齐上游 control.ts
        # 构造注入 `ctx.sessionProjections`）。已装则幂等复用。
        projections = install_session_projections(ctx)
        # dsh-v0.2.0-rc.2：userQuestions 服务在场时注册其提问投影单元（上游
        # UserQuestionService 构造时 ctx.inject(['sessionProjections']) 注册）。
        if ctx.get("userQuestions") is not None:
            from ..interaction.user_questions import register_user_question_projection

            register_user_question_projection(projections)
        install_usage_stats(ctx)
        # 会话当前工作目录服务（对齐上游 dsh-working-directory）：装配 fs /
        # systemPrompt 前置（sessionProjections 已在上面装配）；工具随注册表收编。
        if ctx.get("systemPrompt") is None:
            from ..core.system_prompt import install_system_prompt
            install_system_prompt(ctx)
        # 上游 base 挂 skill-filesystem（catalog/provider）；mini 的 install_skills
        # 幂等装配注册表 + filesystem provider + 提示词节，保证 `skills/list` 有源。
        if ctx.get("skills") is None:
            from ..skills import install_skills
            install_skills(ctx)
        if ctx.get("fs") is None:
            from ..fs import install_local_fs
            install_local_fs(ctx, {"cwd": self.cwd})
        from ..working_directory import install_working_directory, register_working_directory_tool
        install_working_directory(ctx)
        if self.tools.resolve("working_directory") is None:
            register_working_directory_tool(ctx, self.tools)
        self.attachments = ctx.get("attachments")
        self.file_uploads = install_file_uploads(ctx)
        from ..office_to_pdf import install_office_to_pdf
        from ..remote_access import install_remote_access
        install_office_to_pdf(ctx)
        install_remote_access(ctx)
        self._agents: dict[str, AgentLoop] = {}
        # selectModel 记录（advisory：单适配器部署回落恒单模型，不驱动路由）
        self._selections: dict[str, dict] = {}
        # GatewayStreams：Remote 方法面（$events + follow/control + 审批桥）；
        # create_app(api, api.gateway) 装配进 FastAPI。
        from .streams import GatewayStreams
        self.gateway = GatewayStreams(self)
        self.approvals = self.gateway.approvals
        self.questions = self.gateway.questions
        runtime = ctx.get("webhookRuntime")
        if runtime is not None:
            runtime.bind_api(self)

    # ---------- 路由 ----------

    ROUTES: dict[str, str] = {
        "session/list": "list_sessions",
        "session/search": "search",
        "session/create": "create_session",
        "session/selectModel": "select_model",
        "session/modelCatalog": "model_catalog",
        "session/initializeDefaultModel": "initialize_default_model",
        "session/canOpenWorkspacePath": "can_open_workspace_path",
        "session/openWorkspacePath": "open_workspace_path",
        "session/rename": "rename",
        "session/fork": "fork",
        "session/prompt": "prompt",
        "session/attachment": "attachment",
        "session/updateQueue": "update_queue",
        "session/cancel": "cancel",
        "session/page": "page",
        "session/events": "events",
        "session/state": "state",
        "session/wait": "wait",
        "session/projections": "projections",
        "session/workspacePathApplications": "workspace_path_applications",
        "pluginInventory/list": "plugin_inventory",
        "pluginManager/listPlugins": "plugin_manager_list",
        "pluginManager/setPluginEnabled": "plugin_manager_set_enabled",
        "fileUploads/upload": "file_uploads_upload",
        "terminal/environment": "terminal_environment",
        "terminal/shells": "terminal_shells",
        "terminal/list": "terminal_list",
        "terminal/create": "terminal_create",
        "terminal/rename": "terminal_rename",
        "terminal/close": "terminal_close",
        "workspace/create": "workspace_create",
        "workspace/initializeDefault": "workspace_initialize_default",
        "workspace/rename": "workspace_rename",
        "workspace/delete": "workspace_delete",
        "workspace/insertBefore": "workspace_insert_before",
        "workspace/insertSessionBefore": "workspace_insert_session_before",
        "workspace/archiveSession": "workspace_archive_session",
        "workspace/unarchiveSession": "workspace_unarchive_session",
        "workspace/pinSession": "workspace_pin_session",
        "workspace/unpinSession": "workspace_unpin_session",
        "workspaceFiles/read": "workspace_files_read",
        "workspaceFiles/readBytes": "workspace_files_read_bytes",
        "workspaceFiles/stat": "workspace_files_stat",
        "workspaceFiles/list": "workspace_files_list",
        "officeToPdf/render": "office_to_pdf_render",
        "officeToPdf/getGeneration": "office_to_pdf_get_generation",
        "settings/describe": "settings_describe",
        "settings/update": "settings_update",
        "settings/replace": "settings_replace",
        "settings/mutate": "settings_mutate",
        "settings/openSettingsDocument": "settings_open_document",
        "credentials/describe": "credentials_describe",
        "credentials/set": "credentials_set",
        "credentials/unset": "credentials_unset",
        "fileReferences/list": "file_references_list",
        "commands/list": "commands_list",
        "commands/execute": "commands_execute",
        "directoryPicker/list": "directory_picker_list",
        "directoryPicker/createDirectory": "directory_picker_create_directory",
        "directoryPicker/pick": "directory_picker_pick",
        "llm/listProviders": "llm_list_providers",
        "llm/listConfigurableProviders": "llm_list_configurable_providers",
        "llm/discoverModels": "llm_discover_models",
        "skills/list": "skills_list",
        "goals/get": "goals_get",
        "goals/create": "goals_create",
        "goals/edit": "goals_edit",
        "goals/pause": "goals_pause",
        "goals/resume": "goals_resume",
        "goals/complete": "goals_complete",
        "goals/clear": "goals_clear",
        "sessionReferenceResolver/candidates": "session_reference_candidates",
        "job/kill": "job_kill",
        "agentPresets/list": "agent_presets_list",
        "agentPresets/read": "agent_presets_read",
        "agentPresets/select": "agent_presets_select",
        "userQuestions/answer": "user_questions_answer",
        "sessionFeedback/record": "session_feedback_record",
        "messageFeedback/list": "message_feedback_list",
        "messageFeedback/put": "message_feedback_put",
        "messageFeedback/delete": "message_feedback_delete",
        "permissionPresets/catalog": "permission_presets_catalog",
        "schedule/list": "schedule_list",
        "schedule/catalog": "schedule_catalog",
        "schedule/history": "schedule_history",
        "schedule/delete": "schedule_delete",
        "schedule/update": "schedule_update",
        "account/getState": "account_get_state",
        "account/getProfile": "account_get_profile",
        "account/getBalance": "account_get_balance",
        "account/getUnnotifiedBonuses": "account_get_unnotified_bonuses",
        "account/ackBonusNotified": "account_ack_bonus_notified",
        "account/hasRunningAccountTasks": "account_has_running_account_tasks",
        "account/startSignIn": "account_start_sign_in",
        "account/cancelSignIn": "account_cancel_sign_in",
        "account/signOut": "account_sign_out",
    }

    def methods(self) -> frozenset[str]:
        return frozenset(self.ROUTES)

    def dispatch(self, method: str, rpc_id_: str, payload: Any) -> dict | None:
        """按路由表派发；未知方法返回 None（载体层映射 404）。

        返回完整 server-response 帧。payload 非 dict 按 bad-request 拒绝；
        方法级 `{args}` 先经路由层边界校验（args.py），越界抛 `BoundaryReject`
        折成 gateway/arguments-invalid 或 gateway/input-invalid。
        """
        handler = self.ROUTES.get(canonical_endpoint(method))
        if handler is None:
            return None
        if not isinstance(payload, dict):
            return self._err(rpc_id_, "gateway/bad-request", "payload must be a JSON object", {})
        try:
            validate_args(canonical_endpoint(method), payload)
            value = getattr(self, handler)(payload)
        except BoundaryReject as error:
            return self._err(rpc_id_, error.code,
                             boundary_error_message(canonical_endpoint(method), error.message),
                             error.details)
        except _Reject as error:
            return self._err(rpc_id_, error.code, error.message, error.details)
        return self._ok(rpc_id_, value)

    # ---------- 装配 ----------

    def _attach(self, session: Session) -> AgentLoop:
        loop = AgentLoop(session, self.adapter, self.tools, self.ctx)
        loop.publish()
        self._agents[session.session_id] = loop
        self.approvals.install(loop)
        self.questions.install(loop)
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
            raise _Reject("session/agent-busy",
                          f'session "{session_id}" is owned by subagent routing',
                          {"reason": "use subagent delivery for this child session"})
        loop = self._agents.get(session_id)
        if loop is None:
            try:
                loop = self._attach(session)
            except Exception as error:  # noqa: BLE001 - 独占 writer 冲突折稳定码
                if error.__class__.__name__ == "SessionAlreadyOwnedError":
                    raise _Reject("session/writer-held", str(error),
                                  {"sessionId": session_id}) from error
                raise
        return session, loop

    @staticmethod
    def _subagent_owned(session: Session) -> bool:
        return session.meta.get("origin") == "subagent"

    def _selection(self) -> dict:
        default = self.ctx.get("agentDefaultModel")
        if default is not None:
            return default.current_selection()
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
            raise _Reject("gateway/bad-request", "address must be a session or subagent address", {})
        if address["kind"] == "session":
            target = _require_id(address, "sessionId")
            session = self.store.get(target)
            if session is None:
                raise _Reject("session/not-found", f'session "{target}" not found',
                              {"sessionId": target})
            # header.cwd === undefined → session/not-found；子代理会话经 session
            # 地址访问 → session/agent-busy（防御对齐：mini 不生产此类场景）
            if session.meta.get("cwd") is None:
                raise _Reject("session/not-found", f'session "{target}" not found',
                              {"sessionId": target})
            if self._subagent_owned(session):
                raise _Reject("session/agent-busy",
                              f'session "{target}" is owned by subagent routing',
                              {"reason": "use subagent delivery for this child session"})
            return target
        parent_session_id = address.get("parentSessionId")
        child_session_id = address.get("childSessionId")
        if not (isinstance(parent_session_id, str) and parent_session_id
                and isinstance(child_session_id, str) and child_session_id):
            raise _Reject("gateway/bad-request",
                          "subagent address must carry parentSessionId and childSessionId", {})
        session = self.store.get(child_session_id)
        if session is None:
            raise _Reject("session/not-found", f'session "{child_session_id}" not found',
                          {"sessionId": child_session_id})
        if self._subagent_owned(session) and session.meta.get("parentSession") == parent_session_id:
            raise _Reject("subagent/catalog-diagnostic", "subagent descriptor is unavailable",
                          {"parentSessionId": parent_session_id,
                           "childSessionId": child_session_id, "reason": "unsupported"})
        raise _Reject("subagent/unauthorized", "subagent does not belong to the supplied parent",
                      {"childSessionId": child_session_id})

    # ---------- pluginInventory/list ----------

    def plugin_inventory(self, payload: dict) -> dict:
        """Loader 条目 + preset 组合行投影（对齐上游 PluginInventoryGateway.list）。

        读 `ctx.pluginInventory` 服务（有则用其 roster），否则以 WebApi.roster 现场投影；
        无 loader 服务时 `entries` 为空、无 roster 时省略 `agentPresets`（同上游在
        `ctx.get('agentPresets') === undefined` 时省略该键）。
        """
        service = self.ctx.get("pluginInventory")
        if service is not None:
            return service.list()
        return build_inventory(self.ctx, self.roster)

    # ---------- pluginManager 域（packages/boot/plugin-manager 的 mini 子集） ----------

    def _plugin_manager(self):
        service = self.ctx.get("pluginManager")
        if service is None:
            raise _Reject("gateway/invocation-unavailable",
                          "plugin manager is not installed", {})
        return service

    def plugin_manager_list(self, payload: dict) -> list[dict]:
        return self._plugin_manager().list_plugins()

    def plugin_manager_set_enabled(self, payload: dict) -> dict:
        return self._plugin_manager().set_plugin_enabled(
            payload["id"], payload["enabled"])

    # ---------- fileUploads（packages/client/file-upload 的 base64 回退） ----------

    def file_uploads_upload(self, payload: dict) -> dict:
        """持久化一个 canonical base64 文件并在接收会话下签收 receipt。"""
        agent_id = _require_id(payload, "agentId")
        request = payload.get("request")
        if not isinstance(request, dict) or not isinstance(request.get("data"), str):
            raise _Reject("gateway/bad-request", "request must carry canonical base64 data", {})
        name = request.get("name")
        if name is not None and not isinstance(name, str):
            raise _Reject("gateway/bad-request", "request.name must be a string", {})
        self._upload_target(agent_id)
        attachments = self.attachments
        if attachments is None:
            raise _Reject("session/attachment-invalid",
                          "file uploads are unavailable: this deployment mounts no "
                          "attachment service",
                          {"reason": "ATTACHMENT_UNAVAILABLE"})
        try:
            ref = attachments.admit_encoded_file(EncodedFileAttachment(
                data=request["data"], name=name))
        except AttachmentError as error:
            raise _Reject("session/attachment-invalid", error.message,
                          {"reason": error.code}) from error
        except Exception as error:  # noqa: BLE001 - 其余落盘失败折 gateway/internal
            raise _Reject("gateway/internal",
                          f"failed to store file upload: {error}", {}) from error
        if self.store.get(agent_id) is None:
            raise _Reject("session/not-found",
                          f'session "{agent_id}" was disposed before its file upload completed',
                          {"sessionId": agent_id})
        return self.file_uploads.stage(agent_id, ref)

    def _upload_target(self, session_id: str) -> None:
        session = self.store.get(session_id)
        if session is None:
            raise _Reject("session/not-found", f'session "{session_id}" not found',
                          {"sessionId": session_id})
        if self._subagent_owned(session):
            raise _Reject("subagent/attachment-invalid",
                          "subagent conversations do not accept file uploads",
                          {"reason": "SUBAGENT_FILE_UNSUPPORTED"})
        if self._agent_for(session_id) is None:
            raise _Reject("session/not-found", f'session "{session_id}" not found',
                          {"sessionId": session_id})

    # ---------- terminal 域（terminal-controller 的 Remote 方法面） ----------
    #
    # 上游 `packages/api/terminal-controller` 的 wire 契约：agent 参数经 typert
    # lookup 以 `agentId` 到达，`list` 取 `sessionId`。mini 的控制器服务由组合层
    # （`install_terminal_controller`）装配；未挂载时如实拒绝（invocation-unavailable）。

    def _terminal_controller(self):
        controller = self.ctx.get("terminalController")
        if controller is None:
            raise _Reject("gateway/invocation-unavailable",
                          "terminal namespace is not mounted in this deployment", {})
        return controller

    def resolve_terminal_agent(self, agent_id: Any) -> AgentLoop:
        """按 agentId 解析 live Agent（冷会话自动 resume，对齐 resolveAgent）。"""
        found = self._agent_for(_require_id({"agentId": agent_id}, "agentId"))
        if found is None:
            raise _Reject("session/not-found", f'session "{agent_id}" not found',
                          {"sessionId": agent_id})
        return found[1]

    def _remote_call(self, call):
        """执行一次域控制器调用并把带稳定 code 的载体错误折成 RPC 错误。"""
        try:
            return call()
        except _Reject:
            raise
        except Exception as error:  # noqa: BLE001 - 域错误闭集 + gateway/internal
            code = getattr(error, "code", None)
            if code in RPC_ERROR_CODES:
                raise _Reject(code, str(error), getattr(error, "details", None) or {}) from error
            raise _Reject("gateway/internal", str(error), {}) from error

    def terminal_environment(self, payload: dict) -> dict:
        agent = self.resolve_terminal_agent(payload.get("agentId"))
        return self._remote_call(lambda: self._terminal_controller().environment(agent))

    def terminal_shells(self, payload: dict) -> list:
        agent = self.resolve_terminal_agent(payload.get("agentId"))
        return self._remote_call(lambda: self._terminal_controller().shells(agent))

    def terminal_list(self, payload: dict) -> list:
        session_id = _require_id(payload, "sessionId")
        return self._remote_call(lambda: self._terminal_controller().list(session_id))

    def terminal_create(self, payload: dict) -> dict:
        agent = self.resolve_terminal_agent(payload.get("agentId"))
        request: dict[str, Any] = {"id": payload.get("id"),
                                   "cols": payload.get("cols"), "rows": payload.get("rows")}
        if payload.get("shellPath") is not None:
            request["shellPath"] = payload["shellPath"]
        return self._remote_call(lambda: self._terminal_controller().create(agent, request))

    def terminal_rename(self, payload: dict) -> None:
        agent = self.resolve_terminal_agent(payload.get("agentId"))
        self._remote_call(lambda: self._terminal_controller().rename(
            agent, payload["id"], payload["title"]))

    def terminal_close(self, payload: dict) -> None:
        agent = self.resolve_terminal_agent(payload.get("agentId"))
        self._remote_call(lambda: self._terminal_controller().close(agent, payload["id"]))

    # ---------- workspace 域（workspace-controller 的 Remote 方法面） ----------

    def _workspace_controller(self):
        controller = self.ctx.get("workspaceController")
        if controller is None:
            raise _Reject("gateway/invocation-unavailable",
                          "workspace namespace is not mounted in this deployment", {})
        return controller

    def workspace_create(self, payload: dict) -> dict:
        return self._remote_call(
            lambda: self._workspace_controller().create({"path": payload.get("path")}))

    def workspace_initialize_default(self, payload: dict) -> dict | None:
        # dsh-v0.2.0-rc.2：入参删除（上游 initializeDefault(signal) 无载荷，固定目录）。
        return self._remote_call(
            lambda: self._workspace_controller().initialize_default())

    def workspace_rename(self, payload: dict) -> dict:
        return self._remote_call(lambda: self._workspace_controller().rename(
            {"workspaceId": payload.get("workspaceId"), "title": payload.get("title")}))

    def workspace_delete(self, payload: dict) -> dict:
        return self._remote_call(lambda: self._workspace_controller().delete(
            {"workspaceId": payload.get("workspaceId")}))

    def workspace_insert_before(self, payload: dict) -> dict:
        return self._remote_call(lambda: self._workspace_controller().insert_before(
            {"workspaceId": payload.get("workspaceId"),
             "beforeWorkspaceId": payload.get("beforeWorkspaceId")}))

    def workspace_insert_session_before(self, payload: dict) -> dict:
        return self._remote_call(lambda: self._workspace_controller().insert_session_before(
            {"workspaceId": payload.get("workspaceId"), "sessionId": payload.get("sessionId"),
             "beforeSessionId": payload.get("beforeSessionId")}))

    def workspace_archive_session(self, payload: dict) -> dict:
        request: dict[str, Any] = {"sessionId": payload.get("sessionId")}
        if payload.get("stopActivity") is not None:
            request["stopActivity"] = payload["stopActivity"]
        return self._remote_call(
            lambda: self._workspace_controller().archive_session(request))

    def workspace_unarchive_session(self, payload: dict) -> dict:
        return self._remote_call(lambda: self._workspace_controller().unarchive_session(
            {"sessionId": payload.get("sessionId")}))

    def workspace_pin_session(self, payload: dict) -> dict:
        return self._remote_call(lambda: self._workspace_controller().pin_session(
            {"sessionId": payload.get("sessionId")}))

    def workspace_unpin_session(self, payload: dict) -> dict:
        return self._remote_call(lambda: self._workspace_controller().unpin_session(
            {"sessionId": payload.get("sessionId")}))

    # ---------- workspace-files 域（workspace-files 的 Remote 方法面） ----------

    def _workspace_files(self):
        controller = self.ctx.get("workspaceFiles")
        if controller is None:
            raise _Reject("gateway/invocation-unavailable",
                          "workspaceFiles namespace is not mounted in this deployment", {})
        return controller

    def workspace_file_scope(self, payload: dict) -> dict:
        """按会话身份派生文件 scope（header cwd，或 sandboxPolicy 回退根）。"""
        session_id = _require_id(payload, "workspaceFileScopeId")
        session = self.store.get(session_id)
        if session is None:
            raise _Reject("gateway/lookup-not-found",
                          f'session "{session_id}" not found for workspace file scope',
                          {"sessionId": session_id})
        root = (getattr(session, "meta", {}) or {}).get("cwd")
        if not root:
            policy = self.ctx.get("sandboxPolicy")
            root = getattr(policy, "workspace_root", None) or self.cwd
        return {"sessionId": session_id, "workspaceRoot": root}

    def workspace_files_read(self, payload: dict) -> dict:
        scope = self.workspace_file_scope(payload)
        range_ = {}
        if payload.get("offset") is not None:
            range_["offset"] = payload["offset"]
        if payload.get("limit") is not None:
            range_["limit"] = payload["limit"]
        return self._remote_call(
            lambda: self._workspace_files().read(scope, payload.get("path"), range_))

    def workspace_files_read_bytes(self, payload: dict) -> dict:
        """`workspaceFiles/readBytes`：原生字节 `options = {range?, baseFile?}`。

        上游 rc.1（index.ts:257-280）把原 `readAll`/`readRelated` 折进本方法：
        `options.baseFile` 提供相对目标基目录，省略 range 时读整文件并在超
        `maxFileBytes` 时 `workspace-file/too-large`。服务值 `data` 是原生 `bytes`
        （上游 `WorkspaceFileBytes.data`），wire 上由 `web/attachments.py` 拆成
        二进制附件，不在这里折 base64。
        """
        scope = self.workspace_file_scope(payload)
        options = payload.get("options") or {}
        return self._remote_call(lambda: self._workspace_files().read_bytes(
            scope, payload.get("path"), dict(options)))

    def workspace_files_stat(self, payload: dict) -> dict:
        scope = self.workspace_file_scope(payload)
        return self._remote_call(
            lambda: self._workspace_files().stat(scope, payload.get("path")))

    def workspace_files_list(self, payload: dict) -> dict:
        scope = self.workspace_file_scope(payload)
        return self._remote_call(
            lambda: self._workspace_files().list(scope, payload.get("path")))

    # ---------- officeToPdf 域（document/office-to-pdf Remote 面） ----------

    def _office_to_pdf(self):
        service = self.ctx.get("officeToPdf")
        if service is None:
            raise _Reject("gateway/invocation-unavailable",
                          "officeToPdf namespace is not mounted in this deployment", {})
        return service

    def office_to_pdf_render(self, payload: dict) -> dict:
        scope = self.workspace_file_scope(payload)
        priority = payload.get("priority") or "foreground"
        workspace_files = self._workspace_files()
        return self._remote_call(
            lambda: self._office_to_pdf().render(
                workspace_files, scope, payload.get("path"), priority))

    def office_to_pdf_get_generation(self, payload: dict) -> dict:
        del payload
        return self._remote_call(lambda: self._office_to_pdf().get_generation())

    # ---------- settings / credentials 域（settings-controller 的 Remote 方法面） ----------

    def _settings_controller(self):
        controller = self.ctx.get("settingsController")
        if controller is None:
            raise _Reject("gateway/invocation-unavailable",
                          "settings namespace is not mounted in this deployment", {})
        return controller

    def _credentials_controller(self):
        controller = self.ctx.get("credentialsController")
        if controller is None:
            raise _Reject("gateway/invocation-unavailable",
                          "credentials namespace is not mounted in this deployment", {})
        return controller

    def settings_describe(self, payload: dict) -> dict:
        return self._remote_call(lambda: self._settings_controller().describe())

    def settings_update(self, payload: dict) -> dict:
        return self._remote_call(lambda: self._settings_controller().update(
            payload.get("ns"), payload.get("patch"), payload.get("expectedRevision")))

    def settings_replace(self, payload: dict) -> dict:
        return self._remote_call(lambda: self._settings_controller().replace(
            payload.get("ns"), payload.get("section"), payload.get("expectedRevision")))

    def settings_mutate(self, payload: dict) -> dict:
        return self._remote_call(lambda: self._settings_controller().mutate(
            payload.get("ns"), payload.get("ops"), payload.get("expectedRevision")))

    def settings_open_document(self, payload: dict) -> dict:
        return self._remote_call(lambda: self._settings_controller().open_settings_document())

    def credentials_describe(self, payload: dict) -> dict:
        return self._remote_call(
            lambda: self._credentials_controller().describe(payload.get("refs")))

    def credentials_set(self, payload: dict) -> None:
        self._remote_call(lambda: self._credentials_controller().set(
            payload.get("ref"), payload.get("value")))

    def credentials_unset(self, payload: dict) -> None:
        self._remote_call(
            lambda: self._credentials_controller().unset(payload.get("ref")))

    # ---------- directoryPicker 域（workspace-controller 的 browse 载体） ----------

    @staticmethod
    def _fully_qualified_directory_path(path: Any) -> bool:
        """绝对且不依赖进程当前盘符的目录路径（上游 `fullyQualified`）。"""
        if not isinstance(path, str) or path == "":
            return False
        if os.name != "nt":
            return os.path.isabs(path)
        import re

        return bool(os.path.isabs(path) and (
            re.match(r"^[A-Za-z]:[\\/]", path)
            or re.match(r"^[\\/]{2}[^\\/]+[\\/]+[^\\/]+", path)))

    def directory_picker_list(self, payload: dict) -> dict:
        """`directoryPicker/list`：一层目录 + 从根到该层的面包屑。"""
        home = os.path.expanduser("~")
        path = payload.get("path")
        path = home if path is None else path
        if not self._fully_qualified_directory_path(path):
            raise _Reject("directory-picker/unreadable",
                          f"directory path must be fully qualified: {path!r}",
                          {"path": path})
        try:
            rows = []
            with os.scandir(path) as entries:
                for entry in entries:
                    try:
                        if not entry.is_dir():
                            continue
                    except OSError:
                        continue
                    rows.append({"name": entry.name, "path": os.path.join(path, entry.name),
                                 "hidden": entry.name.startswith(".")})
        except OSError as error:
            raise _Reject("directory-picker/unreadable",
                          f"directory cannot be listed: {error}", {"path": path})
        rows.sort(key=lambda row: row["name"].casefold())
        truncated = len(rows) > 1000
        rows = rows[:1000]
        crumbs = []
        current = path
        while True:
            parent = os.path.dirname(current)
            name = current if parent == current else os.path.basename(current)
            crumbs.insert(0, {"name": name, "path": current, "hidden": False})
            if parent == current:
                break
            current = parent
        return {"path": path, "home": home, "crumbs": crumbs,
                "entries": rows, "truncated": truncated}

    def directory_picker_create_directory(self, payload: dict) -> str:
        path = payload.get("path")
        name = payload.get("name")
        if (not isinstance(name, str) or name.strip() == "" or name in (".", "..")
                or "/" in name or "\\" in name):
            raise _Reject("gateway/bad-request",
                          "host.createDirectory requires a single non-blank path segment name",
                          {})
        if not self._fully_qualified_directory_path(path):
            raise _Reject("directory-picker/create-failed",
                          f"parent directory must be fully qualified: {path!r}",
                          {"path": path})
        target = os.path.join(path, name)
        try:
            os.mkdir(target)
        except FileExistsError as error:
            raise _Reject("directory-picker/exists",
                          f"directory already exists: {target}", {"path": target}) from error
        except OSError as error:
            raise _Reject("directory-picker/create-failed",
                          f"directory cannot be created: {error}", {"path": target}) from error
        return target

    def directory_picker_pick(self, payload: dict) -> None:
        """mini 组合的是 browse 载体；native chooser 不可用（上游同码）。"""
        raise _Reject("directory-picker/unavailable",
                      'directoryPicker.pick needs the native capability; '
                      'the composed picker serves "browse"',
                      {"capability": "browse"})

    # ---------- llm 域（packages/llm/llm 的 `llm` Remote namespace） ----------

    def _llm_routes(self) -> list[str]:
        """适配器拥有的 provider 路由（pi-ai 多 profile；单路由适配器一条）。"""
        profiles = getattr(self.adapter, "profiles", None)
        if isinstance(profiles, dict) and profiles:
            return list(profiles)
        provider = getattr(self.adapter, "provider", None)
        return [provider] if isinstance(provider, str) and provider else []

    def llm_list_providers(self, payload: dict) -> list:
        return [{"id": route, "name": route} for route in self._llm_routes()]

    def llm_list_configurable_providers(self, payload: dict) -> list:
        """适配器声明的可配置目录；未声明 → 空数组（不臆造 settings 命名空间）。"""
        declared = getattr(self.adapter, "configurable_providers", None)
        if declared is None:
            return []
        rows = declared() if callable(declared) else declared
        return [dict(row) for row in rows]

    def llm_discover_models(self, payload: dict) -> list:
        """`llm/discoverModels`：按 provider 路由取适配器已知目录。

        上游按 settingsNs 查询已注册的 discovery；mini 无 discovery 注册表，
        由注入适配器自己的目录回答（DeepSeek 的 `available_models`），未知
        provider / 仅给 baseURL → `llm/model-discovery-rejected`。
        载体差异：`name`/`inputModalities`/`badge` 由适配器目录派生；无 contextWindow/
        maxTokens 披露时省略。
        """
        request = payload.get("request") or {}
        provider = request.get("provider")
        available = getattr(self.adapter, "available_models", None)
        if (not isinstance(provider, str) or provider == ""
                or provider not in self._llm_routes() or available is None):
            raise _Reject(
                "llm/model-discovery-rejected",
                f'llm: no model discovery for provider {provider!r}',
                {"settingsNs": payload.get("settingsNs"),
                 **({"provider": provider} if isinstance(provider, str) else {})})
        models = self._remote_call(available)
        rows: list[dict] = []
        for info in models:
            if not isinstance(info, dict):
                continue
            row = _wire_model_from_adapter_info(info)
            if row is not None:
                rows.append(row)
        return rows

    # ---------- commands 域（packages/interaction/commands 的 `commands` Remote namespace） ----------

    def _commands(self):
        registry = self.ctx.get("commands")
        if registry is None:
            raise _Reject("gateway/invocation-unavailable",
                          "commands namespace is not mounted in this deployment", {})
        return registry

    def commands_list(self, payload: dict) -> list:
        self.resolve_terminal_agent(payload.get("agentId"))
        return self._remote_call(lambda: self._commands().descriptors())

    def commands_execute(self, payload: dict) -> dict | None:
        agent = self.resolve_terminal_agent(payload.get("agentId"))
        return self._remote_call(lambda: self._commands().execute(
            agent, payload.get("line"), payload.get("submittedAttachments")))

    # ---------- skills 域（session-controller 的 `skills` Remote namespace） ----------

    def skills_list(self, payload: dict) -> dict:
        """`skills/list`：一条会话可见的 user-invocable skill 元数据。

        对齐 `api/session-controller/src/skill-catalog.ts`：按会话当前工作目录
        （working-directory 投影，缺省 header cwd）取 catalog，只回
        `isUserInvocable` 的条目，不加载正文。载体差异：mini 无 cold session
        observation / preset-scoped registry，只服务 store 内会话与全局注册表。
        """
        from ..skills import is_model_invocable, is_user_invocable

        request = payload.get("request") or {}
        session_id = request.get("sessionId")
        if not isinstance(session_id, str) or session_id == "":
            raise _Reject("gateway/arguments-invalid",
                          "request.sessionId must be a non-empty string", {})
        session = self.store.get(session_id)
        if session is None:
            raise _Reject("session/not-found", f'session "{session_id}" not found',
                          {"sessionId": session_id})
        registry = self.ctx.get("skills")
        if registry is None:
            raise _Reject("gateway/invocation-unavailable",
                          "skills namespace is not mounted in this deployment", {})
        working_directory = self.ctx.get("workingDirectory")
        cwd = (working_directory.get(session) if working_directory is not None
               else (session.meta or {}).get("cwd"))
        skills = self._remote_call(lambda: registry.list({"cwd": cwd}))
        return {"skills": [
            {
                "name": skill["name"],
                **({"path": skill["path"]} if skill.get("path") is not None else {}),
                "description": skill["description"],
                **({"whenToUse": skill["whenToUse"]} if skill.get("whenToUse") is not None else {}),
                "modelInvocable": is_model_invocable(skill),
            }
            for skill in skills if is_user_invocable(skill)
        ]}

    # ---------- goal 域（packages/goal/goal 的 `goals` Remote namespace） ----------

    def _goals(self):
        goals = self.ctx.get("goals")
        if goals is None:
            raise _Reject("gateway/invocation-unavailable",
                          "goals namespace is not mounted in this deployment", {})
        return goals

    def goals_get(self, payload: dict) -> dict | None:
        agent = self.resolve_terminal_agent(payload.get("agentId"))
        return self._remote_call(lambda: self._goals().get(agent))

    def goals_create(self, payload: dict) -> dict:
        """`goals/create`：上游 `remoteExportCreate` 只回 `{ref:{id,revision}}`。"""
        agent = self.resolve_terminal_agent(payload.get("agentId"))
        view = self._remote_call(
            lambda: self._goals().create(agent, payload.get("request")))
        return {"ref": {"id": view["id"], "revision": view["revision"]}}

    def goals_edit(self, payload: dict) -> dict:
        agent = self.resolve_terminal_agent(payload.get("agentId"))
        return self._remote_call(lambda: self._goals().edit(
            agent, payload.get("ref"), payload.get("request")))

    def goals_pause(self, payload: dict) -> dict:
        agent = self.resolve_terminal_agent(payload.get("agentId"))
        return self._remote_call(
            lambda: self._goals().pause(agent, payload.get("ref")))

    def goals_resume(self, payload: dict) -> dict:
        agent = self.resolve_terminal_agent(payload.get("agentId"))
        return self._remote_call(
            lambda: self._goals().resume(agent, payload.get("ref")))

    def goals_complete(self, payload: dict) -> dict:
        agent = self.resolve_terminal_agent(payload.get("agentId"))
        return self._remote_call(
            lambda: self._goals().complete(agent, payload.get("ref")))

    def goals_clear(self, payload: dict) -> dict:
        agent = self.resolve_terminal_agent(payload.get("agentId"))
        return self._remote_call(
            lambda: self._goals().clear(agent, payload.get("ref")))

    # ---------- file-reference / session-reference 域（context 组的 Remote 面） ----------

    def _file_references(self):
        service = self.ctx.get("fileReferences")
        if service is None:
            raise _Reject("gateway/invocation-unavailable",
                          "fileReferences namespace is not mounted in this deployment",
                          {})
        return service

    def file_references_list(self, payload: dict) -> list:
        """`fileReferences/list`：一条 Agent 工作目录下的路径候选。

        对齐 `api/session-controller/src/file-references.ts` 的 Remote 适配面——
        wire 参数为 `agentId`（Agent lookup 的 wire 名）+ `query`；返回
        `{path, kind}` 候选（目录带尾 `/`）。
        """
        agent = self.resolve_terminal_agent(payload.get("agentId"))
        return self._remote_call(lambda: self._file_references().list(
            agent, payload.get("query") or ""))

    def _session_reference_resolver(self):
        resolver = self.ctx.get("sessionReferenceResolver")
        if resolver is None:
            raise _Reject("gateway/invocation-unavailable",
                          "sessionReferenceResolver namespace is not mounted in this deployment",
                          {})
        return resolver

    def session_reference_candidates(self, payload: dict) -> list:
        agent = self.resolve_terminal_agent(payload.get("agentId"))
        return self._remote_call(lambda: self._session_reference_resolver().remote_export_candidates(
            agent, payload.get("query")))

    # ---------- job 域（job-controller 的 Remote 方法面） ----------

    def _jobs_registry(self):
        registry = self.ctx.get("jobs")
        if registry is None:
            raise _Reject("gateway/invocation-unavailable",
                          "job namespace is not mounted in this deployment", {})
        return registry

    def job_kill(self, payload: dict) -> dict:
        """`job/kill`：请求取消一个作业；未知/外会话作业折 `job/not-found`。

        对齐上游 job-controller index.ts:109-127：栅栏预检（get）与 kill 在同一
        同步区间；kill 原因固定 `cancelled by the user`；返回 `{outcome}`。
        """
        session_id = _require_id(payload, "sessionId")
        job_id = _require_id(payload, "jobId")
        registry = self._jobs_registry()
        try:
            job = registry.get(job_id, session_id)
        except Exception as error:  # noqa: BLE001 - 未知/外会话/内部一律折 not-found
            raise _Reject("job/not-found", str(error),
                          {"sessionId": session_id, "jobId": job_id}) from error
        try:
            outcome = registry.kill(job_id, session_id, "cancelled by the user")
        except _Reject:
            raise
        except Exception as error:  # noqa: BLE001 - 域错误闭集 + gateway/internal
            code = getattr(error, "code", None)
            if code in RPC_ERROR_CODES:
                raise _Reject(code, str(error),
                              getattr(error, "details", None) or {}) from error
            raise _Reject("gateway/internal", str(error), {}) from error
        # job 已存在才走到这里：job 对象未用，但保留了栅栏语义（上游 get 预检）。
        _ = job
        return {"outcome": outcome}

    # ---------- agentPresets 域（agent-preset-registry 的 Remote 方法面） ----------

    def _agent_preset_roster(self):
        roster = self.roster
        if roster is None:
            raise _Reject("gateway/invocation-unavailable",
                          "agentPresets namespace is not mounted in this deployment", {})
        return roster

    def agent_presets_list(self, payload: dict) -> dict:
        """`agentPresets.list`：当前部署的 preset 选择列表（AgentPresetRoster）。

        对齐上游 agent-preset-registry remoteExportList：`{presets: rows.map(
        ...isDefault)}`。dsh-v0.2.0-rc.2 删除了 `modeSelectionEnabled` 字段
        （roster 形状收窄，changelog 2026-09-30 boot/preset 行）。roster.rows()
        已产 AgentPresetRow 形态（{id, isDefault, name?, description?, broken?}）。
        """
        roster = self._agent_preset_roster()
        return {"presets": roster.rows()}

    def agent_presets_read(self, payload: dict) -> dict:
        """`agentPresets.read`：一条声明行的可读文档（AgentPresetDocument）。

        对齐上游 readDocument：未知 id → `agent-preset/not-found`（带 available）。
        mini 的组合载体为 preset.json/agent.cordis.yml；content 渲染为组合的
        entry-list YAML（有声明行则原样，JSON 载体给 `rows: []`——同
        file_composition 的既有投影语义）。
        """
        roster = self._agent_preset_roster()
        preset_id = payload.get("agentPreset")
        try:
            _, preset = roster.locate(preset_id)
        except Exception as error:
            raise _Reject("agent-preset/not-found", str(error),
                          {"agentPreset": preset_id, "available": roster.ids()}) from error
        document: dict[str, Any] = {"agentPreset": preset.id,
                                    "content": self._preset_document_content(preset)}
        if preset.name:
            document["name"] = preset.name
        if preset.description:
            document["description"] = preset.description
        return document

    @staticmethod
    def _preset_document_content(preset) -> str:
        """一条 preset 的可读组合内容（AgentPresetDocument.content）。

        mini 载体：preset.json 无插件行 → `rows: []`（同 file_composition 的
        JSON 载体投影）；组合形态文件读 entry-list YAML 原样。
        """
        from ..loader.include import load_entry_list_yaml
        from ..web.inventory import _COMPOSITION_FILES
        path = getattr(preset, "path", None)
        if path is not None and path.name in _COMPOSITION_FILES:
            try:
                rows = load_entry_list_yaml(path.read_text(encoding="utf-8"))
                import yaml
                return yaml.safe_dump(rows, sort_keys=False, allow_unicode=True)
            except Exception:  # noqa: BLE001 - 渲染尽力而为，组合损坏给空文档
                return "rows: []\n"
        return "rows: []\n"

    def agent_presets_select(self, payload: dict) -> dict:
        """`agentPresets.select`：会话首回合前选定组合；已开始 → locked。

        对齐上游 select（index.ts:325-341）：turnBoundary 投影检查
        `openTurnStartSeq !== null || lastTurn > 0` → `agent-preset/locked`；
        合法则 resolve（未知 → `agent-preset/not-found`）→ append
        `agent-preset/selected` 落盘 → 返回提交的 preset id。
        """
        session_id = _require_id(payload, "sessionId")
        preset_id = payload.get("agentPreset")
        found = self._agent_for(session_id)
        if found is None:
            raise _Reject("session/not-found", f'session "{session_id}" not found',
                          {"sessionId": session_id})
        session, _loop = found
        registry = self.ctx.get("sessionProjections")
        boundary = registry.state_of(session, "turnBoundary") if registry is not None else None
        if boundary is not None and (
                boundary.get("openTurnStartSeq") is not None or boundary.get("lastTurn", 0) > 0):
            raise _Reject("agent-preset/locked", "This session has already started",
                          {"sessionId": session_id, "agentPreset": preset_id})
        roster = self._agent_preset_roster()
        try:
            _, preset = roster.locate(preset_id)
        except Exception as error:
            raise _Reject("agent-preset/not-found", str(error),
                          {"agentPreset": preset_id, "available": roster.ids()}) from error
        session.append("agent-preset/selected", {"agentPreset": preset.id})
        return preset.id

    # ---------- userQuestions 域（user-questions Remote 方法面，dsh-v0.2.0-rc.2） ----------

    def _user_questions_service(self):
        service = self.ctx.get("userQuestions")
        if service is None:
            raise _Reject("gateway/invocation-unavailable",
                          "userQuestions namespace is not mounted in this deployment", {})
        return service

    def user_questions_answer(self, payload: dict) -> bool:
        """`userQuestions/answer`：回答一个 continued timed 提问（上游 index.ts:165）。"""
        service = self._user_questions_service()
        agent = self.resolve_terminal_agent(payload.get("agentId"))
        return bool(self._remote_call(lambda: service.answer(
            agent, payload.get("callId"), payload.get("answer"))))

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
        loop = self._agents.get(session.session_id)
        attached = loop is not None and loop.session is session
        summary: dict[str, Any] = {
            "sessionId": session.session_id,
            "updatedAt": max(session.created_at, metadata["lastPromptAt"] or 0),
            # 上游 SessionSummary.agentAvailable：该会话当前是否持有 live Agent。
            "agentAvailable": attached,
            "running": running,
            "blank": metadata["blank"],
        }
        if meta.get("parentSession") is not None:
            summary["parentSessionId"] = meta["parentSession"]
        if meta.get("origin") is not None:
            summary["origin"] = meta["origin"]
        if meta.get("cwd") is not None:
            summary["cwd"] = meta["cwd"]
        hints = self._projection_hints(session, attached)
        if hints is not None:
            summary["projections"] = hints
        return summary

    def _projection_hints(self, session: Session, attached: bool) -> dict | None:
        """SessionProjectionHints（上游 list.ts hintsOf）。

        mini 无持久投影缓存 → 恒按 live 注册表快照直读：挂接 Agent 的会话
        `kind:'sequenced'`，其余 `kind:'cached'`（同进程内存会话一律可折）。
        """
        values = projection_values(session, self.ctx.get("usageStats"),
                                   self.ctx.get("sessionProjections"))
        if not values:
            return None
        return {"kind": "sequenced" if attached else "cached",
                "asOfSeq": session.seq - 1, "values": values}

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
        query = query.strip()
        if not query:
            raise _Reject("gateway/bad-request", "session search query must not be empty", {})
        if _utf16_len(query) > 500:
            raise _Reject("gateway/bad-request",
                          "session search query must contain at most 500 UTF-16 code units", {})
        if "\0" in query:
            raise _Reject("gateway/bad-request", "session search query must not contain NUL", {})
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
        agent_preset = payload.get("agentPreset")
        if workspace_id is not None and cwd is not None:
            raise _Reject("gateway/bad-request", "session.create accepts workspaceId or cwd, not both", {})
        session_id = payload.get("sessionId")
        if session_id is not None and not session_id:
            raise _Reject("gateway/bad-request", "sessionId must be a non-empty string", {})
        if cwd is not None and not os.path.isabs(cwd):
            raise _Reject("gateway/bad-request", "cwd must be an absolute path", {})
        if workspace_id is not None:
            raise _Reject("workspace/not-found", f'workspace "{workspace_id}" not found',
                          {"workspaceId": workspace_id})
        cwd = cwd if cwd is not None else self.cwd

        if session_id is not None and self.store.get(session_id) is not None:
            existing = self.store.get(session_id)
            if agent_preset is not None and existing.meta.get("agentPreset") != agent_preset:
                raise _Reject("agent-preset/conflict",
                              f'session "{session_id}" is bound to agent preset '
                              f'"{existing.meta.get("agentPreset")}", not "{agent_preset}"',
                              {"sessionId": session_id, "requestedPreset": agent_preset,
                               "existingPreset": existing.meta.get("agentPreset")})
            existing_cwd = existing.meta.get("cwd")
            if existing_cwd is None:
                raise _Reject("session/conflict",
                              f'session "{session_id}" records no cwd and cannot be adopted '
                              f'for "{cwd}"',
                              {"sessionId": session_id, "requestedCwd": cwd,
                               "existingCwd": existing_cwd})
            if existing_cwd != cwd:
                raise _Reject("session/conflict",
                              f'session "{session_id}" belongs to "{existing_cwd}", not "{cwd}"',
                              {"sessionId": session_id, "requestedCwd": cwd,
                               "existingCwd": existing_cwd})
            return self._create_result(session_id, self._preset_of(existing))
        if session_id is None:
            session_id = f"session-{uuid.uuid4()}"
        try:
            os.makedirs(cwd, exist_ok=True)
        except OSError as error:
            raise _Reject("gateway/internal",
                          f'failed to ensure project directory "{cwd}": {error}', {}) from error
        meta: dict[str, Any] = {"cwd": cwd}
        if agent_preset is not None:
            meta["agentPreset"] = agent_preset
        session = self.store.prepare(session_id, {"meta": meta})
        self._attach(session)
        return self._create_result(session.session_id, self._preset_of(session))

    def create_webhook_session(self, delivery: dict, rule_id: str, request: dict) -> None:
        """webhook 规则命中后建会话并 queue 提示（fire-and-forget 线程 → 常驻循环）。"""

        async def work() -> None:
            self._create_webhook_session_on_resident(delivery, rule_id, request)

        run_on_resident(work())

    def _create_webhook_session_on_resident(
            self, delivery: dict, rule_id: str, request: dict) -> None:
        workspace_path = request.get("workspacePath")
        agent_preset = request.get("agentPreset")
        permission_preset = request.get("permissionPreset")
        title = request.get("title")
        prompt = request.get("prompt")
        if not isinstance(workspace_path, str) or workspace_path == "":
            raise ValueError("webhook session request requires workspacePath")
        if not isinstance(agent_preset, str) or agent_preset == "":
            raise ValueError("webhook session request requires agentPreset")
        if not isinstance(prompt, str) or prompt.strip() == "":
            raise ValueError("webhook session request requires non-empty prompt")
        if not os.path.isabs(workspace_path):
            raise ValueError("webhook session workspacePath must be absolute")
        try:
            os.makedirs(workspace_path, exist_ok=True)
        except OSError as error:
            raise RuntimeError(
                f'failed to ensure project directory "{workspace_path}": {error}') from error
        session_id = f"session-{uuid.uuid4()}"
        meta: dict[str, Any] = {"cwd": workspace_path, "agentPreset": agent_preset}
        if isinstance(permission_preset, str) and permission_preset != "":
            meta["permissionPreset"] = permission_preset
        if isinstance(title, str) and title != "":
            meta["title"] = title
        meta["webhook"] = {
            "ruleId": rule_id,
            "deliveryId": delivery.get("deliveryId"),
            "source": delivery.get("source"),
            "kind": delivery.get("kind"),
        }
        session = self.store.prepare(session_id, {"meta": meta})
        loop = self._attach(session)
        presets = self.ctx.get("permissionPresets")
        if presets is not None and isinstance(permission_preset, str) and permission_preset != "":
            apply = getattr(presets, "set", None)
            if callable(apply):
                apply({"sessionId": session_id, "preset": permission_preset})
        summary = title if isinstance(title, str) and title != "" else "GitHub event received"
        notice = create_message(
            "user",
            [text_block("")],
            {
                "kind": "webhook",
                "provider": delivery.get("kind") or "github",
                "source": delivery.get("source"),
                "deliveryId": delivery.get("deliveryId"),
                "ruleId": rule_id,
                "form": "notice",
                "summary": summary,
            },
        )
        prompt_message = create_message("user", [text_block(prompt)], {"kind": "user"})
        loop.inject(notice)
        loop.followup(prompt_message)

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

    def _models_for_route(self, route: str) -> list[dict]:
        """一个 provider 路由下的目录模型（对齐 catalog.ts group.models）。"""
        available = getattr(self.adapter, "available_models", None)
        if available is not None:
            rows = self._remote_call(available)
            if not isinstance(rows, list):
                return []
            models: list[dict] = []
            for info in rows:
                if not isinstance(info, dict):
                    continue
                prov = info.get("provider", route)
                if prov != route:
                    continue
                row = _wire_model_from_adapter_info(info)
                if row is not None:
                    models.append(row)
            return models
        info = self.adapter.resolve_model_info()
        if info.get("provider") != route:
            return []
        model_id = info.get("model")
        if not isinstance(model_id, str) or model_id == "":
            return []
        return [{"id": model_id, "name": model_id}]

    def _model_groups(self) -> list[dict]:
        groups: list[dict] = []
        for route in self._llm_routes():
            models = self._models_for_route(route)
            if models:
                groups.append({"id": route, "name": route, "models": models})
        return groups

    def _model_available(self, provider: Any, model: Any) -> bool:
        """模型可用性门（上游 catalog.ts modelAvailable）。"""
        if not isinstance(provider, str) or not isinstance(model, str):
            return False
        for group in self._model_groups():
            if group["id"] != provider:
                continue
            return any(entry["id"] == model for entry in group["models"])
        return False

    def select_model(self, payload: dict) -> dict:
        """`session/selectModel`：安装会话级模型选择（上游 commands.ts selectModel）。

        dsh-v0.2.0-rc.2：默认保存改为 fire-and-forget（保存失败不再让 RPC 失败）；
        会话级记录写 `_selections`，部署默认经 `ctx.agentDefaultModel`
        持久化（无 profile entry 时 no-op）。
        先过 `modelAvailable` 门（未列 → `session/model-unavailable`）。
        """
        session_id = _require_id(payload, "sessionId")
        provider = payload.get("provider")
        model = payload.get("model")
        reasoning_effort = payload.get("reasoningEffort")
        found = self._agent_for(session_id)
        if found is None:
            raise _Reject("session/not-found", f'session "{session_id}" not found',
                          {"sessionId": session_id})
        session, _loop = found
        if not self._model_available(provider, model):
            raise _Reject("session/model-unavailable",
                          "Select an available model before sending a message.",
                          {"provider": provider, "model": model})
        resolved: dict[str, Any] = {"provider": provider, "model": model}
        if reasoning_effort is not None:
            resolved["reasoningEffort"] = reasoning_effort
        self._selections[session.session_id] = resolved
        default = self.ctx.get("agentDefaultModel")
        if default is not None:
            try:
                default.save_selection(resolved)
            except Exception as error:  # noqa: BLE001 - fire-and-forget 契约：只告警
                logger = self.ctx.logger
                if logger is not None:
                    logger.warn(
                        "web: model selection changed for the Session but the "
                        f"default was not saved: {error}")
        return {"selected": resolved}

    def model_catalog(self, payload: dict) -> dict:
        """`session/modelCatalog`：可路由 provider 由「有 ≥1 目录模型」派生
        （上游 catalog.ts buildModelCatalog：groups 过滤 models.length > 0，
        routableProviders = groups.map(id)）。"""
        groups = self._model_groups()
        return {"default": self._selection(),
                "routableProviders": [group["id"] for group in groups],
                "groups": groups, "failures": []}

    def _has_provider_api_key(self) -> bool:
        """配置的 API-key 凭据检查（上游 catalog.ts hasProviderApiKey）。

        与服务设置/凭据面同款：部署未挂 `ctx.settings`/`ctx.credentials` 时
        `session/provider-credentials-unavailable`（上游抛出同码）。mini 无
        configurable provider 注册表（单适配器），检查面恒空 → False。
        """
        settings = self.ctx.get("settings")
        credentials = self.ctx.get("credentials")
        if settings is None or credentials is None:
            raise _Reject("session/provider-credentials-unavailable",
                          "provider credentials are unavailable", {})
        return False

    def initialize_default_model(self, payload: dict) -> None:
        """`session/initializeDefaultModel`：登录后无 API key 时选首个可用账户模型
        （上游 session-controller index.ts:287-303）。

        上游要求 `ctx.settings` + `ctx.credentials`（`hasProviderApiKey`）与
        `deepseek-account` provider 的目录模型；mini 单适配器无账户 provider，
        故缺失 provider 模型时折 `session/provider-models-unavailable`
        （`{provider:'deepseek-account'}`，如实标注无账户目录）。
        """
        provider = "deepseek-account"
        if self._has_provider_api_key():
            return
        catalog = self.model_catalog({})
        group = next((item for item in catalog["groups"]
                      if item["id"] == provider), None)
        if group is None or not group["models"]:
            raise _Reject("session/provider-models-unavailable",
                          f'provider "{provider}" has no available models',
                          {"provider": provider})
        model = group["models"][0]
        selection: dict[str, Any] = {"provider": provider, "model": model["id"]}
        default_effort = ((model.get("reasoning") or {}).get("defaultEffort"))
        if default_effort is not None:
            selection["reasoningEffort"] = default_effort
        default = self.ctx.get("agentDefaultModel")
        if default is not None:
            default.save_selection(selection)

    # ---------- workspace 路径 ----------

    def can_open_workspace_path(self, payload: dict) -> bool:
        return False

    def open_workspace_path(self, payload: dict) -> None:
        # 载体简化（登记 verified-diffs）：mini 无原生桌面打开器（native-command
        # 无对应物）→ 校验通过后仍 gateway/internal。路径校验按上游 verifyDesktopPath
        # 的可验证面折叠：非空 + 真实存在的 Host 路径（无 fs processPath 映射层）。
        path = payload.get("path")
        if not isinstance(path, str) or path == "":
            raise _Reject("gateway/bad-request", "A non-empty file path is required", {})
        self._verify_desktop_path(path)
        raise _Reject("gateway/internal",
                      "path open failed: no native desktop opener is available in this deployment",
                      {})

    def workspace_path_applications(self, payload: dict) -> list:
        # 无原生桌面打开器 → canOpenPath 恒 False（上游 workspacePathApplications
        # 在 `!canOpenPath()` 时直接返回空发现面，不校验路径）。
        return []

    @staticmethod
    def _verify_desktop_path(path: str) -> str:
        """上游 verifyDesktopPath 的 mini 折叠：非空 + 存在的绝对 Host 路径。

        `resolve(path)` 后要求路径可由 Host 文件系统验证（mini 无
        `fs.processPathFromHostPath` 映射层，以 realpath 存在性作等价准入）。
        """
        host_path = os.path.realpath(os.path.abspath(path))
        if not os.path.exists(host_path):
            raise _Reject("gateway/bad-request", "Path has no verified Host path", {})
        return host_path

    # ---------- session.rename ----------

    def rename(self, payload: dict) -> None:
        session_id = _require_id(payload, "sessionId")
        if self._agent_for(session_id) is None:
            raise _Reject("session/not-found", f'session "{session_id}" not found',
                          {"sessionId": session_id})
        raise _Reject("gateway/internal",
                      "renaming is unavailable: this deployment mounts no session-title service",
                      {})

    # ---------- session.prompt ----------

    def prompt(self, payload: dict) -> dict:
        request_id = _require_id(payload, "requestId")
        session_id = _require_id(payload, "sessionId")
        mode = payload.get("mode")
        if mode not in ("queue", "steer"):
            raise _Reject("gateway/bad-request", "mode must be one of 'queue', 'steer'", {})
        content = payload.get("content")
        if not content:
            raise _Reject("gateway/bad-request",
                          "content must be a non-empty array of PromptContentPart", {})
        for part in content:
            if not isinstance(part, dict) or part.get("type") not in ("text", "image", "file"):
                raise _Reject("gateway/bad-request",
                              "each content part must be a text, image, or file object", {})
            if part["type"] == "text" and not isinstance(part.get("text"), str):
                raise _Reject("gateway/bad-request", "text part must carry a string text", {})
            if part["type"] == "image" and not (isinstance(part.get("mediaType"), str)
                                                and isinstance(part.get("data"), str)):
                raise _Reject("gateway/bad-request", "image part must carry mediaType and data", {})
            if part["type"] == "file" and not isinstance(part.get("receiptId"), str):
                raise _Reject("gateway/bad-request", "file part must carry a string receiptId", {})
        # 空内容门（上游 commands.ts hasPromptContent：无非空白文本且无非文本
        # part → gateway/bad-request）
        if not any(part["type"] != "text" or part["text"].strip() for part in content):
            raise _Reject("gateway/bad-request",
                          "prompt content must include non-whitespace text or an attachment", {})

        client_time_zone = payload.get("clientTimeZone")
        canonical_time_zone = None
        if client_time_zone is not None:
            canonical_time_zone = canonical_client_time_zone(client_time_zone)
            if canonical_time_zone is None:
                raise _Reject("session/invalid-time-zone",
                              "clientTimeZone must be UTC or a valid IANA Area/Location name",
                              {"value": client_time_zone})

        found = self._agent_for(session_id)
        if found is None:
            raise _Reject("session/not-found", f'session "{session_id}" not found',
                          {"sessionId": session_id})
        loop = found[1]

        refs: list[ImageAttachmentRef] | None = None
        file_refs: dict[str, Any] = {}
        binding = None
        try:
            for part in content:
                if part["type"] != "file":
                    continue
                receipt_id = part["receiptId"]
                if receipt_id in file_refs:
                    continue
                ref = self.file_uploads.resolve(session_id, receipt_id)
                if ref is None:
                    raise _Reject("session/attachment-invalid",
                                  "File was not uploaded for this session.",
                                  {"reason": "FILE_NOT_STAGED"})
                file_refs[receipt_id] = ref
            if file_refs:
                binding = self.file_uploads.bind_prompt(
                    session_id, list(file_refs), request_id)
            if any(part["type"] == "image" for part in content):
                refs = self._admit_prompt_images(content)
            blocks = self._content_blocks(content, refs, file_refs)
            source: dict[str, Any] = {"kind": "user", "rpcId": request_id}
            if canonical_time_zone is not None:
                source["clientTimeZone"] = canonical_time_zone
            message = create_message("user", blocks, source)
            if mode == "steer":
                loop.steer(message)
            else:
                loop.followup(message)
            if binding is not None:
                binding.commit()
        except AttachmentError as error:
            if binding is not None:
                binding.rollback()
            raise _Reject("session/attachment-invalid", error.message,
                          {"reason": error.code}) from error
        except _Reject:
            if binding is not None:
                binding.rollback()
            raise
        except Exception as error:  # noqa: BLE001 - 其余 admit/落盘失败折叠 agent-busy
            if binding is not None:
                binding.rollback()
            raise _Reject("session/agent-busy", "prompt rejected",
                          {"reason": str(error)}) from error
        return {"accepted": True}

    def _admit_prompt_images(self, content: list) -> list[ImageAttachmentRef]:
        info = self.adapter.resolve_model_info()
        modalities = info.get("input_modalities") or ["text"]
        if "image" not in modalities:
            raise _Reject("session/attachment-invalid",
                          f'Model "{info.get("model") or "unknown"}" does not support image input.',
                          {"reason": "MODEL_DOES_NOT_SUPPORT_IMAGES"})
        if self.attachments is None:
            raise _Reject("session/attachment-invalid",
                          "image prompt admission is unavailable: this deployment mounts "
                          "no attachment service",
                          {"reason": "ATTACHMENT_UNAVAILABLE"})
        images = [EncodedImageAttachment(mediaType=part["mediaType"], data=part["data"],
                                         **({} if part.get("name") is None
                                            else {"name": part["name"]}))
                  for part in content if part["type"] == "image"]
        return admit_encoded_images(self.attachments, images)

    @staticmethod
    def _content_blocks(content: list, refs: list[ImageAttachmentRef] | None,
                        file_refs: dict[str, Any]) -> list:
        blocks = []
        index = 0
        for part in content:
            if part["type"] == "image":
                blocks.append(image_block(refs[index].to_dict()))
                index += 1
            elif part["type"] == "file":
                blocks.append(file_block(file_refs[part["receiptId"]].to_dict()))
            else:
                blocks.append(text_block(part["text"]))
        return blocks

    # ---------- session.updateQueue ----------

    def update_queue(self, payload: dict) -> dict:
        session_id = _require_id(payload, "sessionId")
        item_id = _require_id(payload, "itemId")
        action = payload.get("action")
        if action.get("kind") not in ("edit", "remove", "steer"):
            raise _Reject("gateway/bad-request", "action must be a queue edit, remove, or steer action", {})
        if action["kind"] == "edit":
            content = action.get("content")
            if not isinstance(content, list):
                raise _Reject("gateway/bad-request", "edit action must carry a content array", {})
            if any(not (isinstance(part, dict) and part.get("type") == "text") for part in content):
                raise _Reject("session/attachment-invalid", "queue edits accept text content only",
                              {"reason": "QUEUE_EDIT_NON_TEXT"})
            # 空内容门（上游 commands.ts）：队列编辑无非空白文本 → bad-request
            if not any(isinstance(part.get("text"), str) and part["text"].strip()
                       for part in content):
                raise _Reject("gateway/bad-request",
                              "queue edit content must include non-whitespace text", {})

        # 上游 updateQueue：冷会话先 resolveAgent（resume 后才可变更 pending 队列）；
        # 仅 session/not-found 折 queue-item-not-found，其余稳定错误原样抛出。
        found = self._agent_for(session_id)
        if found is None:
            raise _Reject("session/queue-item-not-found", "queued item is no longer pending",
                          {"itemId": item_id})
        _session, agent = found

        target = self._queue_target(agent, item_id)
        if target is None:
            raise _Reject("session/queue-item-not-found", "queued item is no longer pending",
                          {"itemId": item_id})

        kind = action["kind"]
        if kind == "steer" and (target != "next_turn" or agent.status != "running"):
            raise _Reject("session/steer-unavailable", "current turn no longer accepts steering",
                          {"itemId": item_id})

        message = next(m for m in getattr(agent.inbox, target) if m["id"] == item_id)
        if kind == "edit":
            agent.inbox.replace(item_id, {**message,
                                          "content": [text_block(part["text"])
                                                      for part in action["content"]]})
        elif kind == "remove":
            agent.inbox.remove(item_id)
            request_id = (message.get("source") or {}).get("rpcId")
            if isinstance(request_id, str):
                self.file_uploads.retire_prompt(session_id, request_id)
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
            raise _Reject("session/not-found", f'session "{session_id}" not found (not attached)',
                          {"sessionId": session_id})
        if self._subagent_owned(agent.session):
            raise _Reject("session/agent-busy",
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
            raise _Reject("session/not-found", f'session "{session_id}" not found',
                          {"sessionId": session_id})
        ref = self._referenced_image(session.events, attachment_id)
        if ref is None:
            raise _Reject("session/attachment-invalid", "Image is not referenced by this session.",
                          {"reason": "ATTACHMENT_NOT_REFERENCED"})
        if self.attachments is None:
            raise _Reject("session/attachment-invalid",
                          "attachment service is unavailable in this deployment",
                          {"reason": "ATTACHMENT_UNAVAILABLE"})
        try:
            stored = self.attachments.read_image(ref)
        except AttachmentError as error:
            raise _Reject("session/attachment-invalid", error.message,
                          {"reason": error.code}) from error
        except Exception as error:  # noqa: BLE001 - 载体层折叠
            raise _Reject("gateway/internal", "Unable to read image attachment.", {}) from error
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

    # ---------- session.projections ----------

    def projections(self, payload: dict) -> dict | None:
        """一次非激活的会话投影读（上游 index.ts projections）。

        @returns `SessionProjectionsValue` 判别联合：活会话返回
        `{kind:'sequenced', asOfSeq, values}`（宿主 live 注册表直读）；会话缺席返回
        None。部署未挂投影注册表 → `session/projections-unavailable`。
        """
        session_id = payload.get("sessionId")
        if not isinstance(session_id, str) or session_id == "":
            raise _Reject("gateway/bad-request", "sessionId must not be empty", {})
        session = self.store.get(session_id)
        if session is None:
            return None
        registry = self.ctx.get("sessionProjections")
        if registry is None:
            raise _Reject("session/projections-unavailable",
                          "Session projections are unavailable", {})
        registry.snapshot(session)
        return {"kind": "sequenced", "asOfSeq": session.seq - 1,
                "values": projection_values(session, self.ctx.get("usageStats"), registry)}

    # ---------- session.fork ----------

    def fork(self, payload: dict) -> dict:
        """从源会话的精确事件前缀创建子会话（上游 commands.ts fork）。

        `atSeq` 是含端点的精确源事件 seq；省略时取「最近完成 turn 前缀」
        （最后一个 turn/end + 其后的独立事件，直到下一 turn/start、append
        user/message 或 agent/inbox/spliced）。开放切点由 store.fork 的
        build_fork_seed 以 `forked` cause 合成闭包平衡。
        """
        session_id = _require_id(payload, "sessionId")
        at_seq = payload.get("atSeq")
        if at_seq is not None and (not isinstance(at_seq, int)
                                   or isinstance(at_seq, bool) or at_seq < 0):
            raise _Reject("gateway/bad-request", "atSeq must be a non-negative safe integer", {})
        allow_migration = payload.get("allowMigration")
        if allow_migration is not None and not isinstance(allow_migration, bool):
            raise _Reject("gateway/bad-request", "allowMigration must be a boolean", {})
        source = self.store.get(session_id)
        if source is None:
            # allowMigration=false + 迁移预检（上游 index.ts:444-450）：源非 live 且
            # 存储层是迁移件 → 拒绝，要求先打开源会话完成迁移（allowMigration 缺省 true）。
            if allow_migration is False and self._stored_needs_migration(session_id):
                raise _Reject("session/migration-required",
                              "Open the source Session to complete migration before forking",
                              {"sessionId": session_id})
            return self._fork_cold(session_id, at_seq)
        events = list(source.events)
        boundary = self._fork_boundary(session_id, events, at_seq)
        try:
            child = self.store.fork(source, boundary)
        except SessionForkError as error:
            raise _Reject("session/fork-unavailable", str(error),
                          {"sessionId": session_id}) from error
        return {"sessionId": child.session_id}

    def _fork_cold(self, session_id: str, at_seq: int | None) -> dict:
        """冷（非 live）源会话分叉：从持久化读前缀，直接建 live 子会话。

        上游 `commands.fork` 对冷可读前缀同样分叉；mini 的 `SessionStore.fork`
        只接受 live 源，故这里读 `sessionPersistence.inspect` 后以
        `build_fork_seed` 构造与 live 路径相同的 seed/meta，再走 `store.create`。
        """
        from ..core.session.repair import build_fork_seed

        persistence = self.ctx.get("sessionPersistence")
        if persistence is None:
            raise _Reject("session/not-found", f'session "{session_id}" not found',
                          {"sessionId": session_id})
        try:
            inspected = persistence.inspect(session_id)
        except FileNotFoundError as error:
            raise _Reject("session/not-found", f'session "{session_id}" not found',
                          {"sessionId": session_id}) from error
        events = list(inspected.get("events") or [])
        if not events:
            raise _Reject("session/not-found", f'session "{session_id}" not found',
                          {"sessionId": session_id})
        boundary = self._fork_boundary(session_id, events, at_seq)
        meta: dict[str, Any] = {"parentSession": session_id, "isSeeded": True}
        header_meta = inspected.get("meta") or {}
        if isinstance(header_meta.get("cwd"), str):
            meta["cwd"] = header_meta["cwd"]
        try:
            child = self.store.create(None, {
                "seed": build_fork_seed(events, boundary),
                "meta": meta,
                "inheritedEventCount": boundary + 1,
            })
        except SessionForkError as error:
            raise _Reject("session/fork-unavailable", str(error),
                          {"sessionId": session_id}) from error
        return {"sessionId": child.session_id}

    def _fork_boundary(self, session_id: str, events: list, at_seq: int | None) -> int:
        if at_seq is None:
            boundary = self._latest_completed_prefix_boundary(events)
            if boundary is None:
                raise _Reject("session/fork-unavailable",
                              f'session "{session_id}" has no completed turn to fork from',
                              {"sessionId": session_id})
            return boundary
        if at_seq >= len(events) or events[at_seq].get("seq") != at_seq:
            last_seq = events[-1]["seq"] if events else "none"
            raise _Reject(
                "session/fork-unavailable",
                f'event {at_seq} does not exist in session "{session_id}" '
                f"(last seq: {last_seq})",
                {"sessionId": session_id})
        return at_seq

    @staticmethod
    def _latest_completed_prefix_boundary(events) -> int | None:
        """省略 atSeq 的缺省切点：最近完成 turn 前缀（上游 commands.ts）。

        从最后一个 turn/end 起，向后吸收独立事件（step/end、tool/* 等），
        遇到下一 turn/start、append user/message 或 agent/inbox/spliced 停止。
        """
        last_end = None
        for event in events:
            if event.get("type") == "turn/end":
                last_end = event
        if last_end is None:
            return None
        boundary = last_end["seq"]
        for event in events[boundary + 1:]:
            etype = event.get("type")
            if (etype == "turn/start"
                    or (etype == "user/message" and event.get("surfaceOp") == "append")
                    or etype == "agent/inbox/spliced"):
                break
            boundary = event["seq"]
        return boundary

    def _stored_needs_migration(self, session_id: str) -> bool:
        """存储中的会话是否需迁移（上游 `sessionPersistence.stat().formatStatus`）。

        非迁移只读分类；未挂持久化或后端不暴露分类面 → False（视为无需迁移）。
        """
        persistence = self.ctx.get("sessionPersistence")
        if persistence is None:
            return False
        status = persistence.format_status(session_id)
        return status == "migration-required"

    # ---------- session.page ----------

    def events(self, payload: dict) -> dict:
        from .session_passive import events_page
        return events_page(self, payload)

    def state(self, payload: dict) -> dict:
        from .session_passive import read_session_state_resolved
        session_id = payload.get("sessionId")
        if not isinstance(session_id, str) or session_id == "":
            raise _Reject("gateway/bad-request", "sessionId must not be empty", {})
        return read_session_state_resolved(self, session_id)

    def wait(self, payload: dict) -> dict:
        from .session_passive import wait_session
        return wait_session(self, payload)

    def page(self, payload: dict) -> dict:
        from ..session_query.config import SessionQueryError
        from ..session_query.observation import observe_session
        from .history import (
            address_id,
            history_page,
            resolve_history_request,
            validate_observation_address,
        )

        address = payload.get("address")
        if not isinstance(address, dict) or address.get("kind") not in ("session", "subagent"):
            raise _Reject("gateway/bad-request",
                          "address must be a session or subagent address", {})
        spec = resolve_history_request(payload)
        session_id = address_id(address)
        need_projection = address.get("kind") == "subagent"
        try:
            with observe_session(self.ctx, session_id, projection=need_projection) as observation:
                if need_projection:
                    self._attach_observation_projections(observation)
                validate_observation_address(address, observation)
                return history_page(observation, spec, self._paginate)
        except SessionQueryError as error:
            if error.code == "SESSION_QUERY_SESSION_NOT_FOUND":
                if address.get("kind") == "session":
                    target = address.get("sessionId", "")
                    raise _Reject("session/not-found", f'session "{target}" not found',
                                  {"sessionId": target}) from error
                raise _Reject("subagent/not-found", "subagent is unavailable", {
                    "parentSessionId": address.get("parentSessionId"),
                    "childSessionId": address.get("childSessionId"),
                }) from error
            raise

    @staticmethod
    def _validate_history_window(max_messages: Any, turn_window: Any) -> None:
        """maxMessages + turnWindow 窗口校验（上游 history.ts validateHistoryWindow）。"""
        if max_messages is not None and (not _is_safe_int(max_messages) or max_messages <= 0):
            raise _Reject("gateway/bad-request", "maxMessages must be a positive safe integer", {})
        if turn_window is None:
            return
        if not isinstance(turn_window, dict):
            raise _Reject("gateway/bad-request", "turnWindow must be an object", {})
        min_messages = turn_window.get("minMessages")
        min_turns = turn_window.get("minTurns")
        if (not _is_safe_int(min_messages) or min_messages <= 0
                or min_messages > (max_messages or DEFAULT_MAX_MESSAGES)):
            raise _Reject(
                "gateway/bad-request",
                "turnWindow.minMessages must be a positive safe integer no greater than maxMessages",
                {})
        if not _is_safe_int(min_turns) or min_turns <= 0:
            raise _Reject("gateway/bad-request",
                          "turnWindow.minTurns must be a positive safe integer", {})

    @staticmethod
    def _paginate(events, before_seq: int | None, max_messages: int,
                  through_seq: int | None = None,
                  turn_window: dict | None = None) -> tuple[list[dict], bool]:
        """消息边界分页（history.ts paginate 同款）。

        turnWindow 给定且先满足 minMessages + minTurns 时，在 turn/start 处切段。
        """
        if through_seq is None:
            through_seq = events[-1]["seq"] if events else -1
        end = min(through_seq + 1, before_seq) if before_seq is not None else through_seq + 1
        count = 0
        turns = 0
        cut = 0
        for index in range(end - 1, -1, -1):
            event = events[index]
            if turn_window is not None and event.get("type") == "turn/start":
                turns += 1
                if count >= turn_window["minMessages"] and turns >= turn_window["minTurns"]:
                    cut = index
                    break
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
        """records 条目：全部原样事件（上游 history.ts pageRecords = entryFor）。

        V2 废止 chunk 行（assistant/chunk 不复存在，流内嵌 assistant/message），
        ChunkRow 打包层随之移除；日志事件是冻结的 mappingproxy/tuple，
        解冻为普通 dict 供 JSON 序列化。"""
        return [{"type": "event", "event": thaw(event)} for event in events]

    # ---------- 流订阅工厂 ----------

    def follow(self, request: dict) -> _FollowSubscription:
        """session.follow：快照 + 增量（历史 follow 同款）。"""
        return _FollowSubscription(self, request)

    def observe_event(self, request: dict) -> _ObserveEventSubscription:
        return _ObserveEventSubscription(self, request)

    def observe_state(self, request: dict) -> _ObserveStateSubscription:
        return _ObserveStateSubscription(self, request)

    def observe_projections(self, request: dict) -> _ObserveProjectionsSubscription:
        return _ObserveProjectionsSubscription(self, request)

    def control(self) -> _ControlSubscription:
        """session.control：投影基线 + projection 替换帧。"""
        return _ControlSubscription(self)

    # ---------- feedback 域（command-feedback / message-feedback Remote） ----------

    def _session_feedback(self):
        service = self.ctx.get("sessionFeedback")
        if service is None:
            raise _Reject("gateway/invocation-unavailable",
                          "sessionFeedback namespace is not mounted in this deployment", {})
        return service

    def _message_feedback(self):
        service = self.ctx.get("messageFeedback")
        if service is None:
            raise _Reject("gateway/invocation-unavailable",
                          "messageFeedback namespace is not mounted in this deployment", {})
        return service

    def session_feedback_record(self, payload: dict) -> dict:
        """`sessionFeedback/record`：记录一条会话级反馈。"""
        return self._remote_call(lambda: self._session_feedback().record({
            "sessionId": payload.get("sessionId"),
            "text": payload.get("text"),
            "category": payload.get("category"),
        }))

    def message_feedback_list(self, payload: dict) -> dict:
        return self._remote_call(lambda: self._message_feedback().list(
            {"sessionId": payload.get("sessionId")}))

    def message_feedback_put(self, payload: dict) -> dict:
        return self._remote_call(lambda: self._message_feedback().put({
            "sessionId": payload.get("sessionId"),
            "item": payload.get("item"),
            "ifVersion": payload.get("ifVersion"),
        }))

    def message_feedback_delete(self, payload: dict) -> dict:
        return self._remote_call(lambda: self._message_feedback().delete({
            "sessionId": payload.get("sessionId"),
            "messageId": payload.get("messageId"),
            "ifVersion": payload.get("ifVersion"),
        }))

    def _permission_presets(self):
        service = self.ctx.get("permissionPresets")
        if service is None:
            raise _Reject("gateway/invocation-unavailable",
                          "permissionPresets namespace is not mounted in this deployment", {})
        return service

    def permission_presets_catalog(self, payload: dict) -> dict:
        """`permissionPresets/catalog`：进程级可读目录。"""
        return self._remote_call(lambda: self._permission_presets().catalog())

    # ---------- schedule 域（schedule Remote namespace，dsh-v0.2.0-rc.2） ----------
    #
    # 上游 `packages/schedule/schedule` 的 `schedule` 命名空间 Remote 面：
    #   list / catalog / history / delete / update（model 工具 create 不在此面上）。
    # 读取与删除都不激活会话（权威存储 = schedule storage-domain 表）；服务本体
    # 异步，经常驻事件循环同步驱动（同 workspace/workspaceFiles 控制器）。

    def _schedule_service(self):
        service = self.ctx.get("schedule")
        if service is None:
            raise _Reject("gateway/invocation-unavailable",
                          "schedule namespace is not mounted in this deployment", {})
        return service

    def schedule_list(self, payload: dict) -> list:
        service = self._schedule_service()
        return self._remote_call(lambda: run_on_resident(
            service.list({"sessionId": payload.get("sessionId")})))

    def schedule_catalog(self, payload: dict) -> list:
        service = self._schedule_service()
        return self._remote_call(lambda: run_on_resident(service.catalog()))

    def schedule_history(self, payload: dict) -> dict:
        service = self._schedule_service()
        return self._remote_call(lambda: run_on_resident(service.history({
            "sessionId": payload.get("sessionId"), "id": payload.get("id"),
            "limit": payload.get("limit"), "before": payload.get("before")})))

    def schedule_delete(self, payload: dict) -> dict:
        service = self._schedule_service()
        return self._remote_call(lambda: run_on_resident(service.delete({
            "sessionId": payload.get("sessionId"), "id": payload.get("id")})))

    def schedule_update(self, payload: dict) -> dict:
        service = self._schedule_service()
        return self._remote_call(lambda: run_on_resident(service.update({
            "sessionId": payload.get("sessionId"), "id": payload.get("id"),
            "expected": payload.get("expected"), "change": payload.get("change"),
            "title": payload.get("title"), "prompt": payload.get("prompt")})))

    def _deepseek_account(self):
        service = self.ctx.get("deepseekAccount")
        if service is None:
            raise _Reject("gateway/invocation-unavailable",
                          "account namespace is not mounted in this deployment", {})
        return service

    def account_get_state(self, payload: dict) -> dict:
        """`account/getState`：安全账户投影（无凭据；无 AccountClientMetadata 入参）。"""
        return self._remote_call(lambda: self._deepseek_account().get_state())

    def account_get_profile(self, payload: dict):
        """`account/getProfile`：Platform 档案（未登录/无 grant → null）。

        dsh-v0.2.0-rc.2：RPC 增 `AccountClientMetadata{version,locale,
        timezoneOffsetSeconds}` 入参（Host 据此派生 Platform 请求头）；mini 落空
        实现无 Platform 载体，metadata 被接受但未消费（触发条件登记）。
        """
        client = self._account_client_metadata(payload)
        return self._remote_call(lambda: self._deepseek_account().get_profile(client))

    def account_get_balance(self, payload: dict):
        """`account/getBalance`：Platform 余额（未登录/无 grant → null）。"""
        client = self._account_client_metadata(payload)
        return self._remote_call(lambda: self._deepseek_account().get_balance(client))

    def account_get_unnotified_bonuses(self, payload: dict):
        """`account/getUnnotifiedBonuses`：未展示的已发放 bonus（无 grant → null）。

        dsh-v0.2.0-rc.2 新 RPC。mini 落空实现无 Platform/bonus 数据 → 无 grant 时
        诚实地返回 null（上游同码：accountId + bonuses 批次或 null）。
        """
        client = self._account_client_metadata(payload)
        service = self._deepseek_account()
        method = getattr(service, "get_unnotified_bonuses", None)
        if method is None:
            return None
        return self._remote_call(lambda: method(client))

    def account_ack_bonus_notified(self, payload: dict) -> bool:
        """`account/ackBonusNotified`：记录一条 bonus 已展示（无账户 → false）。"""
        client = self._account_client_metadata(payload)
        service = self._deepseek_account()
        method = getattr(service, "ack_bonus_notified", None)
        if method is None:
            return False
        return bool(self._remote_call(
            lambda: method(payload.get("accountId"), payload.get("orderId"), client)))

    def account_has_running_account_tasks(self, payload: dict) -> bool:
        """`account/hasRunningAccountTasks`：是否有 running 任务在账户路由上。

        对齐上游 `account-tasks.ts` `isRunningAccountTask`：status=='running'
        且最新 request/context provider 为 `deepseek-account`。mini 以 WebApi
        自有的 agent 集合 + `ctx.agents`（若装配）为消费面。
        """
        from ..deepseek_account import is_running_account_task

        registry = self.ctx.get("agents")
        agents = registry.list() if registry is not None else list(self._agents.values())
        return any(is_running_account_task(agent) for agent in agents)

    def account_start_sign_in(self, payload: dict) -> dict:
        """`account/startSignIn`：发起或复用浏览器 PKCE 登录尝试。"""
        client = self._account_client_metadata(payload)
        login_source = payload.get("loginSource")
        if login_source not in ("web", "desktop"):
            raise _Reject("gateway/bad-request",
                          "loginSource must be 'web' or 'desktop'", {})
        callback_origin = payload.get("callbackOrigin")
        if not isinstance(callback_origin, str):
            raise _Reject("gateway/bad-request",
                          "callbackOrigin must be a string", {})
        return self._remote_call(lambda: self._deepseek_account().start_sign_in(
            client, callback_origin, login_source))

    def account_cancel_sign_in(self, payload: dict) -> dict:
        """`account/cancelSignIn`：取消指定的本地登录尝试。"""
        attempt_id = payload.get("attemptId")
        if not isinstance(attempt_id, str):
            raise _Reject("gateway/bad-request",
                          "attemptId must be a string", {})
        return self._remote_call(lambda: self._deepseek_account().cancel_sign_in(
            attempt_id))

    def account_sign_out(self, payload: dict) -> dict:
        """`account/signOut`：移除本地 grant 并在后台向 Platform 撤销。"""
        client = self._account_client_metadata(payload)
        return self._remote_call(
            lambda: self._deepseek_account().sign_out(client))

    @staticmethod
    def _account_client_metadata(payload: dict) -> dict:
        """校验 `AccountClientMetadata`（dsh-v0.2.0-rc.2 全账户 RPC 入参）。

        上游类型 `{version:string, locale:string, timezoneOffsetSeconds:number}`；
        边界层已校验顶层类型，这里校验子字段形状并以业务码拒绝（值域不进路由层）。
        """
        client = payload.get("client")
        if (not isinstance(client, dict)
                or not isinstance(client.get("version"), str)
                or not isinstance(client.get("locale"), str)
                or isinstance(client.get("timezoneOffsetSeconds"), bool)
                or not isinstance(client.get("timezoneOffsetSeconds"), int)):
            raise _Reject(
                "gateway/bad-request",
                "client metadata must carry version, locale, and timezoneOffsetSeconds", {})
        return client

    def _attach_observation_projections(self, observation) -> None:
        registry = self.ctx.get("sessionProjections")
        if registry is None:
            return
        live = observation._live_session
        if live is not None:
            registry.snapshot(live)
            observation.projections = {
                "asOfSeq": live.seq - 1,
                "values": projection_values(live, self.ctx.get("usageStats"), registry),
            }
            return

        class _PreparedSession:
            def __init__(self, session_id, meta, events, inherited_event_count):
                self.session_id = session_id
                self.meta = meta
                self.seq = (events[-1]["seq"] + 1) if events else 0
                self.events = events
                self.inherited_event_count = inherited_event_count

            def snapshot_events(self, from_=None, to_=None):
                items = list(self.events)
                if from_ is not None:
                    items = [event for event in items if event["seq"] >= from_]
                if to_ is not None:
                    items = [event for event in items if event["seq"] < to_]
                return tuple(items)

        stub = _PreparedSession(
            observation.header["id"],
            observation.header,
            observation.events,
            observation.inherited_event_count,
        )
        registry.snapshot(stub)
        observation.projections = {
            "asOfSeq": stub.seq - 1,
            "values": projection_values(stub, self.ctx.get("usageStats"), registry),
        }

    # ---------- wire 辅助 ----------

    @staticmethod
    def _wire_header(session: Session) -> dict:
        """SessionWireHeader（上游 history.ts wireHeader：{...header} 平铺直转）。

        alpha.1：isSeeded 布尔回归 wire（rc.1 的 seedLength 翻译废止）；
        inheritedEventCount 不上 wire（仅持久化 header 携带）。
        """
        meta = session.meta
        header: dict[str, Any] = {
            "version": SESSION_FORMAT_VERSION,
            "id": session.session_id,
            "createdAt": session.created_at,
            "isSeeded": session.is_seeded,
        }
        if meta.get("cwd") is not None:
            header["cwd"] = meta["cwd"]
        if meta.get("parentSession") is not None:
            header["parentSession"] = meta["parentSession"]
        if meta.get("origin") is not None:
            header["origin"] = meta["origin"]
        if meta.get("delegationDepth") is not None:
            header["delegationDepth"] = meta["delegationDepth"]
        if meta.get("agentPreset") is not None:
            header["agentPreset"] = meta["agentPreset"]
        return header