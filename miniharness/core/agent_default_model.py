"""默认模型选择：供未指定模型的新 Agent 使用的部署默认。

上游对照：`packages/core/agent-default-model/src/index.ts`（AgentDefaultModelConfig）。

契约（与上游一致）：
  * `current_selection()` 返回分离的 `{provider, model, reasoningEffort?}`；
    `reasoningEffort` 缺省即不出现。
  * `save_selection(next)` 持久化完整选择；无 profile entry 或无
    `ctx.configEditor` 时是 no-op。
  * 保存按提交顺序串行（mini 为同步调用，天然串行）；某次失败只向调用者
    抛出，不影响后续保存。
  * 服务不校验目录成员资格——模型可用性诊断归发起模型请求的消费者。

载体差异（登记）：上游 Config 为 live refs，保存后经 profile patch 重新
应用即更新读取值；mini 的插件 Config 是安装期 dict，故保存成功后同步更新
内存选择，profile patch 由 ConfigEditor 写入并在重载/重启后生效。
"""
from __future__ import annotations

from typing import Any

from .scope import Context, Service

__all__ = [
    "AgentDefaultModel",
    "install_agent_default_model",
]


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError(
            f"agent-default-model: {field} must be a non-empty string")
    return value


def _resolve_config(provider: Any, model: Any,
                    reasoning_effort: Any) -> dict[str, str]:
    config = {
        "provider": _require_text(provider, "provider"),
        "model": _require_text(model, "model"),
    }
    if reasoning_effort is not None:
        config["reasoningEffort"] = _require_text(
            reasoning_effort, "reasoningEffort")
    return config


class AgentDefaultModel(Service):
    """`ctx.agentDefaultModel`：部署默认模型选择（与传输/宿主无关）。"""

    provide = "agentDefaultModel"

    def __init__(self, ctx: Context, *, provider: Any, model: Any,
                 reasoning_effort: Any = None):
        self._config = _resolve_config(provider, model, reasoning_effort)
        super().__init__(ctx)

    def current_selection(self) -> dict[str, str]:
        """返回当前默认选择（分离副本，可安全修改）。"""
        return dict(self._config)

    def save_selection(self, selection: dict) -> None:
        """保存完整默认选择；无编辑器时保持组合条目不变。

        @param selection `{provider, model, reasoningEffort?}`。
        """
        if not isinstance(selection, dict):
            raise TypeError("agent-default-model: selection must be an object")
        config = _resolve_config(
            selection.get("provider"), selection.get("model"),
            selection.get("reasoningEffort"))
        entry = getattr(self.ctx.fiber, "entry", None)
        editor = self.ctx.get("configEditor")
        if entry is None or editor is None:
            return
        editor.edit(entry, lambda current, inherited: dict(config))
        self._config = config


def install_agent_default_model(ctx: Context, *, provider: Any, model: Any,
                                reasoning_effort: Any = None) -> AgentDefaultModel:
    """装配 `ctx.agentDefaultModel`（幂等：已装返回既有实例）。

    @param provider 已注册的 provider 路由。
    @param model provider 自有模型 id。
    @param reasoning_effort 适配器推理强度（可选）。
    """
    existing = ctx.get("agentDefaultModel")
    if isinstance(existing, AgentDefaultModel):
        return existing
    return AgentDefaultModel(ctx, provider=provider, model=model,
                             reasoning_effort=reasoning_effort)
