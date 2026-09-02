"""第 7 章：ACP 会话生命周期 —— 自动化专用 Agent Client Protocol 服务。

对应 dsh 真实源码：packages/acp/acp（apply/index.ts + codec.ts + content.ts +
session.ts + model-control.ts + updates.ts）。

上游语义（已核实，index.ts + codec.ts + content.ts + session.ts +
model-control.ts + updates.ts）：
  * 自动化专用：只承载 prompt 文本/图片、已提交的 assistant 文本/图片、
    取消、一次性权限决策、标准模型配置；呈现与人机交互留在 web 表面。
  * initialize → {protocolVersion, agentInfo:{name:'deepseek-harness-acp',
    version}, agentCapabilities:{promptCapabilities:{image: <动态>,
    audio:false, embeddedContext:false}, sessionCapabilities:{close:{},
    list:{}, resume:{}}}, authMethods:[]}——image 能力由 supportsAcpImagePrompts
    如实判定（attachment 服务可用且模型声明图片输入）；mcpCapabilities 不宣称
    （mini 无 MCP，诚实标注）。
  * newSession／resumeSession 共用校验：cwd 必须绝对路径（否则 invalid params）、
    additionalDirectories 非空拒绝、mcpServers 非空拒绝（mini 无 MCP）；
    newSession mint sessionId 并返回 {sessionId, configOptions}。
  * resumeSession：依次拒绝 活跃（"session is already active"）、不可恢复
    （"session is not resumable"）、cwd 不符（"session cwd does not match"）；
    恢复的路由取日志最近一次 request/header 的已提交配置（selectionFor，
    缺失时回退部署配置）。
  * listSessions：只列已关闭/可恢复的会话（上游排除活跃与 subagent），
    createdAt 降序 + sessionId UTF-8 字节升序；keyset 游标 = base64url
    (JSON [createdAt, sessionId])，非规范/畸形一律 invalid params
    "session/list cursor is invalid"；页大小默认 100（构造参数
    session_list_page_size，非法即抛错 fail loud）。
  * setSessionConfigOption：model 或 reasoning_effort 标准选项，错误文案逐字
    对齐 AcpModelConfigError（"requires a select value" / "no model selection" /
    "unknown model option" / "unknown reasoning effort for ..."）；切换 model
    同时把 reasoning 复位到 provider 默认（上游 set 同款）。返回完整选项状态。
  * closeSession：未知 session invalid params；关闭失败 internal error
    "session close failed: <e>"；finally 必接解除活动登记（对齐上游）。
  * prompt：session 必须存在；已有 inflight 拒绝（"a prompt is already in
    flight for this session"）；image 块经 admitAcpPrompt 受理（mime 白名单 +
    canonical base64 + attachment 存储），audio/resource 拒绝；空 prompt 拒绝；
    等待 whole-agent idle 结算：turnless → 'cancelled'，max-tokens → 'end_turn'
    （非终局不是 prompt 级 stop reason），其余 turn/end 映射 stopReason；
    turn/end kind='error' → 立即以 "turn failed" 拒绝。受理时 snapshot 模型
    选择并 pin 整个同步回合（上游 pinTurn/releaseTurn 的同步载体），保证
    同一回合内选择不变。
  * 会话更新（updates.ts）逐已提交事件投影：assistant/message 仅 inflight
    turn 内的块可见（upper 同款过滤），逐 block——reasoning 非空 →
    agent_thought_chunk（{messageId, content:{type:'text',text}}），其余经
    assistantBlockToAcp → agent_message_chunk（{messageId, content}，每个
    block 一条 update）；tool/call → tool_call（in_progress + 保留畸形 JSON），
    tool/result → tool_call_update（isError → 'failed' 否则 'completed'）。
  * cancel：未知 session no-op；否则取消 agent 并结算 'cancelled'。
  * 审批桥：仅当 callId 存在时提供二选一（allow-once → 'allowed-once'，
    reject-once → 'rejected'，cancelled → 'cancelled'）；callId 缺失 → next()。
  * 错误码：invalid params / internal error（JSON-RPC -32602 / -32603）。

载体简化：上游 async（whenIdle 等待 + stream 通知 + 磁盘持久化）；mini 同步
——prompt 直接跑完整回合后返回 stopReason，session/update 通知按 updates 记录逐条
排发（无真实事件总线，回合结束后批量一次性投影，非上游并发流式）；close 归档在
内存（无磁盘持久化目录，恢复复用同一 Session 对象与装配 ctx、冷重建 loop；createdAt 取
Session.created_at 的进程内时间戳）；模型选择为单一 adapter 路由——provider/
model 多选目录经可选的 adapter.models_catalog 教学扩展承载（上游为 llm 服务
listProviders/listModels），reasoning 目录经可选的
adapter.resolve_model_info()['reasoning'] 承载（内置适配器不声明 → 该选项
正常缺席）。**选择施加是信封级**：上游 installModelSelection 作用于路由选择
（换 provider/model 即换某个真实连接），mini 的 agent/request waterfall 覆写
等价一致——但 mini 的 adapter 实例即路由，同一 prompt 实际流经的仍是该单一
实例，config 里的 provider/model/reasoningEffort 只写入 request 信封
（request/header、request/context 日志）成为「模型可见」事实，执行同源。如实
标注：这是单适配器载体的教学边界，wire 层换路由不在 mini 范围内；
同步单飞下 pin_turn/release_turn
无 turn 编号匹配（单一提示符串行回合语义等价）。双平台：会话事件流的
request/header 数据形状为 {header:{config, adapterDefaults?}, reason}。
"""
from __future__ import annotations

import base64
import json
import os
import re
import uuid
from typing import Any, Callable

from ..attachment import (
    INVALID_IMAGE_BASE64,
    AttachmentError,
    AttachmentStore,
    ImageAttachmentRef,
    SaveImageAttachment,
    is_image_admission_error,
)
from ..core.scope import Context
from ..llm import FakeLlmAdapter
from ..llm.retry import apply_retry_planner
from ..compaction import (
    TokenMeter,
    install_compaction,
    install_tool_result_pruner,
)
from ..jobs import install_jobs, register_job_tools
from ..skills import install_skills, register_skill_tools
from ..core.system_prompt import install_system_prompt
from ..core.agent_loop.agent import AgentLoop
from ..core.session import Session, create_message, image_block, text_block
from ..core.session_store import install_sessions
from ..core.agents import install_agents
from ..core.tools import ToolRegistry

__all__ = [
    "AcpContentError",
    "AcpModelConfigError",
    "AcpModelControl",
    "AcpRequestError",
    "AcpServer",
    "acp_prompt_to_text",
    "admit_acp_prompt",
    "assistant_block_to_acp",
    "internal_error",
    "invalid_params",
    "prompt_has_unsupported_content",
    "selection_for",
    "supports_acp_image_prompts",
    "turn_end_to_stop_reason",
]

# ACP 与核心词汇共享的光栅格式（上游 content.ts IMAGE_MEDIA_TYPES）
_IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")

# 规范 RFC 4648 base64（无空白、无 URL-safe 别名；上游 CANONICAL_BASE64）
_CANONICAL_BASE64 = re.compile(
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


# ---------- 会话模型配置（acp/src/model-control.ts） ----------

_MODEL_CONFIG_ID = "model"
_REASONING_CONFIG_ID = "reasoning_effort"
# 上游 DSH reasoning effort id 恒非空，空串是互斥的 provider-default 选择
_PROVIDER_DEFAULT_REASONING_VALUE = ""
_DEFAULT_SESSION_LIST_PAGE_SIZE = 100


class AcpModelConfigError(Exception):
    """模型配置错误：setSessionConfigOption 的合法拒绝（上游同名类）。

    由 setSessionConfigOption 映射为 invalid params；消息逐字对齐上游。
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _model_value(provider: str, model: str) -> str:
    """不透明 ACP 选项值 = JSON 数组 [provider, model]（上游 modelValue）。

    紧凑序列化（无空格、不转义非 ASCII），与 JSON.stringify 字节一致。
    """
    return json.dumps([provider, model], ensure_ascii=False, separators=(",", ":"))


def selection_for(logged: dict | None,
                  fallback: dict | None) -> dict | None:
    """恢复最近一次已提交路由，缺失时回退部署配置（session.ts selectionFor）。

    @param logged request/header 的 header 对象（{config, adapterDefaults?}）。
    @param fallback 部署配置的初始选择。
    @returns {provider, model, reasoningEffort?}；reasoningEffort 仅在既有显式
        设置且非连接默认时保留（adapterDefaults.reasoningEffort 为真时放弃，
        交由连接默认驱动）。
    """
    if logged is None:
        return fallback
    config = logged.get("config") or {}
    selection = {"provider": config.get("provider"), "model": config.get("model")}
    reasoning = config.get("reasoningEffort")
    adapter_default = (logged.get("adapterDefaults") or {}).get("reasoningEffort")
    if reasoning is not None and not adapter_default:
        selection["reasoningEffort"] = reasoning
    return selection


# ---------- session/list 游标（index.ts SessionListCursor） ----------

def _resolve_session_list_page_size(value: int | None) -> int:
    """解析并校验部署属地的 page 上限（index.ts resolveSessionListPageSize）。"""
    resolved = value if value is not None else _DEFAULT_SESSION_LIST_PAGE_SIZE
    if isinstance(resolved, bool) or not isinstance(resolved, int) or resolved < 1:
        raise ValueError("acp: sessionListPageSize must be a positive safe integer")
    return resolved


def _encode_cursor(created_at: int, session_id: str) -> str:
    """keyset 续页令牌 = base64url(无填充, JSON [createdAt, sessionId])。"""
    raw = json.dumps([created_at, session_id],
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(value: str | None) -> dict | None:
    """解码并强制 canonical 的游标；畸形/非规范一律抛 ValueError。

    对齐上游 decodeSessionListCursor：字符集限制、字段形状校验、
    重编码比较（客户端无需理解语义，canonical 防歧义）。
    """
    if value is None:
        return None
    if not re.match(r"^[A-Za-z0-9_-]+$", value or ""):
        raise ValueError("session/list cursor is invalid")
    try:
        padded = value + "=" * ((4 - len(value) % 4) % 4)
        decoded = json.loads(
            base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        raise ValueError("session/list cursor is invalid")
    ok = (
        isinstance(decoded, list) and len(decoded) == 2
        and isinstance(decoded[0], int) and not isinstance(decoded[0], bool)
        and decoded[0] >= 0
        and isinstance(decoded[1], str) and len(decoded[1]) > 0
    )
    if not ok:
        raise ValueError("session/list cursor is invalid")
    if _encode_cursor(decoded[0], decoded[1]) != value:
        raise ValueError("session/list cursor is invalid")
    return {"createdAt": decoded[0], "sessionId": decoded[1]}


def _same_directory(left: str | None, right: str) -> bool:
    """按物理身份比较目录，缺失路径退化为词法比较（index.ts sameDirectory）。"""
    if left is None:
        return False
    try:
        return os.path.realpath(left) == os.path.realpath(right)
    except (OSError, ValueError):
        return os.path.abspath(left) == os.path.abspath(right)


def _session_bytes(session_id: str) -> bytes:
    """UTF-8 字节序的稳定 sessionId 比较键（index.ts compareSessionIds）。"""
    return session_id.encode("utf-8")


def _parse_tool_arguments(value: str) -> Any:
    """模型畸形 JSON 保留为不透明原文而非丢弃调用（updates.ts parseToolArguments）。"""
    try:
        return json.loads(value)
    except ValueError:
        return value


class AcpModelControl:
    """把一个 Agent 的 provider/model/reasoning 选择投影为 ACP 配置选项。

    对齐上游 model-control.ts AcpModelControl：model / reasoning_effort 两个
    标准选项、provider 分组目录、当前路由不在目录时补一枚、切换 model 复位
    reasoning 到 provider 默认。载体简化：上游经 llm 服务 listProviders/
    listModels/resolveModelInfo；mini 为单一 adapter 路由——可选教学扩展
    adapter.models_catalog（list of {provider, model, name, description?}）
    承载多模型目录，reasoning 目录经可选 adapter.resolve_model_info()['reasoning']
    （{efforts:[{id,name,description?}], defaultEffort?}）承载；内置适配器均不
    声明 → 默认无 reasoning 选项（如实标注）。
    """

    def __init__(self, adapter: Any, selection: dict | None):
        self._adapter = adapter
        self._selected = selection            # {provider, model, reasoningEffort?}
        self._turn_pin: dict | None = None    # 同步单飞 pin（上游 turnSelection）

    @property
    def current(self) -> dict | None:
        """Agent request 装配读取的当前选择（上游 selectionRef.current）。"""
        return self._turn_pin if self._turn_pin is not None else self._selected

    def snapshot(self) -> dict | None:
        """受理下一个 ACP prompt 时快照选择（上游 snapshot）。"""
        return dict(self._selected) if self._selected is not None else None

    def pin_turn(self, selection: dict | None) -> None:
        """pin 整个同步回合的精确受理选择。

        同步载体简化：mini 单飞串行，无并发穿插，pin 覆盖一次 prompt 的
        全部 step；上游按 turn 编号 pinTurn/releaseTurn（turn/end 时释放），
        语义等价（见模块 docstring 简化标注）。
        """
        if selection is not None:
            self._turn_pin = dict(selection)

    def release_turn(self) -> None:
        self._turn_pin = None

    def options(self) -> list[dict]:
        """完整标准配置选项状态（model-control.ts options()）。"""
        return self._state()["options"]

    def set(self, config_id: str, value: Any) -> list[dict]:
        """应用一个已广告的选项并返回完整结果状态（model-control.ts set()）。

        校验与错误文案逐字对齐上游；具体：
          * value 非字符串 → f"{configId} requires a select value"
          * 无选择 → "this session has no model selection"
          * 未知 configId → "unknown session config option: {configId}"
          * model：目录外的 value → "unknown model option: {value}"；
            切换后 reasoning 复位（{provider, model} 无 reasoningEffort）
          * reasoning_effort：除 provider-default（''，仅当无 defaultEffort）
            外必须命中目录 → "unknown reasoning effort for {p}/{m}: {value}"
        """
        if not isinstance(value, str):
            raise AcpModelConfigError(f"{config_id} requires a select value")
        current = self._selected
        if current is None:
            raise AcpModelConfigError("this session has no model selection")
        if config_id == _MODEL_CONFIG_ID:
            state = self._state()
            selected = state["choices"].get(value)
            if selected is None:
                raise AcpModelConfigError(f"unknown model option: {value}")
            self._selected = {"provider": selected["provider"],
                              "model": selected["model"]}
        elif config_id == _REASONING_CONFIG_ID:
            info = self._resolve_info() or {}
            reasoning = info.get("reasoning")
            provider_default = (
                value == _PROVIDER_DEFAULT_REASONING_VALUE
                and reasoning is not None
                and reasoning.get("defaultEffort") is None
            )
            valid = (
                reasoning is not None
                and (
                    provider_default
                    or any(e["id"] == value for e in reasoning.get("efforts", []))
                )
            )
            if not valid:
                raise AcpModelConfigError(
                    f"unknown reasoning effort for {current['provider']}/"
                    f"{current['model']}: {value}")
            nxt = {"provider": current["provider"], "model": current["model"]}
            if not provider_default:
                nxt["reasoningEffort"] = value
            self._selected = nxt
        else:
            raise AcpModelConfigError(f"unknown session config option: {config_id}")
        return self._state()["options"]

    def _state(self) -> dict:
        """构建分离的模型选择目录与依赖的 reasoning 选项（state()）。

        返回 {"choices": {value: {provider, model}}, "options": [...]}。
        当前路由不在任何目录时补一枚（上游 choices 未命中即 unshift）。
        """
        selected = self._selected
        if selected is None:
            return {"choices": {}, "options": []}
        provider = selected.get("provider")
        model = selected.get("model")
        info = self._resolve_info()
        choices: dict[str, dict] = {}
        groups: list[dict] = []
        by_provider: dict[str, dict] = {}
        for entry in getattr(self._adapter, "models_catalog", None) or []:
            gp = by_provider.get(entry["provider"])
            if gp is None:
                gp = {"group": entry["provider"], "name": entry["provider"],
                      "options": []}
                by_provider[entry["provider"]] = gp
                groups.append(gp)
            value = _model_value(entry["provider"], entry["model"])
            choices[value] = {"provider": entry["provider"], "model": entry["model"]}
            opt = {"value": value, "name": entry.get("name", entry["model"])}
            if entry.get("description") is not None:
                opt["description"] = entry["description"]
            gp["options"].append(opt)
        current_value = _model_value(provider, model)
        if current_value not in choices:
            choices[current_value] = {"provider": provider, "model": model}
            gp = by_provider.get(provider)
            if gp is None:
                gp = {"group": provider, "name": provider, "options": []}
                by_provider[provider] = gp
                groups.append(gp)
            gp["options"].insert(0, {"value": current_value, "name": model})
        options: list[dict] = [{
            "id": _MODEL_CONFIG_ID,
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": current_value,
            "options": [g for g in groups if g["options"]],
        }]
        reasoning = info.get("reasoning") if info else None
        if reasoning is not None:
            re_options: list[dict] = []
            if reasoning.get("defaultEffort") is None:
                re_options.append({
                    "value": _PROVIDER_DEFAULT_REASONING_VALUE,
                    "name": "Provider default",
                })
            for effort in reasoning.get("efforts", []):
                opt = {"value": effort["id"],
                       "name": effort.get("name", effort["id"])}
                if effort.get("description") is not None:
                    opt["description"] = effort["description"]
                re_options.append(opt)
            current_effort = selected.get("reasoningEffort")
            options.append({
                "id": _REASONING_CONFIG_ID,
                "name": "Reasoning effort",
                "category": "thought_level",
                "type": "select",
                "currentValue": (_PROVIDER_DEFAULT_REASONING_VALUE
                                 if current_effort is None else str(current_effort)),
                "options": re_options,
            })
        return {"choices": choices, "options": options}

    def _resolve_info(self) -> dict | None:
        """当前路由的能力声明；声明失败按不可解析处理（上游 per-provider 容错）。"""
        try:
            return self._adapter.resolve_model_info()
        except Exception:
            return None


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
    resume_session / list_sessions / set_session_config_option /
    close_session / prompt / cancel；会话更新流经 `_emit_session_updates` 投影为
    `session/update` 通知（逐条排发，回合结束后批量一次性投影，简化标注）。
    会话生命周期在内存完成：close 归档、resume 复用同一 Session 对象与装配
    ctx、冷重建 loop（无磁盘持久化，见模块 docstring 载体简化）。
    """

    WIRE_NAME = "deepseek-harness-acp"

    def __init__(self, adapter: Any = None, provider: str | None = None,
                 model: str | None = None, attachment: Any = None,
                 session_list_page_size: int | None = None):
        self._adapter = adapter or FakeLlmAdapter()
        # 初始路由：构造参数优先，缺省取 adapter 声明的路由（上游 AcpConfig
        # provider/model；mini 单一 adapter 即路由本体）
        self.provider = provider if provider is not None else self._adapter.provider
        self.model = (model if model is not None
                      else getattr(self._adapter, "model", None) or self.provider)
        self._initial_selection = {"provider": self.provider, "model": self.model}
        self._attachment = attachment   # AttachmentStore | None（无则宣称 image:false）
        self._sessions: dict[str, dict] = {}    # 活跃: session_id -> record
        self._archived: dict[str, dict] = {}    # 已关闭归档（可恢复池）
        self._closed = False
        self.updates: list[dict] = []          # 会话更新流（简化载体）
        self._answerer: Callable | None = None   # 审批决策注入（测试用）
        self._session_list_page_size = _resolve_session_list_page_size(
            session_list_page_size)

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
                "sessionCapabilities": {"close": {}, "list": {}, "resume": {}},
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
        record = {
            "session": Session(session_id),
            "cwd": cwd,
            "ctx": ctx,
            "reg": self._scaffold(ctx),
            "loop": None,
            "model_control": None,
            "selection": self._initial_selection,
            "projected_seq": 0,
            "inflight": False,
            "meter": TokenMeter(),
        }
        record["model_control"] = AcpModelControl(self._adapter,
                                                  self._initial_selection)
        self._install_model_selection(ctx, record)
        record["loop"] = self._activate(record)
        self._sessions[session_id] = record
        self._archived[session_id] = record
        return {"sessionId": session_id,
                "configOptions": record["model_control"].options()}

    def list_sessions(self, cwd: str | None = None,
                      cursor: str | None = None) -> dict:
        """列出已关闭/可恢复会话（index.ts listSessions 的 mini 等价）。

        必然排除活跃会话（本进程归档即"持久化层"）；createdAt 降序、
        同值按 sessionId UTF-8 字节升序；keyset 游标强制 canonical。
        """
        self._assert_open()
        if cwd is not None and not os.path.isabs(cwd):
            raise invalid_params(f"cwd must be an absolute path: {cwd}")
        try:
            cursor_obj = _decode_cursor(cursor)
        except ValueError as e:
            raise invalid_params(str(e))
        entries = []
        for session_id, record in self._archived.items():
            if session_id in self._sessions:
                # 活跃会话不计入恢复列表（上游 listSessions 排除活跃）
                continue
            header_cwd = record["cwd"]
            if header_cwd is None or not os.path.isabs(header_cwd):
                continue
            if cwd is not None and not _same_directory(header_cwd, cwd):
                continue
            entries.append({
                "sessionId": record["session"].session_id,
                "cwd": header_cwd,
                "createdAt": record["session"].created_at,
            })
        entries.sort(key=lambda e: (-e["createdAt"], _session_bytes(e["sessionId"])))
        if cursor_obj is not None:
            entries = [
                e for e in entries
                if e["createdAt"] < cursor_obj["createdAt"]
                or (e["createdAt"] == cursor_obj["createdAt"]
                    and _session_bytes(e["sessionId"])
                    > _session_bytes(cursor_obj["sessionId"]))
            ]
        page = entries[:self._session_list_page_size]
        has_more = len(entries) > len(page)
        result: dict = {
            "sessions": [{"sessionId": e["sessionId"], "cwd": e["cwd"]}
                         for e in page],
        }
        if has_more:
            # keyset 游标 = 本页最后一条（上游 page.at(-1)），下页从严格
            # 大于游标处续取，边界条目不丢。
            last = page[-1]
            result["nextCursor"] = _encode_cursor(last["createdAt"],
                                                  last["sessionId"])
        return result

    def resume_session(self, session_id: str, cwd: str,
                       additional_directories: list | None = None,
                       mcp_servers: list | None = None) -> dict:
        """恢复一个已关闭的会话（index.ts resumeSession 的 mini 等价）。

        校验顺序对齐上游：cwd 绝对路径 → additionalDirectories/mcpServers
        拒绝（mini 无 MCP）→ 活跃拒绝 → 不可恢复拒绝 → cwd 物理一致；
        恢复路由 = selectionFor(最近 request/header, 归档回退选择)。
        """
        self._assert_open()
        if not os.path.isabs(cwd):
            raise invalid_params(f"cwd must be an absolute path: {cwd}")
        if additional_directories:
            raise invalid_params("additionalDirectories is not supported")
        if mcp_servers:
            raise invalid_params("mcpServers is not supported")
        if session_id in self._sessions:
            raise invalid_params(f"session is already active: {session_id}")
        record = self._archived.get(session_id)
        if record is None:
            raise invalid_params(f"session is not resumable: {session_id}")
        if not _same_directory(record["cwd"], cwd):
            raise invalid_params(f"session cwd does not match: {cwd}")
        model_control = AcpModelControl(
            self._adapter,
            selection_for(self._last_request_header(record), record["selection"]),
        )
        record["model_control"] = model_control
        record["loop"] = self._activate(record)
        record["inflight"] = False
        self._sessions[session_id] = record
        return {"configOptions": model_control.options()}

    def set_session_config_option(self, session_id: str,
                                  config_id: str, value: Any) -> dict:
        """应用一个标准配置选项并返回完整选项状态（index.ts setSessionConfigOption）。"""
        self._assert_open()
        record = self._sessions.get(session_id)
        if record is None:
            raise invalid_params(f"unknown session: {session_id}")
        try:
            return {"configOptions": record["model_control"].set(config_id, value)}
        except AcpModelConfigError as e:
            raise invalid_params(e.message)

    def close_session(self, session_id: str) -> dict:
        """关闭会话并入可恢复归档（index.ts closeSession）。

        关闭（cancel + 拆 loop 子作用域）失败 → internal error
        "session close failed: <e>"；finally 必解除活跃登记（对齐上游）。
        """
        self._assert_open()
        record = self._sessions.get(session_id)
        if record is None:
            raise invalid_params(f"unknown session: {session_id}")
        try:
            record["loop"].cancel()
            record["loop"].dispose()
        except Exception as e:
            raise internal_error(f"session close failed: {e}")
        finally:
            if self._sessions.get(session_id) is record:
                del self._sessions[session_id]
        return {}

    def _scaffold(self, ctx: Context) -> ToolRegistry:
        """每会话一次幂等装配：服务安装 + 工具注册（resume 复用，不重复注册）。"""
        if getattr(ctx, "_miniharness_acp_scaffolded", False):
            return ctx.get("tools")
        apply_retry_planner(ctx)
        install_compaction(ctx)
        install_tool_result_pruner(ctx)
        install_jobs(ctx)
        install_skills(ctx)
        install_system_prompt(ctx)
        install_sessions(ctx)
        install_agents(ctx)
        reg = ToolRegistry(ctx)
        register_job_tools(reg, ctx.get("jobs"))
        register_skill_tools(reg, ctx.get("skills"))
        ctx._miniharness_acp_scaffolded = True
        return reg

    def _install_model_selection(self, ctx: Context, record: dict) -> None:
        """每会话一次安装 agent/request 选择施加监听（resume 复用同一 ctx）。"""
        if getattr(ctx, "_miniharness_acp_model_bound", False):
            return
        ctx.on("agent/request",
               lambda config, next_fn: self._apply_selection(record, config, next_fn))
        ctx._miniharness_acp_model_bound = True

    def _apply_selection(self, record: dict, config: dict, next_fn: Callable) -> dict:
        """waterfall 尾段：先委派整链，再对决议 config 施加当前选择。

        对齐上游 model-selection.ts installModelSelection 的 agent/request 段：
        去掉继承的 reasoningEffort（选择未声明时恢复 provider 默认），覆写
        provider/model，仅当选择携带 effort 才补回 reasoningEffort。
        """
        resolved = next_fn(config)
        selected = record["model_control"].current
        if selected is None or not isinstance(resolved, dict):
            return resolved
        out = {k: v for k, v in resolved.items() if k != "reasoningEffort"}
        out["provider"] = selected.get("provider")
        out["model"] = selected.get("model")
        if "reasoningEffort" in selected:
            out["reasoningEffort"] = selected["reasoningEffort"]
        return out

    def _activate(self, record: dict) -> AgentLoop:
        """冷重建 loop：复用同一 Session 对象与装配 ctx，发布即重进会话店。

        上游 resume 等价物（session.ts resume）：拆解只卸 loop 子作用域与
        会话店成员资格；恢复后新 loop 首个请求以 reason='resume' 重落
        request/header（AgentLoop._stream_step_async 同语义）。
        """
        loop = AgentLoop(record["session"], self._adapter, record["reg"],
                         record["ctx"])
        loop.publish()
        return loop

    def _last_request_header(self, record: dict) -> dict | None:
        """日志里最近一次 request/header 的 header 对象（session.ts requestHeader()）。"""
        for event in reversed(record["session"].events):
            if event["type"] == "request/header":
                return event["data"].get("header")
        return None

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
        # 受理时 snapshot 选择并在整个同步回合内 pin（session.ts snapshot +
        # onInboxClaimed → pinTurn；mini 单飞串行，无穿插切换）
        prompt_selection = record["model_control"].snapshot()
        record["inflight"] = True
        try:
            message = create_message("user", content, {"kind": "user"})
            record["model_control"].pin_turn(prompt_selection)
            try:
                record["loop"].followup(message)
            finally:
                record["model_control"].release_turn()
            self._emit_session_updates(record)
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

    def _emit_session_updates(self, record: dict) -> None:
        """把新已提交会话事件投影为 ACP 更新流（updates.ts + session.ts 同款）。

        assistant/message 仅 inflight turn 内的块可见（事件逐 block：reasoning
        非空 → agent_thought_chunk；其余经 assistantBlockToAcp →
        agent_message_chunk，均携带 messageId）；tool/call → tool_call 起始，
        tool/result → tool_call_update 结算。projected_seq 截止杜绝跨 prompt
        重复投射旧 turn 的工具更新；image 块经 readImage 读回 base64 内联。
        assistant/message 带 usage 且会话有 contextWindow 时，块更新之后额外
        发射 usage_update（对齐 updates.ts usageUpdate）。
        """
        session = record["session"]
        events = session.events
        cutoff = record["projected_seq"]
        inflight_turn = None
        for event in reversed(events):
            if event["type"] == "turn/end":
                inflight_turn = event["data"].get("turn")
                break
        for event in events[cutoff:]:
            etype = event["type"]
            if etype == "assistant/message":
                if event["data"].get("turn") != inflight_turn:
                    continue
                message = event["data"].get("message") or {}
                message_id = message.get("id")
                for block in message.get("content", []):
                    if block.get("type") == "reasoning":
                        text = block.get("text", "")
                        if text:
                            self._push_update(record, {
                                "sessionUpdate": "agent_thought_chunk",
                                "messageId": message_id,
                                "content": {"type": "text", "text": text},
                            })
                        continue
                    content = assistant_block_to_acp(self._attachment, block)
                    if content is None:
                        continue
                    self._push_update(record, {
                        "sessionUpdate": "agent_message_chunk",
                        "messageId": message_id,
                        "content": content,
                    })
                self._emit_usage_update(record, event)
            elif etype == "tool/call":
                data = event["data"]
                self._push_update(record, {
                    "sessionUpdate": "tool_call",
                    "toolCallId": data.get("callId"),
                    "title": data.get("name"),
                    "kind": "other",
                    "status": "in_progress",
                    "rawInput": _parse_tool_arguments(data.get("arguments", "")),
                })
            elif etype == "tool/result":
                data = event["data"]
                blocks = (data.get("message") or {}).get("content") or []
                if not blocks:
                    continue
                block = blocks[0]
                content = []
                for inner in block.get("content", []):
                    converted = assistant_block_to_acp(self._attachment, inner)
                    if converted is None:
                        continue
                    content.append({"type": "content", "content": converted})
                self._push_update(record, {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": block.get("toolCallId"),
                    "status": "failed" if block.get("isError") else "completed",
                    "content": content,
                })
        record["projected_seq"] = len(events)

    def _emit_usage_update(self, record: dict, event: dict) -> None:
        """assistant/message 带 usage 且会话有 contextWindow 时发射 usage_update。

        对齐 updates.ts usageUpdate：三个事实齐备才发射（usage 存在、
        requestContext().contextWindow 存在、tokenMeter 可用），缺任一个静默
        省略；used = tokenMeter.measure(session).totalTokens，size = contextWindow。
        """
        if event["data"].get("usage") is None:
            return
        size = (record["session"].request_context() or {}).get("contextWindow")
        if size is None:
            return
        total = record["meter"].measure(record["session"])["totalTokens"]
        self._push_update(record, {
            "sessionUpdate": "usage_update",
            "used": total,
            "size": size,
        })

    def _push_update(self, record: dict, update: dict) -> None:
        """追加一条 session/update 记录（简化载体；上游经 notify 发送标准通知）。"""
        self.updates.append({
            "sessionId": record["session"].session_id,
            "update": update,
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
        self._archived.clear()

    def _assert_open(self) -> None:
        if self._closed:
            raise internal_error("the ACP bridge has been disposed")

    @property
    def sessions(self) -> dict:
        return self._sessions

    @property
    def archived(self) -> dict:
        """已关闭（可恢复）会话记录：session_id -> record（测试与内省用）。"""
        return self._archived