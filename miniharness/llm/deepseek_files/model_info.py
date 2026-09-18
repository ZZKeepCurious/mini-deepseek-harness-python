"""协议无关的模型能力与推理档位解析。

对应 dsh 真实源码：packages/llm/llm-deepseek/src/common/model-info.ts。

返回结构为 mini 的 dict 契约（`resolve_model_info`）：
  * provider / model / name / description?
  * input_modalities —— 缺省 ['text']；
  * context = {contextWindow}、defaultMaxTokens；
  * reasoning = {efforts:[{id,name,description?}], defaultEffort?}（thinking 关闭时仅 off）；
  * systemPromptUpdate（catalog 声明时）。
"""
from __future__ import annotations

__all__ = [
    "REASONING_EFFORTS",
    "catalog_model_info",
    "model_info",
]

_OFF = {
    "id": "off",
    "name": "Off",
    "description": "Use for simple tasks that do not need reasoning.",
}
REASONING_EFFORTS = (
    _OFF,
    {
        "id": "low",
        "name": "Low",
        "description": "Prefer for routine or latency-sensitive tasks.",
    },
    {
        "id": "high",
        "name": "High",
        "description": "The default balance for most tasks.",
    },
    {
        "id": "max",
        "name": "Max",
        "description": "Reserve for the hardest quality-first tasks.",
    },
)
_OFF_ONLY_REASONING_EFFORTS = (_OFF,)

_EFFORT_DEFAULTS = {"off": "off", "low": "low", "max": "max"}


def catalog_model_info(provider: str, model) -> dict:
    """广告一个 catalog 条目（上游 catalogModelInfo）。"""
    info: dict = {
        "provider": provider,
        "model": model.id,
        "name": model.name or model.id,
        "input_modalities": list(model.inputModalities or ("text",)),
    }
    if model.description is not None:
        info["description"] = model.description
    return info


def model_info(connection, provider: str, model: str) -> dict:
    """按一代配置解析模型能力（上游 modelInfo）。"""
    configured = next(
        (entry for entry in connection.models if entry.id == model), None)
    if configured is None:
        # 未编目 endpoint 安全地按 text-only 处理：声明未经验证的图片能力会让宿主
        # 持久化 endpoint 之后每轮都可能拒绝的输入。
        base = {"provider": provider, "model": model, "name": model,
                "input_modalities": ["text"]}
        context_window = connection.defaultContextWindow
    else:
        base = catalog_model_info(provider, configured)
        context_window = (configured.contextWindow
                          if configured.contextWindow is not None
                          else connection.defaultContextWindow)

    info: dict = dict(base)
    info["context"] = {"contextWindow": context_window}
    default_max_tokens = (configured.maxTokens if configured is not None
                          and configured.maxTokens is not None
                          else connection.maxTokens)
    info["defaultMaxTokens"] = default_max_tokens
    if configured is not None and configured.systemPromptUpdate is not None:
        info["systemPromptUpdate"] = configured.systemPromptUpdate

    if connection.defaults.thinking == "disabled":
        info["reasoning"] = {"efforts": [dict(_OFF)],
                             "defaultEffort": "off"}
    else:
        info["reasoning"] = {
            "efforts": [dict(entry) for entry in REASONING_EFFORTS],
            "defaultEffort": _EFFORT_DEFAULTS.get(
                connection.defaults.reasoningEffort or "", "high"),
        }
    return info
