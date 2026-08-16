"""Plan-mode 部署方配置校验（对齐上游 PlanModeConfig）。

上游对照：packages/plan/plan-mode/src/index.ts resolveConfig（index.ts:106-119）：
缺省/空/非字符串 section、未知键一律插件加载期 fail loud。
"""
from __future__ import annotations

__all__ = ["resolve_config"]


def resolve_config(config: dict | None) -> dict:
    """校验并返回脱离输入的 {section: str} 配置。

    @param config - 原始插件配置；None 等价于空字典。
    @raises ValueError - section 缺失/非字符串/空白，或存在未知键。
    """
    raw = {} if config is None else dict(config)
    section = raw.get("section")
    if not isinstance(section, str):
        raise ValueError("PlanModeConfig needs a string `section`")
    if section.strip() == "":
        raise ValueError("PlanModeConfig needs a non-empty `section`")
    unknown = [key for key in raw if key != "section"]
    if unknown:
        raise ValueError(
            f"PlanModeConfig has unknown key(s) {', '.join(unknown)} — config is {{ section }}"
        )
    return {"section": section}
