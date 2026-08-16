"""plan 包：log-only `plan/mode` 状态 + plan:policy prompt section + 审查 UI。

分层：L2 编排（依赖 L0/L1 的 core/session、core/scope、core/system_prompt，
被 L3 应用层导入）。装配序：install_system_prompt(ctx) 之后 install_plan_mode(ctx)，
可选 install_plan_review(ctx, controller)（审查 UI：exit_plan_mode 工具 + /plan 命令）。

简化标注见 mode.py / review.py / projection.py 模块 docstring。
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
from .projection import fold_plan_projection
from .review import (
    APPROVE_LABEL,
    EXIT_PLAN_MODE,
    EXIT_PLAN_MODE_DESCRIPTION,
    KEEP_PLANNING_LABEL,
    REVIEW_ID,
    install_plan_review,
)

__all__ = [
    "APPROVE_LABEL",
    "EXIT_PLAN_MODE",
    "EXIT_PLAN_MODE_DESCRIPTION",
    "KEEP_PLANNING_LABEL",
    "PLAN_POLICY_ORDER",
    "PLAN_POLICY_SECTION",
    "REVIEW_ID",
    "PlanModeController",
    "fold_plan_mode",
    "fold_plan_projection",
    "install_plan_mode",
    "install_plan_review",
    "resolve_config",
]
