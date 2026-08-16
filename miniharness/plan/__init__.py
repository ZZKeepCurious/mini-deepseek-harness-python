"""plan 包：log-only `plan/mode` 状态 + plan:policy prompt section 注入。

分层：L2 编排（依赖 L0/L1 的 core/session、core/scope、core/system_prompt，
被 L3 应用层导入）。装配序：install_system_prompt(ctx) 之后 install_plan_mode(ctx)。

教学扩展/后置标注见 mode.py 模块 docstring（审查 UI、/plan 命令、投影单元）。
"""
from __future__ import annotations

from .config import resolve_config
from .mode import (
    PLAN_POLICY_ORDER,
    PLAN_POLICY_SECTION,
    PlanModeController,
    fold_plan_mode,
    install_plan_mode,
)

__all__ = [
    "PLAN_POLICY_ORDER",
    "PLAN_POLICY_SECTION",
    "PlanModeController",
    "fold_plan_mode",
    "install_plan_mode",
    "resolve_config",
]
