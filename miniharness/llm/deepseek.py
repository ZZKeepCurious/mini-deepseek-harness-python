"""DeepSeek 官方 Messages API 的 wire 适配器。

对应 dsh 真实源码：packages/llm/llm-deepseek/src（adapter.ts + serialize.ts +
sse.ts + translate.ts）。

上游 llm-deepseek 自 dsh-v0.1.7-rc.1 起只保留 Anthropic 兼容 Messages 协议
（Chat Completions 已删除）：默认 base ``https://api.deepseek.com/anthropic``，
请求 POST 到 ``messagesApiRoot(baseURL) + "/messages"``。自 dsh-v0.2.0-rc.2 起
认证经 ``resolveAuth`` seam 拆双 provider：``deepseek-official`` 发 ``x-api-key``、
``deepseek-account`` 发 ``x-dsh-auth-token``；另加 ``anthropic-version:
2023-06-01``（file id 请求追加 ``anthropic-beta``）。协议
细节在 deepseek_messages.py；本模块是传输适配器：

  * 请求体 stream:true，响应以 message_stop 为完成点；EOF 未到 message_stop
    抛 STREAM_CLOSED（截断响应不可信）；带内 error 事件即 provider 失败
  * 空响应抛 EMPTY_RESPONSE
  * 错误映射：401/403→AUTH、413→INVALID_REQUEST、quota 措辞→QUOTA、
    429→RATE_LIMIT、400 上下文超限→CONTEXT_WINDOW_EXCEEDED（否则
    INVALID_REQUEST）、500+→SERVER、其余→HTTP_<status>；LlmError facts
    （status / providerRetryAfterMs / requestId）
  * image-capable 路径：Files API file-id 优先、解析失败整请求回退 inline
    base64
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from .protocol import (
    ACCOUNT_QUOTA,
    ACCOUNT_SIGN_IN_REQUIRED,
    ACCOUNT_TOKEN_INVALID,
    IMAGE_OFFLOAD_REQUIRED,
    INVALID_CREDENTIAL,
    MISSING_CREDENTIAL,
    QUOTA,
    TIMEOUT,
    TRANSPORT,
    LlmAdapter,
    LlmFailure,
    attribution_headers,
)
from ..identity import get_or_create_anonymous_user_id
from .retry_policy import resolve_retry_policy
from .content import (
    content_has_image,
    content_has_file,
    offloaded_image_text,
    project_offloaded_images,
    project_images_for_text_model,
    request_image_handle_text,
    required_image_offload,
    resolve_image_attachment_access,
    text_only_image_text,
)
from .deepseek_messages import (
    MESSAGES_FILES_BETA,
    MESSAGES_TOOL_CHANGES_BETA,
    UNSUPPORTED_CONTENT,
    has_tool_changes,
    messages_api_root,
    parse_sse_frames,
    provider_error,
    provider_retry_after_ms,
    request_id,
    resolve_image_parts,
    serialize,
    serialize_messages,
    serialize_messages_with_images,
    translate,
    _error_detail,
    _error_message,
    _http_error_code,
)
from ..attachment.types import AttachmentId, Dimensions, ImageAttachmentRef
from .deepseek_files import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_FILES_API_TIMEOUT_MS,
    DEFAULT_FILE_EXPIRY_SECONDS,
    DEFAULT_FILE_QUOTA_CLEANUP_BATCH,
    DEFAULT_FILE_REFRESH_MARGIN_SECONDS,
    DEFAULT_IMAGE_OFFLOAD_BYTE_QUANTUM,
    DEFAULT_IMAGE_OFFLOAD_COUNT_QUANTUM,
    DEFAULT_INLINE_IMAGE_OFFLOAD_BYTE_QUANTUM,
    DEFAULT_MAX_IMAGES_PER_REQUEST,
    DEFAULT_MAX_INLINE_REQUEST_IMAGE_BYTES,
    DEFAULT_MAX_REQUEST_FILES_BYTES,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODELS,
    DEFAULT_STREAM_IDLE_TIMEOUT_MS,
    DeepSeekConnectionOptions,
    DeepSeekFileConnection,
    DeepSeekFilePolicy,
    DeepSeekFileStore,
    FileResolutionFailure,
    ImageWireLocation,
    RequestDefaults,
    RequestFiles,
    deep_seek_image_request_pricing,
    model_info,
    resolve_request_image_target,
)

__all__ = [
    "DeepSeekAdapter",
    "UNSUPPORTED_CONTENT",
    "content_has_file",
    "content_has_image",
    "IMAGE_OFFLOAD_REQUIRED",
    "MESSAGES_FILES_BETA",
    "messages_api_root",
    "provider_retry_after_ms",
    "request_id",
    "serialize",
    "serialize_messages",
    "serialize_messages_with_images",
    "project_offloaded_images",
    "project_images_for_text_model",
    "required_image_offload",
    "text_only_image_text",
    "offloaded_image_text",
    "request_image_handle_text",
]


# 上游 api-key.ts LEGAL_API_KEY：HTTP 头可逐字携带的字符（可打印 ASCII，空格除外）。
_LEGAL_API_KEY = re.compile(r"^[\x21-\x7E]+$")


# ---------- image-capable 请求辅助（serialize.ts / request-files.ts 载体桥） ----------

def _ref_from_dict(value):
    """消息块里的 ImageAttachmentRef dict → dataclass（attachment 服务期望 dataclass）。"""
    if isinstance(value, ImageAttachmentRef):
        return value
    original = value.get("originalDimensions")
    return ImageAttachmentRef(
        attachmentId=AttachmentId(str(value["attachmentId"])),
        mediaType=value["mediaType"],
        bytes=value["bytes"],
        width=value["width"],
        height=value["height"],
        name=value.get("name"),
        originalDimensions=(Dimensions(**original) if isinstance(original, dict)
                            else original),
    )


def _version_dict(version) -> dict:
    """RequestImageAttachment（dataclass）→ 序列化器期望的 dict 载体。"""
    attachment = version.attachment
    return {
        "variantId": str(version.variantId),
        "attachment": attachment.to_dict() if hasattr(attachment, "to_dict") else attachment,
        "data": version.data,
        "mediaType": version.mediaType,
        "bytes": version.bytes,
        "width": version.width,
        "height": version.height,
        "depth": version.depth,
        "space": version.space,
        "hasAlpha": version.hasAlpha,
    }


def _make_resolve_file_id(request_files, objects: dict):
    """把一个请求版本的 Files 解析包装成序列化器期望的 async resolveFileId 回调。"""
    async def resolve_file_id(version, block, location):
        attachment_id = str(block["attachment"]["attachmentId"])
        resolved = await request_files.resolve(
            objects[attachment_id],
            ImageWireLocation(location["message"], location["image"]))
        return resolved
    return resolve_file_id


class DeepSeekAdapter(LlmAdapter):
    """DeepSeek 官方 Messages API 的 SSE 适配器（httpx 异步传输）。

    与上游 llm-deepseek 一致：
      * 请求体 stream:true，响应以 message_stop 为完成点
      * EOF 未到 message_stop 抛 STREAM_CLOSED（截断响应不可信）
      * 空响应抛 EMPTY_RESPONSE
      * per-read idle 超时 300s（对齐上游 fetch watchdog）+ 真取消：abort
        置位即关闭连接（httpx 原生 asyncio 传输，无遗留线程）

    认证 seam（dsh-v0.2.0-rc.2 的 resolveAuth 拆分）：``auth`` 选择凭据模式
    ——``"official"``（x-api-key）或 ``"account"``（x-dsh-auth-token）；缺省按
    account_token 是否存在推断。``provider`` 随之返回 ``deepseek-official`` /
    ``deepseek-account``。两模式无交叉回退。请求与 Files API 都经
    ``_auth_headers()`` 取头。

    推理 effort 与上游 serialize.ts 一致：请求级 reasoningEffort 承载
    off/low/high/max 四档，经 thinking/output_config 入请求体；thinking
    禁用仅允许 off。响应侧 thinking→reasoning、usage 按 Anthropic 拼写映射。
    """

    # 凭据模式对应的 provider id（上游 llm-deepseek-api-key / -account 两包）；
    # 实例构造时按 auth 覆盖为 "deepseek-account"。
    provider = "deepseek-official"

    # 上游 llm-deepseek REASONING_EFFORTS：'off' 在 wire 上省略
    REASONING_EFFORTS = ("off", "low", "high", "max")

    # 上游 per-read idle watchdog（fetch 流读间隙超时）；连接超时取常用值
    CONNECT_TIMEOUT_S = 30.0
    READ_TIMEOUT_S = 300.0

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str = "deepseek-chat", max_tokens: int | None = None,
                 retry_policy: dict | None = None, transport=None,
                 reasoning_effort: str | None = None,
                 models=None, attachments=None, map_host_path=None,
                 files_store=None, files_transport=None,
                 file_policy=None, default_context_window=None,
                 thinking=None, account_token=None,
                 auth: str | None = None, reject_token=None):
        self._key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
        # DSH 账户 token：配置后以 x-dsh-auth-token 发送（账户 provider 路由）。
        self._account_token = account_token
        # 认证 seam（上游 resolveAuth 拆分）：显式 auth 选择凭据模式；缺省按
        # account_token 是否存在推断（account_token 在场 → account，否则 official）。
        if auth is None:
            auth = "account" if account_token is not None else "official"
        if auth not in ("official", "account"):
            raise ValueError(
                f'invalid auth {auth!r}; expected "official" or "account"')
        self._auth = auth
        # provider id 随凭据模式（实例覆盖类缺省 deepseek-official）。
        self.provider = "deepseek-account" if auth == "account" else "deepseek-official"
        # 账户 token 被 401 拒绝时的回调（上游 account.rejectToken；mini 由宿主注入）。
        self._reject_token = reject_token
        self._base = (base_url or os.environ.get(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")).rstrip("/")
        self._model = model
        self._max_tokens = max_tokens
        self._thinking = thinking
        self._transport = transport  # httpx transport（MockTransport 测试注入口）
        if reasoning_effort is not None and reasoning_effort not in self.REASONING_EFFORTS:
            raise ValueError(
                f"invalid reasoning mode {reasoning_effort!r}; "
                f"expected one of {self.REASONING_EFFORTS}")
        if thinking == "disabled" and reasoning_effort not in (None, "off"):
            raise ValueError('only reasoning_effort "off" can be configured when thinking is disabled')
        self._reasoning_effort = reasoning_effort
        # 上游 llm-deepseek：retryPolicy 省略即 normal 默认（resolveRetryPolicy(undefined)）
        self.retry_policy = resolve_retry_policy(retry_policy, "llm-deepseek: retryPolicy")
        # 请求侧图片能力（上游 DeepSeekAdapterOptions 的 mini 载体）：
        #   * models——建议性目录（缺省 DEFAULT_MODELS，inputModalities 含 image
        #     才宣称支持图片输入）；
        #   * attachments——按操作解析的 attachment 服务（callable，缺省 None）；
        #   * map_host_path——宿主路径→工具执行世界的映射（callable，可选）；
        #   * files_store / files_transport——Files API 执行簇与测试注入。
        self._models = tuple(models) if models is not None else DEFAULT_MODELS
        self._resolve_attachments = attachments
        self._map_host_path = map_host_path
        self._files_store = files_store
        self._files_transport = files_transport
        self._connection = DeepSeekConnectionOptions(
            baseURL=self._base,
            defaults=RequestDefaults(thinking=thinking, reasoningEffort=reasoning_effort),
            maxTokens=max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS,
            defaultContextWindow=(default_context_window if default_context_window is not None
                                  else DEFAULT_CONTEXT_WINDOW),
            models=self._models,
            streamIdleTimeoutMs=int(self.READ_TIMEOUT_S * 1000),
            maxRequestFilesBytes=DEFAULT_MAX_REQUEST_FILES_BYTES,
            maxInlineRequestImageBytes=DEFAULT_MAX_INLINE_REQUEST_IMAGE_BYTES,
            maxImagesPerRequest=DEFAULT_MAX_IMAGES_PER_REQUEST,
            imageOffloadByteQuantum=DEFAULT_IMAGE_OFFLOAD_BYTE_QUANTUM,
            inlineImageOffloadByteQuantum=DEFAULT_INLINE_IMAGE_OFFLOAD_BYTE_QUANTUM,
            imageOffloadCountQuantum=DEFAULT_IMAGE_OFFLOAD_COUNT_QUANTUM,
            filesApiTimeoutMs=DEFAULT_FILES_API_TIMEOUT_MS,
            filePolicy=file_policy if file_policy is not None else DeepSeekFilePolicy(
                DEFAULT_FILE_EXPIRY_SECONDS, DEFAULT_FILE_REFRESH_MARGIN_SECONDS,
                DEFAULT_FILE_QUOTA_CLEANUP_BATCH),
        )

    def _files(self) -> DeepSeekFileStore:
        """进程级上传复用 store（上游 resolveFiles）。"""
        if self._files_store is None:
            self._files_store = DeepSeekFileStore(transport=self._files_transport)
        return self._files_store

    def image_request_pricing(self, model: str) -> "object":
        """按连接快照构建 provider 侧请求图定价（上游 imageRequestPricing）。"""
        model_entry = next((entry for entry in self._models if entry.id == model), None)
        if model_entry is None or "image" not in (model_entry.inputModalities or ()):
            return deep_seek_image_request_pricing(self._connection, model)
        attachments = self._resolve_attachments() if self._resolve_attachments is not None else None
        if attachments is None or self._map_host_path is None:
            return deep_seek_image_request_pricing(self._connection, model)

        def resolve_access(ref):
            return resolve_image_attachment_access(
                attachments, self._map_host_path, _ref_from_dict(ref))

        return deep_seek_image_request_pricing(self._connection, model, resolve_access)

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def reasoning_effort(self) -> str | None:
        """推理 effort 档位（'off'|'low'|'high'|'max'，None 表示未设置）。"""
        return self._reasoning_effort

    def _auth_headers(self) -> dict:
        """解析本次请求/文件操作的认证头（上游 resolveAuth → { headers }）。

        * official：x-api-key，缺失/畸形分别抛 MISSING_CREDENTIAL / INVALID_CREDENTIAL；
        * account：x-dsh-auth-token，未登录抛 ACCOUNT_SIGN_IN_REQUIRED。
        两模式无交叉回退。
        """
        if self._auth == "account":
            token = self._account_token
            if token is None or len(token) == 0:
                raise LlmFailure(
                    ACCOUNT_SIGN_IN_REQUIRED,
                    "Sign in to DeepSeek to use the account provider. The request "
                    "destination must allow account authentication.")
            return {"x-dsh-auth-token": token}
        key = (self._key or "").strip()
        if len(key) == 0:
            raise LlmFailure(
                MISSING_CREDENTIAL,
                'llm-deepseek: no API key for provider route "deepseek-official"; '
                "store DEEPSEEK_API_KEY through the credentials service (the web "
                "Models page writes it), or export DEEPSEEK_API_KEY in the launching "
                "environment")
        if not _LEGAL_API_KEY.match(key):
            raise LlmFailure(
                INVALID_CREDENTIAL,
                "llm-deepseek: the API key resolved from DEEPSEEK_API_KEY contains "
                "characters no HTTP header can carry; set DEEPSEEK_API_KEY to the raw "
                "key alone (the web Models page writes it)")
        return {"x-api-key": key}

    def _map_request_error(self, error: LlmFailure) -> LlmFailure:
        """账户路由的失败分类（上游 DeepSeekRequestAuth.onRequestError）。

        * QUOTA → ACCOUNT_QUOTA（HTTP 402 与带内 SSE error 同码）；
        * HTTP 401 → ACCOUNT_TOKEN_INVALID 并调用 reject_token(token)；
        * 其余状态（含 403）原样透传；official 一律原样透传。
        """
        if self._auth != "account":
            return error
        if error.code == QUOTA:
            return LlmFailure(
                ACCOUNT_QUOTA, str(error), status=error.status,
                provider_retry_after_ms=error.provider_retry_after_ms,
                request_id=error.request_id)
        if error.status != 401:
            return error
        token = self._account_token
        if token is not None and self._reject_token is not None:
            try:
                self._reject_token(token)
            except Exception:
                # 凭据清除失败不得取代推断失败（上游注释同款）。
                pass
        return LlmFailure(
            ACCOUNT_TOKEN_INVALID, str(error), status=error.status,
            provider_retry_after_ms=error.provider_retry_after_ms,
            request_id=error.request_id)

    def resolve_model_info(self) -> dict:
        """按模型目录解析能力（上游 adapter.ts resolveModelInfo → modelInfo）。"""
        return model_info(self._connection, self.provider, self._model)

    async def stream(self, messages, tools, signal=None,
                     session_id=None, purpose=None):
        """async 迭代器（对齐上游 async stream）：httpx 异步传输 + Messages SSE。

        含图片且模型目录宣称 image 输入时走 image-capable 序列化（Files API
        file-id 优先、解析失败整请求回退 inline base64），否则走文本路径。
        session_id/purpose 映射为 x-deepseek-harness-session-id/-compact 头
        （adapter.ts:129-130）。
        """
        abort_event = getattr(signal, "event", None) if signal is not None else None
        if not any(content_has_image(message.get("content") or []) for message in messages):
            body = self._build_body(messages, tools)
            async for chunk in self._iter_request_chunks(
                    body, abort_event, session_id=session_id, purpose=purpose):
                yield chunk
            return
        async for chunk in self._stream_images_impl(
                messages, tools, abort_event, signal, session_id, purpose):
            yield chunk

    async def _iter_request_chunks(self, *args, **kwargs):
        """请求执行段（HTTP + SSE）的账户失败映射包装（上游 request 的 catch 块）。

        模型能力校验/附件准备在进入本包装前完成，故其失败不经 onRequestError。
        """
        try:
            async for chunk in self._iter_chunks(*args, **kwargs):
                yield chunk
        except LlmFailure as error:
            mapped = self._map_request_error(error)
            if mapped is error:
                raise
            raise mapped from error

    async def _stream_images_impl(self, messages, tools, abort_event, signal,
                                  session_id=None, purpose=None):
        model = next((entry for entry in self._models if entry.id == self._model), None)
        if model is None or "image" not in (model.inputModalities or ()):
            raise LlmFailure(
                UNSUPPORTED_CONTENT,
                f'DeepSeek model "{self._model}" does not accept image input.')
        attachments = self._resolve_attachments() if self._resolve_attachments is not None else None
        if attachments is None:
            raise LlmFailure(
                UNSUPPORTED_CONTENT,
                "DeepSeek image conversion requires the durable attachment service.")
        request_images, request_objects = self._prepare_request_images(
            messages, attachments, model)
        resolve_access = None
        if self._map_host_path is not None:
            def resolve_access(ref):
                return resolve_image_attachment_access(
                    attachments, self._map_host_path, _ref_from_dict(ref))
        file_connection = DeepSeekFileConnection(
            baseURL=self._base, headers=self._auth_headers())
        request_files = RequestFiles(
            self._files(), file_connection, self._connection.filePolicy,
            self._connection.filesApiTimeoutMs, abort_event, lambda: None)
        representation: dict = {"kind": "file"}
        while True:
            request_files.begin_attempt()
            images = {
                "representation": (
                    representation if representation["kind"] == "base64"
                    else {"kind": "file",
                          "resolveFileId": _make_resolve_file_id(request_files, request_objects)}
                ),
                "requestImages": request_images,
                "maxRequestImageBytes": (
                    self._connection.maxInlineRequestImageBytes
                    if representation["kind"] == "base64"
                    else self._connection.maxRequestFilesBytes),
                "maxImagesPerRequest": self._connection.maxImagesPerRequest,
                "byteQuantum": (
                    self._connection.inlineImageOffloadByteQuantum
                    if representation["kind"] == "base64"
                    else self._connection.imageOffloadByteQuantum),
                "countQuantum": self._connection.imageOffloadCountQuantum,
            }
            if resolve_access is not None:
                images["resolveImageAccess"] = resolve_access
            try:
                projected, image_parts = await resolve_image_parts(messages, images)
            except FileResolutionFailure:
                representation = {"kind": "base64"}
                continue
            body = self._build_body(projected, tools, image_parts=image_parts)
            retry_state = {"retry": False}
            async for chunk in self._iter_request_chunks(
                    body, abort_event, request_files, retry_state,
                    files_beta=representation["kind"] == "file",
                    session_id=session_id, purpose=purpose):
                yield chunk
            if retry_state["retry"]:
                continue
            return

    def _prepare_request_images(self, messages, attachments, model) -> tuple[dict, dict]:
        """按路由目标为保守保留的规范化附件准备请求版本（上游 prepareRequestImages）。"""
        refs: dict = {}

        def collect(blocks) -> None:
            for block in blocks or []:
                if block.get("type") == "image" and block.get("offloaded") is not True:
                    ref = block["attachment"]
                    refs[str(ref["attachmentId"])] = ref

        for message in messages:
            collect(message.get("content") or [])
        version_map: dict = {}
        object_map: dict = {}
        for attachment_id, ref in refs.items():
            version = attachments.read_image_request(
                _ref_from_dict(ref), resolve_request_image_target(model, ref))
            version_map[attachment_id] = _version_dict(version)
            object_map[attachment_id] = version
        return version_map, object_map

    def _build_body(self, messages, tools, image_parts=None) -> dict:
        return serialize(
            messages, model=self._model, models=self._models,
            reasoning_effort=self._reasoning_effort, thinking=self._thinking,
            max_tokens=self._max_tokens,
            default_max_tokens=self._connection.maxTokens,
            tools=tools, image_parts=image_parts)

    async def _iter_chunks(self, body: dict, abort_event=None,
                           request_files=None, retry_state=None,
                           files_beta: bool = False,
                           session_id=None, purpose=None):
        """httpx 异步传输：POST /messages + 错误映射，逐行喂给 SSE 解析器。

        错误映射对齐上游 transport.ts；abort 置位经 _aiter_raced 抛
        StreamAborted，async-with 退出即关闭连接。

        request_files 非 None 时（image 路径）：stale file-id 响应先走有界
        invalidate 重试，retry_state["retry"]=True 由调用方重新序列化派发。
        """
        headers = {
            **attribution_headers(),
            "x-deepseek-harness-user-id": get_or_create_anonymous_user_id(),
            "content-type": "application/json",
            "accept": "text/event-stream",
            "anthropic-version": "2023-06-01",
            **self._auth_headers(),
        }
        # adapter.ts:129-130：会话身份头 + 压缩目的头（仅 compaction 映射）。
        if session_id is not None:
            headers["x-deepseek-harness-session-id"] = str(session_id)
        if purpose == "compaction":
            headers["x-deepseek-harness-compact"] = "1"
        betas = []
        if files_beta:
            betas.append(MESSAGES_FILES_BETA)
        if has_tool_changes(body):
            betas.append(MESSAGES_TOOL_CHANGES_BETA)
        if betas:
            headers["anthropic-beta"] = ",".join(betas)
        timeout = httpx.Timeout(self.CONNECT_TIMEOUT_S, read=self.READ_TIMEOUT_S)
        kwargs: dict[str, Any] = {"timeout": timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        endpoint = messages_api_root(self._base) + "/messages"
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                async with client.stream(
                    "POST", endpoint, json=body, headers=headers,
                ) as resp:
                    if resp.status_code >= 400:
                        raw_text = (await resp.aread()).decode("utf-8", "replace")
                        parsed = None
                        try:
                            parsed = json.loads(raw_text)
                        except ValueError:
                            # 网关不返回 JSON 时 HTTP 状态是权威。
                            pass
                        detail = _error_detail(parsed) or raw_text[:500]
                        if (request_files is not None and retry_state is not None
                                and await request_files.retry(detail)):
                            retry_state["retry"] = True
                            return
                        # 网关不返回 JSON 时以原始文本作为分类/文案兜底。
                        error_input = parsed if parsed is not None else {
                            "error": {"message": raw_text[:500]}}
                        failure = provider_error(error_input, resp.status_code, resp.headers)
                        if request_files is not None:
                            provider_message = (_error_message(parsed)
                                                or f"HTTP {resp.status_code}: {raw_text[:500]}")
                            failure = LlmFailure(
                                failure.code,
                                request_files.error_message(
                                    resp.status_code, provider_message, detail),
                                status=failure.status,
                                provider_retry_after_ms=failure.provider_retry_after_ms,
                                request_id=failure.request_id)
                        raise failure
                    async for chunk in self._parse_sse(resp.aiter_lines(), abort_event):
                        yield chunk
        except httpx.TimeoutException as e:
            raise LlmFailure(TIMEOUT, "请求超时") from e
        except httpx.HTTPError as e:
            raise LlmFailure(TRANSPORT, f"网络错误: {e}") from e

    async def _parse_sse(self, aiter_lines, abort_event=None):
        """SSE spec-strict 解析 + Messages 事件翻译（上游 sse.ts + translate.ts）。"""
        async for chunk in translate(parse_sse_frames(aiter_lines, abort_event)):
            yield chunk
