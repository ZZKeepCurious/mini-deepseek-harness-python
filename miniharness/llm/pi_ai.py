"""llm-pi-ai：多 provider 协议适配器（对齐上游 llm-pi-ai 的功能对标迁移）。

上游 `packages/llm/llm-pi-ai`：把外部 `@earendil-works/pi-ai` npm SDK 包成
`ctx.llm` 的多 provider 路由适配器（profile 目录 / OAuth / 模型发现）。mini 无
`ctx.llm` adapter-registry 基建（单适配器直连，agent-loop 不按 provider/model
路由），故按功能对标迁移到**可独立使用的协议适配器层**：

  * `PiAiAdapter(LlmAdapter)`：持有一组 provider profile（id / baseURL / api /
    apiKeyEnv / models），`stream()` 按**本次调用的 provider+model** 决议到对应
    profile 与协议（anthropic-messages / openai-completions / openai-responses），
    httpx 异步传输。
  * profile 配置 + 校验（api 闭集、baseURL 非空、apiKeyEnv 成对、模型容量）。
  * `resolve_model_info()` 按 provider+model 返回能力（provider/model/容量/
    input_modalities）。

载体差异（登记）：
  * 上游动态注册（settings 驱动 dormant→live）依赖 `ctx.llm` 注册表与
    loader volatile 配置——mini 无此基建，profile 以构造参数静态传入。
  * 上游 pi-ai SDK 目录数据（数百 provider 的预置 catalog）与 OAuth 登录流
    不移植（外部 SDK 数据面）；apiKeyEnv 凭据解析复用 `seams/credentials_local`
    或环境。
  * 模型发现（fetch /models）按需实现，非默认。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ..core.scope import Context
from .protocol import (
    LlmAdapter,
    LlmDiscoveredModel,
    LlmFailure,
    StreamChunk,
)
from .retry_policy import resolve_retry_policy

__all__ = [
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_INPUT",
    "DEFAULT_MAX_TOKENS",
    "PiAiAdapter",
    "SUPPORTED_PROTOCOLS",
    "resolve_profiles",
]

SUPPORTED_PROTOCOLS = ("openai-completions", "openai-responses", "anthropic-messages")

#: 缺省容量/模态回退（config.ts:65-80）。
DEFAULT_CONTEXT_WINDOW = 262_144
DEFAULT_MAX_TOKENS = 32_768
DEFAULT_INPUT = ("text",)

#: 已移除的预发布字段拒绝（config.ts:371-386）。
_REMOVED_FIELDS = {
    "provider": "llm-pi-ai: provider \"{route}\" sets \"provider\", which moved to "
                "the providers dict key",
    "maxRetries": "llm-pi-ai: provider \"{route}\" sets \"maxRetries\", which was "
                  "removed; compose agent recovery with dsh-llm-retry",
    "maxRetryDelayMs": "llm-pi-ai: provider \"{route}\" sets \"maxRetryDelayMs\", "
                       "which was removed; compose agent recovery with dsh-llm-retry",
}

_PROTOCOL_DEFAULT_PATHS = {
    "openai-completions": "/v1/chat/completions",
    "openai-responses": "/v1/responses",
    "anthropic-messages": "/v1/messages",
}


def _require_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"llm-pi-ai: {name} must be a positive safe integer")
    return value


def _require_positive_finite(name: str, value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or value <= 0 or value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"llm-pi-ai: {name} must be a positive finite number")
    return float(value)


@dataclass(frozen=True)
class PiAiProviderProfile:
    """一个 provider 路由的解析后 profile（config.ts:186-219 的 mini 子集）。"""

    id: str
    display_name: str
    api: str
    base_url: str
    api_key_env: str | None = None
    models: tuple = ()          # 显式模型表（替代内置目录）
    default_context_window: int = DEFAULT_CONTEXT_WINDOW
    default_max_tokens: int = DEFAULT_MAX_TOKENS
    default_input: tuple = DEFAULT_INPUT
    retry_policy: dict | None = None

    def __post_init__(self) -> None:
        if not self.default_input:
            raise ValueError(
                "llm-pi-ai: defaultInput must name at least one modality")
        for mod in self.default_input:
            if mod not in ("text", "image"):
                raise ValueError(f"llm-pi-ai: unknown modality {mod!r}")


@dataclass(frozen=True)
class PiAiModelProfile:
    """一个模型的 profile 条目（config.ts:303-324 的 mini 子集）。"""

    id: str
    name: str | None = None
    context_window: int | None = None
    max_tokens: int | None = None
    input: tuple = ()
    reasoning: Any = None


def _parse_model_profile(value: Any) -> PiAiModelProfile:
    if not isinstance(value, dict):
        raise ValueError("llm-pi-ai: model profile must be an object")
    model_id = value.get("id")
    if not isinstance(model_id, str) or model_id == "":
        raise ValueError("llm-pi-ai: model profile id must be a non-empty string")
    context_window = value.get("contextWindow")
    if context_window is not None:
        context_window = _require_int("model contextWindow", context_window)
    max_tokens = value.get("maxTokens")
    if max_tokens is not None:
        max_tokens = _require_int("model maxTokens", max_tokens)
    input_modalities = value.get("input") or ()
    reasoning = value.get("reasoningEfforts")
    return PiAiModelProfile(
        id=model_id, name=value.get("name"),
        context_window=context_window, max_tokens=max_tokens,
        input=tuple(input_modalities), reasoning=reasoning)


def _resolve_provider(route: str, value: Any) -> PiAiProviderProfile:
    """解析一个 provider profile（config.ts:410-509 的 mini 子集）。"""
    for removed, message in _REMOVED_FIELDS.items():
        if removed in value:
            raise ValueError(message.format(route=route))
    if not isinstance(value, dict):
        raise ValueError(f"llm-pi-ai: provider {route!r} profile must be an object")
    if not route:
        raise ValueError("llm-pi-ai: provider names must be non-empty")
    api = value.get("api")
    if api is None:
        # 缺省：单协议 anthropic-messages（无外部目录可探测）
        api = "anthropic-messages"
    if api not in SUPPORTED_PROTOCOLS:
        raise ValueError(
            f"llm-pi-ai: provider {route!r} names api {api!r}, which this build "
            "cannot serve; supported protocols are openai-completions, "
            "openai-responses, anthropic-messages")
    base_url = value.get("baseURL")
    if not isinstance(base_url, str) or base_url == "":
        raise ValueError(f"llm-pi-ai: provider {route!r} baseURL must be a non-empty string")
    display_name = value.get("displayName")
    if display_name is None:
        display_name = route
    if not isinstance(display_name, str) or display_name == "":
        raise ValueError(f"llm-pi-ai: provider {route!r} displayName must be non-empty")
    api_key_env = value.get("apiKeyEnv")
    if api_key_env is not None and (not isinstance(api_key_env, str) or api_key_env == ""):
        raise ValueError(f"llm-pi-ai: provider {route!r} apiKeyEnv must be a string")
    models = ()
    raw_models = value.get("models")
    if raw_models is not None:
        if not isinstance(raw_models, (list, tuple)):
            raise ValueError(f"llm-pi-ai: provider {route!r} models must be an array")
        models = tuple(_parse_model_profile(m) for m in raw_models)
    default_context = value.get("defaultContextWindow", DEFAULT_CONTEXT_WINDOW)
    default_max = value.get("defaultMaxTokens", DEFAULT_MAX_TOKENS)
    default_input = tuple(value.get("defaultInput") or DEFAULT_INPUT)
    retry = value.get("retryPolicy")
    return PiAiProviderProfile(
        id=route, display_name=display_name, api=api, base_url=base_url,
        api_key_env=api_key_env, models=models,
        default_context_window=_require_int("defaultContextWindow", default_context),
        default_max_tokens=_require_int("defaultMaxTokens", default_max),
        default_input=default_input,
        retry_policy=resolve_retry_policy(retry, f"llm-pi-ai:{route}"))


def resolve_profiles(providers: Any) -> dict[str, PiAiProviderProfile]:
    """解析 providers dict → {route: profile}（config.ts:410-509）。"""
    if isinstance(providers, (list, tuple)):
        raise ValueError(
            "llm-pi-ai: providers is now a dict keyed by provider route, not an "
            "array of profiles")
    if providers is None:
        return {}
    if not isinstance(providers, dict):
        raise ValueError("llm-pi-ai: providers must be a dict keyed by route")
    resolved: dict[str, PiAiProviderProfile] = {}
    for route, value in providers.items():
        resolved[route] = _resolve_provider(route, value)
    return resolved


class PiAiAdapter(LlmAdapter):
    """多 provider 协议适配器：`stream` 按 provider+model 决议到 profile/协议。

    构造注入 profile 表（`resolve_profiles` 产出）+ 可选的凭据解析器
    （`resolve(env_name) -> str`；缺省读环境）。provider 类属性为缺省路由
    （无显式路由时的回退）。
    """

    provider = "default"
    model = None

    def __init__(self, profiles: dict[str, PiAiProviderProfile],
                 credential_resolver: Any = None,
                 default_provider: str | None = None):
        self.profiles = profiles
        self._credential_resolver = credential_resolver
        if default_provider is not None:
            if default_provider not in profiles:
                raise ValueError(
                    f"llm-pi-ai: default provider {default_provider!r} is not configured")
            self.provider = default_provider
            self.model = self._default_model_for(default_provider)
        elif profiles:
            self.provider = next(iter(profiles))
            self.model = self._default_model_for(self.provider)

    def _default_model_for(self, route: str) -> str | None:
        profile = self.profiles.get(route)
        if profile is None:
            return None
        if profile.models:
            return profile.models[0].id
        return None

    # ---------- 能力 ----------

    def _model_for(self, route: str, model: str | None) -> PiAiModelProfile | None:
        profile = self.profiles.get(route)
        if profile is None:
            raise LlmFailure("NO_ADAPTER",
                             f"pi-ai adapter does not own provider {route!r}")
        if not profile.models:
            # 无显式模型表：以 route 的缺省能力回退
            return PiAiModelProfile(
                id=model or "", context_window=profile.default_context_window,
                max_tokens=profile.default_max_tokens, input=profile.default_input)
        for m in profile.models:
            if m.id == model:
                return m
        raise LlmFailure("UNKNOWN_MODEL",
                         f"pi-ai provider {route!r} has no configured model {model!r}")

    def resolve_model_info(self, provider: str | None = None,
                           model: str | None = None) -> dict:
        """模型能力声明（按 provider+model；缺省取实例路由）。"""
        route = provider or self.provider
        model = model or self.model
        m = self._model_for(route, model)
        modalities = list(m.input) if m.input else list(DEFAULT_INPUT)
        return {
            "provider": route,
            "model": model,
            "input_modalities": modalities,
            **({} if m.context_window is None
               else {"contextWindow": m.context_window}),
            **({} if m.max_tokens is None else {"defaultMaxTokens": m.max_tokens}),
        }

    def list_models(self, provider: str | None = None) -> list[LlmDiscoveredModel]:
        """profile 显式模型表 → 可发现模型（无表 → 空）。"""
        route = provider or self.provider
        profile = self.profiles.get(route)
        if profile is None:
            return []
        return [LlmDiscoveredModel(
            id=m.id, name=m.name, contextWindow=m.context_window,
            maxTokens=m.max_tokens) for m in profile.models]

    def resolve_api_key(self, profile: PiAiProviderProfile) -> str | None:
        """按 apiKeyEnv 解析凭据（credential resolver ?? 环境）。"""
        if profile.api_key_env is None:
            return None
        if self._credential_resolver is not None:
            value = self._credential_resolver(profile.api_key_env)
        else:
            value = os.environ.get(profile.api_key_env)
        if value is None or value == "":
            raise LlmFailure(
                "MISSING_CREDENTIAL",
                f"llm-pi-ai: no credential for provider route {profile.id!r}; its "
                f"profile resolves {profile.api_key_env!r}, which is not set")
        return value.strip()

    # ---------- 流式 ----------

    async def stream(self, messages: list[dict], tools: list[dict],
                     signal: Any | None = None) -> AsyncIterator[StreamChunk]:
        """按实例路由（provider/model）决议 profile 并走对应协议流。"""
        import asyncio

        provider = self.provider
        model = self.model
        profile = self.profiles.get(provider)
        if profile is None:
            raise LlmFailure("NO_ADAPTER",
                             f"pi-ai adapter does not own provider {provider!r}")
        api_key = self.resolve_api_key(profile)
        if profile.api == "anthropic-messages":
            url = _join_url(profile.base_url, "/v1/messages")
            body = _build_anthropic_body(profile, model, messages, tools)
            headers = _anthropic_headers(api_key)
        elif profile.api == "openai-completions":
            url = _join_url(profile.base_url, "/v1/chat/completions")
            body = _build_openai_body(profile, model, messages, tools)
            headers = _openai_headers(api_key)
        else:
            url = _join_url(profile.base_url, "/v1/responses")
            body = _build_openai_responses_body(profile, model, messages, tools)
            headers = _openai_headers(api_key)

        # httpx 异步传输：asyncio.ensure_future 在调用方循环内驱动，chunk 经
        # asyncio 队列桥回。
        queue: "asyncio.Queue" = asyncio.Queue()
        done = {"error": None}

        async def _consume() -> None:
            import httpx
            try:
                timeout = httpx.Timeout(connect=30.0, read=300.0)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream("POST", url, json=body, headers=headers) as resp:
                        if resp.status_code not in (200,):
                            text = (await resp.aread()).decode("utf-8", errors="replace")
                            await queue.put(_http_error_chunk(resp.status_code, text))
                            return
                        async for line in resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            payload = line[6:].strip()
                            if payload == "[DONE]":
                                break
                            try:
                                event = json.loads(payload)
                            except ValueError:
                                continue
                            for chunk in _translate_anthropic_event(event, model):
                                await queue.put(chunk)
                        await queue.put(_finish_chunk(model))
            except Exception as error:  # noqa: BLE001 - 传输错误折 finish error
                await queue.put(_finish_chunk(model, error))
            finally:
                await queue.put(None)

        task = asyncio.ensure_future(_consume())
        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, StreamChunk):
                yield item
            else:
                yield StreamChunk("finish", reason=item)

    # ---------- 装配 ----------

    @classmethod
    def from_providers(cls, providers: Any,
                       credential_resolver: Any = None,
                       default_provider: str | None = None) -> "PiAiAdapter":
        return cls(resolve_profiles(providers), credential_resolver, default_provider)


def _join_url(base: str, path: str) -> str:
    return base.rstrip("/") + path


def _openai_headers(api_key: str | None) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _anthropic_headers(api_key: str | None) -> dict:
    headers = {"Content-Type": "application/json",
               "anthropic-version": "2023-06-01"}
    if api_key is not None:
        headers["x-api-key"] = api_key
    return headers


def _build_anthropic_body(profile, model, messages, tools) -> dict:
    """harness 消息 → Anthropic messages 请求体（对齐 deepseek_messages 形态）。"""
    system: list[str] = []
    msgs: list[dict] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content") or []
        if role == "system":
            system.append(_text_of(content))
            continue
        if role == "assistant":
            blocks = []
            for block in content:
                if block.get("type") == "text":
                    blocks.append({"type": "text", "text": block.get("text", "")})
                elif block.get("type") == "reasoning":
                    blocks.append({"type": "thinking",
                                   "thinking": block.get("text", "")})
                elif block.get("type") == "tool-call":
                    blocks.append({"type": "tool_use", "id": block.get("id"),
                                   "name": block.get("name"),
                                   "input": _parse_args(block.get("arguments"))})
            msgs.append({"role": "assistant", "content": blocks})
            continue
        if role == "tool":
            tool_call_id = message.get("toolCallId")
            blocks = [{"type": "tool_result", "tool_use_id": tool_call_id,
                       "content": _text_of(content),
                       "is_error": bool(message.get("isError"))}]
            msgs.append({"role": "user", "content": blocks})
            continue
        # user
        text = _text_of(content)
        msgs.append({"role": "user", "content": text})
    body: dict = {
        "model": model,
        "max_tokens": profile.default_max_tokens,
        "messages": msgs,
    }
    if system:
        body["system"] = "\n".join(system)
    if tools:
        body["tools"] = [_anthropic_tool(t) for t in tools]
    return body


def _build_openai_body(profile, model, messages, tools) -> dict:
    body: dict = {"model": model, "messages": []}
    for message in messages:
        role = message.get("role")
        content = _text_of(message.get("content") or [])
        if role == "tool":
            body["messages"].append({"role": "tool", "tool_call_id":
                                     message.get("toolCallId"), "content": content})
            continue
        body["messages"].append({"role": role, "content": content})
    if tools:
        body["tools"] = [{"type": "function", "function": {
            "name": t.get("name"), "description": t.get("description", ""),
            "parameters": t.get("parameters") or {"type": "object"}}} for t in tools]
    return body


def _build_openai_responses_body(profile, model, messages, tools) -> dict:
    body: dict = {"model": model, "input": []}
    for message in messages:
        role = message.get("role")
        content = _text_of(message.get("content") or [])
        body["input"].append({"role": role, "content": content})
    if tools:
        body["tools"] = [{"type": "function", "name": t.get("name"),
                          "description": t.get("description", ""),
                          "parameters": t.get("parameters") or {"type": "object"}}
                         for t in tools]
    return body


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return str(content or "")


def _parse_args(arguments) -> Any:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except ValueError:
            return {}
    return arguments or {}


def _anthropic_tool(tool: dict) -> dict:
    return {"name": tool.get("name"), "description": tool.get("description", ""),
            "input_schema": tool.get("parameters") or {"type": "object"}}


def _translate_anthropic_event(event: dict, model: str) -> list:
    """Anthropic SSE 事件 → StreamChunk 流（message_start/content_block_*/
    message_delta/message_stop/ping/error）。"""
    event_type = event.get("type")
    chunks: list = []
    if event_type == "content_block_start":
        block = event.get("content_block") or {}
        if block.get("type") == "text":
            chunks.append(StreamChunk("block-start", index=0, blockType="text"))
        elif block.get("type") == "thinking":
            chunks.append(StreamChunk("block-start", index=0, blockType="reasoning"))
        elif block.get("type") == "tool_use":
            chunks.append(StreamChunk("tool-call-delta", index=0, id=block.get("id"),
                                      name=block.get("name"), argumentsDelta=""))
    elif event_type == "content_block_delta":
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            chunks.append(StreamChunk("text-delta", index=0, text=delta.get("text", "")))
        elif delta.get("type") == "thinking_delta":
            chunks.append(StreamChunk("reasoning-delta", index=0,
                                      text=delta.get("thinking", "")))
        elif delta.get("type") == "input_json_delta":
            chunks.append(StreamChunk("tool-call-delta", index=0, id="",
                                      argumentsDelta=delta.get("partial_json", "")))
    elif event_type == "content_block_stop":
        # 工具调用参数在 block-end 一次性收口
        chunks.append(StreamChunk("block-end", index=0, block={}))
    elif event_type == "message_delta":
        delta = event.get("delta") or {}
        stop_reason = delta.get("stop_reason")
        if stop_reason:
            chunks.append(_finish_chunk(model, stop_reason=stop_reason))
    return chunks


def _finish_chunk(model: str, error: BaseException | None = None,
                  stop_reason: str | None = None) -> StreamChunk:
    if error is not None:
        return StreamChunk("finish", reason={
            "kind": "error", "failure": {"message": str(error), "code": "SERVER"}})
    reason: dict
    if stop_reason == "max_tokens":
        reason = {"kind": "max-tokens"}
    elif stop_reason == "tool_use":
        reason = {"kind": "tool-calls"}
    elif stop_reason == "end_turn":
        reason = {"kind": "stop"}
    else:
        reason = {"kind": "stop"}
    return StreamChunk("finish", reason=reason)


def _http_error_chunk(status: int, text: str) -> StreamChunk:
    """HTTP 状态 → LlmFailure 码（deepseek_messages provider_error 同语义）。"""
    if status in (401, 403):
        code = "AUTH"
    elif status == 429 or "rate" in text.lower():
        code = "RATE_LIMIT"
    elif status >= 500:
        code = "SERVER"
    else:
        code = "INVALID_REQUEST"
    return StreamChunk("finish", reason={
        "kind": "error",
        "failure": {"message": f"HTTP {status}: {text[:500]}", "code": code},
    })


def install_pi_ai(ctx: Context, providers: Any, default_provider: str | None = None,
                  credential_resolver: Any = None) -> PiAiAdapter:
    """装配一个 `ctx.get('llm')` 可用的 PiAiAdapter（幂等语义由调用方选择）。

    @returns 按 providers 决议的适配器实例（不登记到 ctx 服务——mini 无
    llm 注册表；调用方直接持有传给 agent-loop）。
    """
    return PiAiAdapter.from_providers(providers, credential_resolver,
                                      default_provider)