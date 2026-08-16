"""第 4 章：模型请求重试策略 —— provider 属地的策略解析与默认。

对应 dsh 真实源码：packages/llm/llm/src/retry-policy.ts。

上游语义（已核实，retry-policy.ts）：
  * 两种模式：normal（maxRetries + retryableCodes 白名单）/ always（无限重试）。
  * 默认值：maxRetries 2、initialDelayMs 500、maxDelayMs 10000、
    jitterRatio 0.1、retryableCodes [EMPTY_RESPONSE, RATE_LIMIT, SERVER,
    TIMEOUT, TRANSPORT]（上下文溢出、认证、配额、畸形凭据故意不在默认
    白名单——重试它们只会以相同方式失败）。
  * 校验严格：未知键拒绝；backoff 的 initial/max 必须正有限且 ≤
    MAX_TIMER_DELAY_MS、initial ≤ max、jitter ∈ [0,1]；normal 的
    maxRetries 非负安全整数、retryableCodes 非空/无重复/非空字符串。
  * 解析结果冻结（不可变），供 provider 注册时捕获（每次请求决议时
    不可刷新）。
"""
from __future__ import annotations

import json
from types import MappingProxyType
from typing import Any

# Node setTimeout 上限（上游 @deepseek-ai/dsh-timeout MAX_TIMER_DELAY_MS）
MAX_TIMER_DELAY_MS = 2_147_483_647

DEFAULT_MAX_RETRIES = 2
DEFAULT_INITIAL_DELAY_MS = 500
DEFAULT_MAX_DELAY_MS = 10_000
DEFAULT_JITTER_RATIO = 0.1
DEFAULT_RETRYABLE_CODES = (
    "EMPTY_RESPONSE",
    "RATE_LIMIT",
    "SERVER",
    "TIMEOUT",
    "TRANSPORT",
)

# LlmFailure code 常量（llm/src/error.ts 同名词典）
CONTEXT_WINDOW_EXCEEDED = "CONTEXT_WINDOW_EXCEEDED"
EMPTY_RESPONSE = "EMPTY_RESPONSE"

_NORMAL_KEYS = frozenset({"mode", "maxRetries", "retryableCodes", "backoff"})
_ALWAYS_KEYS = frozenset({"mode", "backoff"})
_BACKOFF_KEYS = frozenset({"initialDelayMs", "maxDelayMs", "jitterRatio"})


def _validate_keys(value: dict, allowed: frozenset, path: str) -> None:
    for key in value:
        if key not in allowed:
            raise ValueError(f"{path}: unknown key {key!r}")


def _resolve_backoff(config: dict | None, path: str) -> dict:
    if config is not None:
        _validate_keys(config, _BACKOFF_KEYS, path)
    initial = config.get("initialDelayMs", DEFAULT_INITIAL_DELAY_MS) if config else DEFAULT_INITIAL_DELAY_MS
    maximum = config.get("maxDelayMs", DEFAULT_MAX_DELAY_MS) if config else DEFAULT_MAX_DELAY_MS
    jitter = config.get("jitterRatio", DEFAULT_JITTER_RATIO) if config else DEFAULT_JITTER_RATIO
    if not (isinstance(initial, (int, float)) and 0 < initial <= MAX_TIMER_DELAY_MS):
        raise ValueError(f"{path}.initialDelayMs must be a positive finite number no greater than {MAX_TIMER_DELAY_MS}")
    if not (isinstance(maximum, (int, float)) and 0 < maximum <= MAX_TIMER_DELAY_MS):
        raise ValueError(f"{path}.maxDelayMs must be a positive finite number no greater than {MAX_TIMER_DELAY_MS}")
    if initial > maximum:
        raise ValueError(f"{path}.initialDelayMs must be less than or equal to maxDelayMs")
    if not (isinstance(jitter, (int, float)) and 0 <= jitter <= 1):
        raise ValueError(f"{path}.jitterRatio must be between 0 and 1")
    return {
        "initialDelayMs": initial, "maxDelayMs": maximum, "jitterRatio": jitter,
    }


def _freeze(policy: dict) -> dict:
    """冻结策略：顶层只读（MappingProxyType），retryableCodes 为元组。

    对齐上游 ResolvedRetryPolicy：backoff 展开平铺在顶层
    （resolveBackoff 的 ... 展开），无嵌套 backoff 键。
    """
    frozen = dict(policy)
    if "retryableCodes" in frozen:
        frozen["retryableCodes"] = tuple(frozen["retryableCodes"])
    return MappingProxyType(frozen)


def resolve_retry_policy(config: dict | None = None, path: str = "retryPolicy") -> dict:
    """校验、补默认并冻结 provider 属地的重试策略。

    与上游 resolveRetryPolicy 同语义：config 省略 → normal 默认；
    返回不可变策略（嵌套只读，MappingProxyType + 元组），供 provider
    注册时捕获（每次请求决议时不可刷新）。
    """
    if config is None:
        return _freeze({
            "mode": "normal",
            "maxRetries": DEFAULT_MAX_RETRIES,
            "retryableCodes": list(DEFAULT_RETRYABLE_CODES),
            **_resolve_backoff(None, f"{path}.backoff"),
        })
    mode = config.get("mode")
    if mode == "normal":
        _validate_keys(config, _NORMAL_KEYS, path)
        max_retries = config.get("maxRetries", DEFAULT_MAX_RETRIES)
        codes = config.get("retryableCodes", list(DEFAULT_RETRYABLE_CODES))
        if not (isinstance(max_retries, int) and max_retries >= 0):
            raise ValueError(f"{path}.maxRetries must be a non-negative safe integer")
        if len(codes) == 0:
            raise ValueError(f"{path}.retryableCodes must not be empty")
        if any(not isinstance(code, str) or len(code) == 0 for code in codes):
            raise ValueError(f"{path}.retryableCodes must contain only non-empty strings")
        if len(set(codes)) != len(codes):
            raise ValueError(f"{path}.retryableCodes must not contain duplicates")
        return _freeze({
            "mode": "normal",
            "maxRetries": max_retries,
            "retryableCodes": list(codes),
            **_resolve_backoff(config.get("backoff"), f"{path}.backoff"),
        })
    if mode == "always":
        _validate_keys(config, _ALWAYS_KEYS, path)
        return _freeze({
            "mode": "always",
            **_resolve_backoff(config.get("backoff"), f"{path}.backoff"),
        })
    raise ValueError(f'{path}.mode must be "normal" or "always"')


def retry_policy_key(policy: dict) -> str:
    """策略的规范化身份键（上游 retryPolicyKey 同构）。

    同 turn/step/provider 下用同一 policyKey 关联的重试计数共享同一
    retryId 与计数上限。
    """
    if policy["mode"] == "always":
        return json.dumps([policy["mode"], policy["initialDelayMs"],
                           policy["maxDelayMs"], policy["jitterRatio"]])
    return json.dumps([policy["mode"], policy["maxRetries"],
                       sorted(policy["retryableCodes"]),
                       policy["initialDelayMs"], policy["maxDelayMs"],
                       policy["jitterRatio"]])