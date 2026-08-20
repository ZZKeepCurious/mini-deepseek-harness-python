"""第 7 章：ACP 最小子集 —— 自动化专用 Agent Client Protocol 服务。

对应 dsh 真实源码：packages/acp/acp（apply + codec.ts + content.ts）。

上游语义（已核实，index.ts + codec.ts + content.ts）：
  * 自动化专用：只承载 prompt 文本/图片、已提交的 assistant 文本/图片、
    取消、一次性权限决策；呈现与人机交互留在 web 表面。
  * initialize → {protocolVersion, agentInfo:{name:'deepseek-harness-acp',
    version}, agentCapabilities:{promptCapabilities:{image: <动态>,
    audio:false, embeddedContext:false}}, authMethods:[]}——image 能力由
    supportsAcpImagePrompts 如实判定（attachment 服务可用且模型声明图片输入）。
  * newSession：cwd 必须绝对路径（否则 invalid params）、additionalDirectories
    非空拒绝、mcpServers 非空拒绝；mint sessionId。
  * prompt：session 必须存在；已有 inflight 拒绝（"a prompt is already in
    flight for this session"）；image 块经 admitAcpPrompt 受理（mime 白名单 +
    canonical base64 + attachment 存储），audio/resource 拒绝；空 prompt 拒绝；
    等待 whole-agent idle 结算：turnless → 'cancelled'，max-tokens → 'end_turn'
    （README：非终局不是 prompt 级 stop reason），其余 turn/end 映射 stopReason；
    turn/end kind='error' → 立即以 "turn failed" 拒绝。
  * 会话更新只发已提交的 assistant text/image（image 块经 readImage 读回
    base64 内联），chunk/reasoning/tools/plan 不上线。
  * cancel：未知 session no-op；否则取消 agent 并结算 'cancelled'。
  * 审批桥：仅当 callId 存在时提供二选一（allow-once → 'allowed-once'，
    reject-once → 'rejected'，cancelled → 'cancelled'）；callId 缺失 → next()。
  * 错误码：invalid params / internal error（JSON-RPC -32602 / -32603）。

载体简化：上游 async（whenIdle 等待 + stream 通知）；mini 同步——prompt
直接跑完整回合后返回 stopReason，stream 通知以记录列表承载；attachment
存储为内存/本地实现（见 miniharness/attachment）；模型图片能力经
LlmAdapter.resolve_model_info 承载（上游为 llm 服务 resolveModelInfo）。
"""
from __future__ import annotations

import base64
import os
import uuid
from typing import Any, Callable

from ..attachment import (
    INVALID_IMAGE_BASE64,
    AttachmentError,
    AttachmentStore,
    ImageAttachmentRef,
    SaveImageAttachment,
    image_media_type,
    is_image_admission_error,
)
from ..core.scope import Context
from ..llm import FakeLlmAdapter
from ..llm.retry import apply_retry_planner
from ..compaction import install_compaction
from ..jobs import install_jobs, register_job_tools
from ..skills import install_skills, register_skill_tools
from ..core.system_prompt import install_system_prompt
from ..core.agent_loop.agent import AgentLoop
from ..core.session import Session, create_message, image_block, text_block
from ..core.tools import ToolRegistry

# ACP 与核心词汇共享的光栅格式（上游 content.ts IMAGE_MEDIA_TYPES）
_IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")

# 规范 RFC 4648 base64（无空白、无 URL-safe 别名；上游 CANONICAL_BASE64）
_CANONICAL_BASE64 = __import__("re").compile(
    r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$"
)

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


# ---------- 富媒体内容受理与投影（acp/src/content.ts） ----------

class AcpContentError(Exception):
    """内容受理失败：invalid（-32602 参数错误）或 internal（-32603 内部失败）。

    对齐上游 AcpContentError {kind: 'invalid'|'internal'}（content.ts）。
    """

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.message = message
        self.kind = kind


def supports_acp_image_prompts(attachment, adapter) -> bool:
    """是否可在 initialize 如实宣称内联图片 prompt（上游 supportsAcpImagePrompts）。

    三项缺一即 false：attachment 服务可用、其媒体类型含光栅白名单、
    模型声明图片输入（resolve_model_info.input_modalities 含 'image'）。
    """
    if attachment is None or adapter is None:
        return False
    if not any(mt in _IMAGE_MEDIA_TYPES for mt in attachment.image_limits.mediaTypes):
        return False
    try:
        info = adapter.resolve_model_info()
        return "image" in info.get("input_modalities", [])
    except Exception:
        return False


def _decode_image_block(block: dict) -> SaveImageAttachment:
    """严格解码一个 ACP 内联图片（上游 decodeImage）：mime 白名单 + canonical base64。"""
    media_type = block.get("mimeType")
    if media_type not in _IMAGE_MEDIA_TYPES:
        raise AcpContentError(
            "image mimeType must be image/png, image/jpeg, image/webp, or image/gif",
            "invalid",
        )
    data = block.get("data", "")
    if not isinstance(data, str) or not _CANONICAL_BASE64.match(data):
        raise AcpContentError("image data must be canonical base64", "invalid")
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception:
        raise AcpContentError("image data must be canonical base64", "invalid")
    if base64.b64encode(raw).decode("ascii") != data:
        raise AcpContentError("image data must be canonical base64", "invalid")
    return SaveImageAttachment(data=raw, mediaType=media_type)


def admit_acp_prompt(attachment, prompt: list | tuple, image_enabled: bool) -> list[dict]:
    """把 ACP prompt 受理为有序持久核心内容（上游 admitAcpPrompt）。

    校验通过后才开始存储写入（上游：批次内图片全部验证通过才启动写入）；
    返回 wire 顺序的核心 ContentBlock[]（text / resource_link 保持顺序、
    image 转 {type:'image', attachment: ref}）。
    """
    images: list[SaveImageAttachment] = []
    for block in prompt or []:
        btype = block.get("type")
        if btype == "image":
            if not image_enabled:
                raise AcpContentError(
                    "inline image prompts were not advertised by this connection",
                    "invalid",
                )
            images.append(_decode_image_block(block))
        elif btype in ("text", "resource_link"):
            continue
        elif btype == "audio":
            raise AcpContentError("audio prompt content is not supported", "invalid")
        elif btype == "resource":
            raise AcpContentError(
                "embedded resource prompt content is not supported", "invalid"
            )
        else:
            raise AcpContentError("unsupported ACP prompt content", "invalid")

    refs: list[dict] = []
    if images:
        try:
            refs = attachment.save_images(images)
        except AttachmentError as e:
            if is_image_admission_error(e):
                raise AcpContentError(e.message, "invalid")
            raise AcpContentError("unable to persist the prompt image batch", "internal")

    content: list[dict] = []
    pending_text = ""
    image_index = 0

    def flush_text() -> None:
        nonlocal pending_text
        if pending_text:
            content.append(text_block(pending_text))
            pending_text = ""

    for block in prompt or []:
        btype = block.get("type")
        if btype == "text":
            pending_text += block.get("text", "")
        elif btype == "resource_link":
            pending_text += acp_prompt_to_text([block])
        elif btype == "image":
            flush_text()
            ref = refs[image_index]
            image_index += 1
            content.append(image_block(ref.to_dict() if hasattr(ref, "to_dict") else ref))
    flush_text()
    if not any(
        b.get("type") == "image"
        or (b.get("type") == "text" and b.get("text", "").strip())
        for b in content
    ):
        raise AcpContentError("empty prompt", "invalid")
    return content


def assistant_block_to_acp(attachment, block: dict) -> dict | None:
    """已提交 assistant 块 → ACP wire 内容（上游 assistantBlockToAcp）。

    text 空块跳过；image 块从 attachment 读回 + 完整性校验后 base64 内联；
    其余核心块不上自动化线。
    """
    if block.get("type") == "text":
        text = block.get("text", "")
        return {"type": "text", "text": text} if text else None
    if block.get("type") != "image":
        return None
    try:
        stored = attachment.read_image(
            _ref_from_dict(block.get("attachment", {}))
        )
    except AttachmentError as e:
        raise AcpContentError(
            "cannot deliver assistant image: the attachment is unavailable or corrupt",
            "internal",
        )
    return {
        "type": "image",
        "data": base64.b64encode(stored.data).decode("ascii"),
        "mimeType": stored.ref.mediaType,
    }


def _ref_from_dict(d: dict) -> Any:
    """把日志里的 image.attachment（ref.to_dict 形状）还原为 ImageAttachmentRef。"""
    from ..attachment import AttachmentId, ImageAttachmentRef

    return ImageAttachmentRef(
        attachmentId=AttachmentId(d["attachmentId"]),
        mediaType=d["mediaType"],
        bytes=d["bytes"],
        width=d["width"],
        height=d["height"],
        **({"name": d["name"]} if "name" in d else {}),
    )


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
                 model: str = "fake-model", attachment: Any = None):
        self._adapter = adapter or FakeLlmAdapter()
        self.provider = provider
        self.model = model
        self._attachment = attachment   # AttachmentStore | None（无则宣称 image:false）
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
                "promptCapabilities": {
                    "image": supports_acp_image_prompts(self._attachment, self._adapter),
                    "audio": False,
                    "embeddedContext": False,
                },
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
        apply_retry_planner(ctx)
        install_compaction(ctx)
        install_jobs(ctx)
        install_skills(ctx)
        install_system_prompt(ctx)
        reg = ToolRegistry(ctx)
        register_job_tools(reg, ctx.get("jobs"))
        register_skill_tools(reg, ctx.get("skills"))
        return AgentLoop(Session(session_id), self._adapter, reg, ctx)

    # ---------- prompt ----------

    def prompt(self, session_id: str, prompt: list | tuple) -> dict:
        self._assert_open()
        record = self._sessions.get(session_id)
        if record is None:
            raise invalid_params(f"unknown session: {session_id}")
        if record.get("inflight"):
            raise invalid_params("a prompt is already in flight for this session")
        image_enabled = supports_acp_image_prompts(self._attachment, self._adapter)
        try:
            content = admit_acp_prompt(self._attachment, prompt, image_enabled)
        except AcpContentError as e:
            if e.kind == "invalid":
                raise invalid_params(e.message)
            raise internal_error(e.message)
        if not any(b.get("type") == "image" for b in content) and (
            not content or not content[-1].get("text", "").strip()
        ):
            # 纯文本且无文本内容 → 空 prompt（图片已含在 admit 内校验）
            raise invalid_params("empty prompt")
        record["inflight"] = True
        try:
            message = create_message("user", content, {"kind": "user"})
            record["loop"].followup(message)
            self._emit_assistant_output(record["loop"])
        finally:
            record["inflight"] = False
        reason = self._last_turn_end(record["loop"])
        if reason is None:
            # turnless：无回合结束记录 → 'cancelled'（上游 index.ts:331 同语义）
            return {"stopReason": "cancelled"}
        if reason.get("kind") == "error":
            # 对齐上游：error turn 立即以 internalError 拒绝（turn failed: <message>）
            error = reason.get("error") or {}
            raise internal_error(f"turn failed: {error.get('message', 'unknown')}")
        if reason.get("kind") == "max-tokens":
            # 对齐上游 index.ts:326：max-tokens 非终局，映射到 end_turn
            return {"stopReason": "end_turn"}
        return {"stopReason": turn_end_to_stop_reason(reason)}

    def _last_turn_end(self, loop) -> dict | None:
        for event in reversed(loop.session.events):
            if event["type"] == "turn/end":
                return event["data"].get("reason")
        return None

    def _emit_assistant_output(self, loop) -> None:
        """把回合内已提交的 assistant/message 投影为 ACP 更新流（简化载体）。

        对齐上游 index.ts:222-252：只在 inflight turn 内、逐 block 发
        `agent_message_chunk`（上游 session/event 监听按 `event.data.turn ===
        inflight.turn` 过滤；每个 block 一条 update，content 为单块）。
        chunk/reasoning/tools/plan 不上线；image 块经 readImage 读回 base64
        内联（assistantBlockToAcp）。mini 仍以 updates 列表承载（无真实事件
        总线，同步回合结束一次性投影，标注简化）。
        """
        session_id = loop.session.session_id
        inflight_turn = None
        for event in reversed(loop.session.events):
            if event["type"] == "turn/end":
                inflight_turn = event["data"].get("turn")
                break
        for event in loop.session.events:
            if event["type"] != "assistant/message":
                continue
            if event["data"].get("turn") != inflight_turn:
                continue
            blocks = (event["data"].get("message") or {}).get("content", [])
            for block in blocks:
                content = assistant_block_to_acp(self._attachment, block)
                if content is None:
                    continue
                self.updates.append({
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": content,
                    },
                })

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
        for record in self._sessions.values():
            try:
                record["loop"].dispose()
            except Exception:
                pass
        self._closed = True
        self._sessions.clear()

    def _assert_open(self) -> None:
        if self._closed:
            raise internal_error("the ACP bridge has been disposed")

    @property
    def sessions(self) -> dict:
        return self._sessions