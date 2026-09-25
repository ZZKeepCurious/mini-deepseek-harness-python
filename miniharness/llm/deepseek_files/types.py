"""DeepSeek 协议共享的目录与请求局部依赖类型。

对应 dsh 真实源码：packages/llm/llm-deepseek/src/common/types.ts。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .file_store import DeepSeekFileConnection, DeepSeekFilePolicy

__all__ = [
    "DeepSeekCatalogModel",
    "DeepSeekConnectionOptions",
    "DeepSeekFileConnection",
    "DeepSeekFilePolicy",
    "RequestDefaults",
]


@dataclass(frozen=True)
class DeepSeekCatalogModel:
    """直连适配器广告的一个可选模型条目（上游 DeepSeekCatalogModel）。"""

    id: str
    name: str | None = None
    description: str | None = None
    contextWindow: int | None = None
    maxTokens: int | None = None
    inputModalities: tuple[str, ...] | None = None
    imagePixelBudget: "int | str | None" = None
    imageMaxBytes: int | None = None
    systemPromptUpdate: str | None = None


@dataclass(frozen=True)
class RequestDefaults:
    """插件配置而来的适配器级请求缺省（上游 RequestDefaults）。"""

    thinking: str | None = None
    reasoningEffort: str | None = None


@dataclass(frozen=True)
class DeepSeekConnectionOptions:
    """一次操作的有效连接事实（上游 DeepSeekConnectionOptions 的 mini 子集）。

    mini 无 schemastery loader；本结构由调用方（适配器构造或 CLI）显式装配，
    适配器信任它并逐操作重读，使配置变化无需重注册即可到达下一次请求。
    """

    baseURL: str
    defaults: RequestDefaults = field(default_factory=RequestDefaults)
    maxTokens: int = 0
    defaultContextWindow: int = 0
    models: tuple[DeepSeekCatalogModel, ...] = ()
    streamIdleTimeoutMs: int = 0
    maxRequestFilesBytes: int = 0
    maxInlineRequestImageBytes: int = 0
    maxImagesPerRequest: int = 0
    imageOffloadByteQuantum: int = 0
    inlineImageOffloadByteQuantum: int = 0
    imageOffloadCountQuantum: int = 0
    filesApiTimeoutMs: int = 0
    filePolicy: DeepSeekFilePolicy | None = None
