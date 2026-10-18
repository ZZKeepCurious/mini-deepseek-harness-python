"""第 7 章：DeepSeek 请求扩展 —— 官方请求顶层字段的注册表（上游
packages/llm/deepseek-llm-api-extensions + llm-deepseek/src/request-extensions.ts）。

每个扩展 provider 独立拥有一个顶层请求字段（如 `dsh_plugin_packages`）。请求
发出前逐字段准备：准备抛错或值无法 JSON 克隆的 provider 从本次请求省略（每
字段仅告警一次）；准备成功但提交在 HTTP 2xx 之后，可能失败且不使请求失败。

mini 载体：注册表为普通对象（上游为 cordis Service），`prepare` 为同步调用
（上游异步 Promise/AbortSignal）；`signal.throwIfAborted()` 的取消检查由调用方
在发起请求前完成，故本模块无 signal 参数（载体差异，见 verified-diffs）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _clone_json(value: Any) -> Any:
    """克隆并校验一个值可 JSON 往返（上游 structuredClone + freezeJson）。

    不可表示（非有限数、非对象等）→ 抛 TypeError，调用方按准备失败省略该字段。
    """
    return json.loads(json.dumps(value, allow_nan=False))


class ExtensionRegistry:
    """官方 DeepSeek 请求顶层字段的注册表（上游 DeepSeekLlmApiExtensionRegistry）。

    register 字段名必须是非空、无首尾空白的字符串，且不可重复注册；注销由返回
    的 disposer 完成。prepare 逐字段调用 provider.prepare(request)，返回
    {"fields": {...}, "accept": callable}：字段值克隆后回传（上游 freezeJson 的
    mini 载体是深拷贝隔离——调用方不再持有可写别名），accept 汇总所有 provider
    的提交回调。
    """

    def __init__(self) -> None:
        self._providers: dict[str, Any] = {}
        # 已告警过的字段：后续失败保持静默（上游 warnedFields）。
        self._warned_fields: set[str] = set()

    def register(self, field: str, provider: Any) -> Callable[[], None]:
        """注册某顶层字段的唯一 provider；返回释放该字段的 disposer。"""
        if not isinstance(field, str) or field.strip() != field or field == "":
            raise ValueError(
                "deepseek-llm-api-extensions: field must be a non-blank trimmed string")
        if field in self._providers:
            raise ValueError(
                f"deepseek-llm-api-extensions: field {json.dumps(field)} is already registered")
        self._providers[field] = provider

        def dispose() -> None:
            self._providers.pop(field, None)

        return dispose

    def prepare(self, request: dict) -> dict:
        """按当前注册逐字段准备一次请求（上游 prepare）。

        @param request - {"body", "signal", "sessionId", "purpose"} 的请求事实快照。
        @returns {"fields": {...}, "accept": callable}；字段值经 JSON 往返深拷贝
          隔离，provider 不保留对外发请求的可变别名。
        """
        fields: dict[str, Any] = {}
        callbacks: list[Callable[[], Any]] = []
        for field, provider in list(self._providers.items()):
            result = self._prepare_field(field, provider, request)
            if result is None:
                continue
            value, accept = result
            fields[field] = value
            if accept is not None:
                callbacks.append(accept)

        def accept() -> None:
            for callback in callbacks:
                callback()

        return {"fields": fields, "accept": accept}

    def _prepare_field(self, field: str, provider: Any, request: dict):
        try:
            prepare = provider["prepare"] if isinstance(provider, dict) else provider.prepare
            result = prepare(request)
            if result is None:
                return None
            return _clone_json(result["value"]), result.get("accept")
        except Exception as error:
            if field not in self._warned_fields:
                self._warned_fields.add(field)
                logger.warning(
                    "deepseek-llm-api-extensions: omitting field %s from this request "
                    "because its preparation failed: %r", json.dumps(field), error)
            return None


def prepare_request_extensions(body: dict, request: dict,
                               registry: "ExtensionRegistry | None",
                               on_omitted: Callable[[list, Exception], None] | None = None,
                               on_unaccepted: Callable[[Exception], None] | None = None):
    """准备并合并扩展字段，返回 (payload, accept)（上游 prepareRequestExtensions）。

    扩展准备失败 → REQUEST_EXTENSION（带原因）；与基础请求字段冲突 → REQUEST_EXTENSION；
    合并体无法 JSON 序列化 → 退回基础请求体单独发送 + `on_omitted(字段名, 失败)`，accept 为
    no-op（贡献方在后续请求重发未接受状态）；基础请求体本身不可序列化 → 原样抛出；
    合并成功但 accept 失败由调用方经 on_unaccepted 上报且不使请求失败。

    registry 为 None 时返回 (body, no-op)。
    """
    from .protocol import LlmFailure, REQUEST_EXTENSION

    if registry is None:
        return body, (lambda: None)
    try:
        extensions = registry.prepare({**request, "body": body})
    except Exception as error:
        raise LlmFailure(
            REQUEST_EXTENSION,
            f"DeepSeek request extension preparation failed: {error}") from error
    fields = extensions["fields"]
    for field in fields:
        if field in body:
            raise LlmFailure(
                REQUEST_EXTENSION,
                f"DeepSeek request extension field {json.dumps(field)} collides with the base request")
    payload = {**body, **fields}
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as error:
        # 合并体不可序列化：退回基础请求体（上游 request-extensions.ts:40-48）。
        json.dumps(body, allow_nan=False)
        if on_omitted is not None:
            on_omitted(list(fields), error)
        return body, (lambda: None)

    def accept() -> None:
        try:
            extensions["accept"]()
        except Exception as error:
            if on_unaccepted is not None:
                on_unaccepted(error)

    return payload, accept


__all__ = [
    "ExtensionRegistry",
    "prepare_request_extensions",
]
