"""压缩后端配置：加载期校验 + 阈值/保留预算决议。

上游对照：packages/compaction/compaction-basic/src/config.ts（resolveConfig /
resolveTargetPolicy / resolveCompactSpec）。mini 简化：无 modelPolicies
（按 provider/model 精确路由的覆盖表），默认策略即全部；配置键与默认值对齐：
thresholdRatio 0.8 / retainRatio 0.16 / maxTokens 8192 / compactionRetries 1 /
maxOverflowRetries 1 / auto True。retainTokens 与 retainRatio 互斥。
"""
from __future__ import annotations

__all__ = ["DEFAULT_RETAIN_RATIO", "DEFAULT_THRESHOLD_RATIO", "TargetPressureConfigError",
           "resolve_config", "resolve_spec"]

DEFAULT_THRESHOLD_RATIO = 0.8
DEFAULT_RETAIN_RATIO = 0.16

POLICY_KEYS = frozenset({
    "thresholdRatio", "retainRatio", "retainTokens", "maxTokens",
    "compactionRetries", "maxOverflowRetries", "auto",
})


class TargetPressureConfigError(Exception):
    """目标模型上下文容量配置失败（上游 config.ts TargetPressureConfigError）。"""

    def __init__(self, target_key: str, message: str):
        super().__init__(message)
        self.target_key = target_key


def resolve_config(config: dict | None = None) -> dict:
    """解析并校验默认策略（detached，调用方不可变约定由调用方遵守）。"""
    config = dict(config or {})
    unknown = set(config) - POLICY_KEYS
    if unknown:
        raise ValueError(f"BasicCompactionConfig: unknown key {sorted(unknown)}")
    threshold = config.get("thresholdRatio", DEFAULT_THRESHOLD_RATIO)
    _assert_ratio("thresholdRatio", threshold)
    retain_ratio = config.get("retainRatio")
    retain_tokens = config.get("retainTokens")
    if retain_ratio is not None and retain_tokens is not None:
        raise ValueError("BasicCompactionConfig: retainRatio and retainTokens are mutually exclusive")
    if retain_ratio is not None:
        _assert_ratio("retainRatio", retain_ratio)
        if retain_ratio >= threshold:
            raise ValueError(
                f"BasicCompactionConfig: retainRatio ({retain_ratio}) must be less than "
                f"the resolved thresholdRatio ({threshold})"
            )
    elif retain_tokens is not None:
        _assert_non_negative_int("retainTokens", retain_tokens)
    else:
        retain_ratio = DEFAULT_RETAIN_RATIO
    max_tokens = config.get("maxTokens", 8192)
    _assert_positive_int("maxTokens", max_tokens)
    compaction_retries = config.get("compactionRetries", 1)
    _assert_non_negative_int("compactionRetries", compaction_retries)
    max_overflow_retries = config.get("maxOverflowRetries", 1)
    _assert_non_negative_int("maxOverflowRetries", max_overflow_retries)
    auto = config.get("auto", True)
    if not isinstance(auto, bool):
        raise ValueError("BasicCompactionConfig: auto must be a boolean")
    return {
        "thresholdRatio": threshold,
        "retainRatio": retain_ratio,
        "retainTokens": retain_tokens,
        "maxTokens": max_tokens,
        "compactionRetries": compaction_retries,
        "maxOverflowRetries": max_overflow_retries,
        "auto": auto,
    }


def resolve_spec(policy: dict, context_window: int) -> dict:
    """按模型上下文容量换算具体 token 预算（上游 resolveCompactSpec）。

    thresholdTokens = floor(contextWindow × thresholdRatio)；
    retainTokens = 显式值或 floor(contextWindow × retainRatio)。
    """
    target_key = policy.get("target", "?")
    if not isinstance(context_window, int) or context_window <= 0:
        raise TargetPressureConfigError(
            target_key,
            f"BasicCompactionConfig: contextWindow ({context_window}) must be a positive integer",
        )
    threshold_tokens = int(context_window * policy["thresholdRatio"])
    retain_tokens = policy.get("retainTokens")
    if retain_tokens is None:
        retain_tokens = int(context_window * policy["retainRatio"])
    if retain_tokens >= threshold_tokens:
        raise TargetPressureConfigError(
            target_key,
            f"BasicCompactionConfig: retainTokens ({retain_tokens}) must be less than "
            f"threshold tokens ({threshold_tokens})",
        )
    return {
        "contextWindow": context_window,
        "thresholdTokens": threshold_tokens,
        "retainTokens": retain_tokens,
        "maxTokens": policy["maxTokens"],
        "compactionRetries": policy["compactionRetries"],
        "maxOverflowRetries": policy["maxOverflowRetries"],
    }


def _assert_ratio(name: str, value) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or value <= 0 or value > 1:
        raise ValueError(f"BasicCompactionConfig: {name} must be a number in (0, 1]")


def _assert_positive_int(name: str, value) -> None:
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"BasicCompactionConfig: {name} must be a positive integer")


def _assert_non_negative_int(name: str, value) -> None:
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"BasicCompactionConfig: {name} must be a non-negative integer")