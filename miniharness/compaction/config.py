"""压缩后端配置：加载期校验 + 按 provider/model 精确路由的策略合并 + 阈值/保留预算决议。

上游对照：packages/compaction/compaction-basic/src/config.ts（resolveConfig /
resolveModelPolicies / resolveTargetPolicy / resolveCompactSpec）。

配置键与默认值对齐：thresholdRatio 0.8 / headroomTokens 65536 / retainRatio 0.16 /
maxTokens（缺省取 headroomTokens，不再固定 8192）/ compactionRetries 1 /
maxOverflowRetries 1 / auto True。retainTokens 与 retainRatio 互斥。modelPolicies
为可选的 provider/model 精确覆盖表——每个条目可单独覆盖 thresholdRatio /
headroomTokens / retainRatio / retainTokens 等，与全局默认策略合并后走同一校验管线。

resolve_spec(policy, contextWindow, reservedCompletionTokens) 对齐上游：
messageBudget = contextWindow - reservedCompletionTokens；
pressureBudget = messageBudget - headroomTokens；
threshold = floor(min(contextWindow × thresholdRatio, pressureBudget))；
retain = 显式 retainTokens 或 floor(messageBudget × retainRatio)。
任一步预算非正抛 TargetPressureConfigError（fail-closed）。
"""
from __future__ import annotations

__all__ = ["DEFAULT_HEADROOM_TOKENS", "DEFAULT_RETAIN_RATIO", "DEFAULT_THRESHOLD_RATIO",
           "TargetPressureConfigError", "resolve_config", "resolve_spec",
           "resolve_target_policy"]

DEFAULT_THRESHOLD_RATIO = 0.8
DEFAULT_RETAIN_RATIO = 0.16
DEFAULT_HEADROOM_TOKENS = 65_536

POLICY_KEYS = frozenset({
    "thresholdRatio", "headroomTokens", "retainRatio", "retainTokens", "maxTokens",
    "compactionRetries", "maxOverflowRetries", "auto", "modelPolicies",
})


class TargetPressureConfigError(Exception):
    """目标模型上下文容量配置失败（上游 config.ts TargetPressureConfigError）。"""

    def __init__(self, target_key: str, message: str):
        super().__init__(message)
        self.target_key = target_key


def resolve_config(config: dict | None = None) -> dict:
    """解析并校验默认策略（含可选 modelPolicies 覆盖表；detached 深拷贝）。

    modelPolicies 为可选的 Provider/Model 精确覆盖列表——每个条目必须带
    provider（非空字符串）和 model（非空字符串），可单独覆盖 thresholdRatio /
    headroomTokens / retainRatio / retainTokens 等；同一 provider/model 不可重复。
    加载期逐条校验 ratio 约束（同全局默认）。
    """
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
    headroom_tokens = config.get("headroomTokens", DEFAULT_HEADROOM_TOKENS)
    _assert_non_negative_int("headroomTokens", headroom_tokens)
    max_tokens_explicit = "maxTokens" in config
    max_tokens = config.get("maxTokens", headroom_tokens)
    _assert_positive_int("maxTokens", max_tokens)
    compaction_retries = config.get("compactionRetries", 1)
    _assert_non_negative_int("compactionRetries", compaction_retries)
    max_overflow_retries = config.get("maxOverflowRetries", 1)
    _assert_non_negative_int("maxOverflowRetries", max_overflow_retries)
    auto = config.get("auto", True)
    if not isinstance(auto, bool):
        raise ValueError("BasicCompactionConfig: auto must be a boolean")
    model_policies = _resolve_model_policies(
        config.get("modelPolicies"), threshold, retain_ratio, retain_tokens,
        headroom_tokens, max_tokens, max_tokens_explicit,
    )
    return {
        "thresholdRatio": threshold,
        "headroomTokens": headroom_tokens,
        "retainRatio": retain_ratio,
        "retainTokens": retain_tokens,
        "maxTokens": max_tokens,
        "compactionRetries": compaction_retries,
        "maxOverflowRetries": max_overflow_retries,
        "auto": auto,
        "modelPolicies": model_policies,
    }


def resolve_spec(policy: dict, context_window: int,
                 reserved_completion_tokens: int = 0) -> dict:
    """按模型上下文容量与保留的输出 token 换算具体预算（上游 resolveCompactSpec）。

    @param policy - 合并后的精确路由策略（须含 headroomTokens）。
    @param context_window - 适配器声明的正容量。
    @param reserved_completion_tokens - 一次路由请求预留的输出 token。
    """
    target_key = policy.get("target", "?")
    if not isinstance(context_window, int) or isinstance(context_window, bool) \
            or context_window <= 0:
        raise TargetPressureConfigError(
            target_key,
            f"BasicCompactionConfig: contextWindow ({context_window}) must be a positive integer",
        )
    if not isinstance(reserved_completion_tokens, int) \
            or isinstance(reserved_completion_tokens, bool) \
            or reserved_completion_tokens < 0:
        raise TargetPressureConfigError(
            target_key,
            f"BasicCompactionConfig: reservedCompletionTokens ({reserved_completion_tokens}) "
            "must be a non-negative integer",
        )
    message_budget = context_window - reserved_completion_tokens
    if message_budget <= 0:
        raise TargetPressureConfigError(
            target_key,
            f"compaction-basic: {target_key} reserves {reserved_completion_tokens} completion "
            f"tokens of its {context_window}-token context window, leaving no message budget; "
            "configure the adapter model's contextWindow above the effective request maxTokens",
        )
    pressure_budget = message_budget - policy["headroomTokens"]
    if pressure_budget <= 0:
        raise TargetPressureConfigError(
            target_key,
            f"compaction-basic: {target_key} reserves {reserved_completion_tokens} completion "
            f"tokens and {policy['headroomTokens']} headroom tokens of its {context_window}-token "
            "context window, leaving no pressure budget; reduce the effective request maxTokens "
            "or compaction headroomTokens, or configure a larger adapter model contextWindow",
        )
    threshold_tokens = int(min(context_window * policy["thresholdRatio"], pressure_budget))
    retain_tokens = policy.get("retainTokens")
    if retain_tokens is None:
        retain_tokens = int(message_budget * policy["retainRatio"])
    if retain_tokens >= threshold_tokens:
        raise TargetPressureConfigError(
            target_key,
            f"BasicCompactionConfig: {target_key} retainTokens ({retain_tokens}) must be less "
            f"than threshold tokens {threshold_tokens}",
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
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"BasicCompactionConfig: {name} must be a positive integer")


def _assert_non_negative_int(name: str, value) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"BasicCompactionConfig: {name} must be a non-negative integer")


def _resolve_retention(entry: dict, fallback_retain_ratio: float | None,
                       fallback_retain_tokens: int | None) -> tuple[float | None, int | None]:
    """解析单条策略的 retention（retainRatio/retainTokens 互斥）并返回
    （retain_ratio, retain_tokens）中恰好一个非 None 的结果。"""
    retain_ratio = entry.get("retainRatio")
    retain_tokens = entry.get("retainTokens")
    if retain_ratio is not None and retain_tokens is not None:
        raise ValueError(
            "BasicCompactionConfig: retainRatio and retainTokens are mutually exclusive"
        )
    if retain_ratio is not None:
        return retain_ratio, None
    if retain_tokens is not None:
        return None, retain_tokens
    return fallback_retain_ratio, fallback_retain_tokens


def _validate_ratio_retention(name: str, threshold_ratio: float,
                              retain_ratio: float | None,
                              retain_tokens: int | None,
                              threshold_tokens: float | None) -> None:
    """校验单条策略的 ratio/retention 约束。"""
    if retain_ratio is not None:
        if retain_ratio >= threshold_ratio:
            raise ValueError(
                f"{name}: retainRatio ({retain_ratio}) must be less than "
                f"the resolved thresholdRatio ({threshold_ratio})"
            )
    elif retain_tokens is not None and threshold_tokens is not None:
        if retain_tokens >= threshold_tokens:
            raise ValueError(
                f"{name}: retainTokens ({retain_tokens}) must be less than "
                f"threshold tokens ({threshold_tokens})"
            )


def _resolve_model_policies(configured: object,
                            global_threshold: float,
                            global_retain_ratio: float | None,
                            global_retain_tokens: int | None,
                            global_headroom: int,
                            global_max_tokens: int,
                            global_max_tokens_explicit: bool) -> list[dict]:
    """解析并校验 modelPolicies 数组（对齐 resolveModelPolicies）。

    每条必须带 provider/model 非空字符串，不可重复；各条的 ratio/retention
    按全局默认做独立校验（加载期 fail-closed）。条目未给 maxTokens 且全局未
    显式设置时，其 headroomTokens 作为该条目的生成上限（对齐上游）。
    """
    if configured is None:
        return []
    if not isinstance(configured, list):
        raise ValueError("BasicCompactionConfig: modelPolicies must be an array")
    seen: set[str] = set()
    result: list[dict] = []
    for index, source in enumerate(configured):
        name = f"BasicCompactionConfig: modelPolicies[{index}]"
        if not isinstance(source, dict) or not source:
            raise ValueError(f"{name} must be a non-empty object")
        provider = source.get("provider")
        model = source.get("model")
        if not isinstance(provider, str) or not provider:
            raise ValueError(f"{name}: provider must be a non-empty string")
        if not isinstance(model, str) or not model:
            raise ValueError(f"{name}: model must be a non-empty string")
        key = f"{provider}\x00{model}"
        if key in seen:
            raise ValueError(
                f"BasicCompactionConfig: duplicate model policy for {provider}/{model}"
            )
        seen.add(key)
        entry_threshold = source.get("thresholdRatio", global_threshold)
        if "thresholdRatio" in source:
            _assert_ratio(f"{name}.thresholdRatio", entry_threshold)
        headroom = source.get("headroomTokens")
        if headroom is not None:
            _assert_non_negative_int(f"{name}.headroomTokens", headroom)
        retain_ratio, retain_tokens = _resolve_retention(
            source, global_retain_ratio, global_retain_tokens,
        )
        entry_max = source.get("maxTokens")
        if entry_max is None and not global_max_tokens_explicit and headroom is not None:
            entry_max = headroom
        _assert_positive_int(
            f"{name}.maxTokens",
            entry_max if entry_max is not None else global_max_tokens,
        )
        compaction_retries = source.get("compactionRetries")
        if compaction_retries is not None:
            _assert_non_negative_int(f"{name}.compactionRetries", compaction_retries)
        max_overflow_retries = source.get("maxOverflowRetries")
        if max_overflow_retries is not None:
            _assert_non_negative_int(f"{name}.maxOverflowRetries", max_overflow_retries)
        # 加载期 ratio 约束校验：用 contextWindow=1000 做一次缩放检查
        _validate_ratio_retention(
            name, entry_threshold, retain_ratio, retain_tokens,
            1000 * entry_threshold if retain_tokens is None else None,
        )
        entry = {"provider": provider, "model": model, "thresholdRatio": entry_threshold}
        if headroom is not None:
            entry["headroomTokens"] = headroom
        if retain_ratio is not None:
            entry["retainRatio"] = retain_ratio
        if retain_tokens is not None:
            entry["retainTokens"] = retain_tokens
        if entry_max is not None:
            entry["maxTokens"] = entry_max
        if compaction_retries is not None:
            entry["compactionRetries"] = compaction_retries
        if max_overflow_retries is not None:
            entry["maxOverflowRetries"] = max_overflow_retries
        for key in ("summarizationProvider", "summarizationModel"):
            if source.get(key) is not None:
                entry[key] = source[key]
        result.append(entry)
    return result


def resolve_target_policy(config: dict, target: dict) -> dict:
    """合并全局策略与 modelPolicies 精确覆盖（对齐 resolveTargetPolicy）。

    target 必须带 provider 和 model。linear scan 匹配 modelPolicies 中
    首条 provider+model 命中的条目——覆盖字段优先，缺省字段继承全局。
    """
    provider = target.get("provider", "")
    model = target.get("model", "")
    override = None
    for policy in config.get("modelPolicies", []):
        if policy["provider"] == provider and policy["model"] == model:
            override = policy
            break
    src = override if override is not None else config
    inherit_retain_ratio = config["retainRatio"]
    inherit_retain_tokens = config.get("retainTokens")
    retain_ratio, retain_tokens = _resolve_retention(
        src, inherit_retain_ratio, inherit_retain_tokens,
    )
    result = {
        "thresholdRatio": src.get("thresholdRatio", config["thresholdRatio"]),
        "headroomTokens": src.get("headroomTokens", config["headroomTokens"]),
        "retainRatio": retain_ratio,
        "retainTokens": retain_tokens,
        "maxTokens": src.get("maxTokens", config["maxTokens"]),
        "compactionRetries": src.get("compactionRetries", config["compactionRetries"]),
        "maxOverflowRetries": src.get("maxOverflowRetries", config["maxOverflowRetries"]),
        "target": f"{provider}/{model}",
    }
    for key in ("summarizationProvider", "summarizationModel"):
        if src.get(key) is not None:
            result[key] = src[key]
    return result
