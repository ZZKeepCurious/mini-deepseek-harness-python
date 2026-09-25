"""每条 DeepSeek 协议共享的缺省模型目录。

对应 dsh 真实源码：packages/llm/llm-deepseek/src/common/models.ts。
"""
from __future__ import annotations

from .defaults import DEFAULT_CONTEXT_WINDOW
from .types import DeepSeekCatalogModel

__all__ = ["DEFAULT_MODELS"]

#: 建议性官方模型条目；部署可替换整个目录（上游 models.ts）。
DEFAULT_MODELS = (
    DeepSeekCatalogModel(
        id="deepseek-flash",
        name="DeepSeek-V41-Flash",
        contextWindow=DEFAULT_CONTEXT_WINDOW,
        inputModalities=("text", "image"),
        systemPromptUpdate="in-history",
    ),
    DeepSeekCatalogModel(
        id="deepseek-v4-pro",
        name="DeepSeek-V4-Pro",
        description="Stronger agentic coding, knowledge, and difficult reasoning; suited to complex or quality-critical tasks at higher cost.",
        contextWindow=DEFAULT_CONTEXT_WINDOW,
    ),
)
