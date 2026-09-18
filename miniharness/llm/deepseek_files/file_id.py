"""DeepSeek Files API 标识符品牌类型。

对应 dsh 真实源码：packages/llm/llm-deepseek/src/common/file-id.ts。

  * DeepSeekFileId——Files API 返回的不透明文件标识（wire 校验后品牌化）；
  * DeepSeekFileScope——标识一个 endpoint 与 API-key 文件命名空间的非秘密摘要
    （sha256(endpoint + NUL + apiKey)），用于索引隔离，绝不持久化或记录原始 key。
"""
from __future__ import annotations

__all__ = ["DeepSeekFileId", "DeepSeekFileScope"]


class DeepSeekFileId(str):
    """Files API 返回的不透明文件标识（上游 Branded<'DeepSeekFileId'>）。"""

    def __new__(cls, id: str) -> "DeepSeekFileId":
        return super().__new__(cls, id)


class DeepSeekFileScope(str):
    """本地派生的 endpoint/API-key 命名空间摘要（上游 Branded<'DeepSeekFileScope'>）。"""

    def __new__(cls, scope: str) -> "DeepSeekFileScope":
        return super().__new__(cls, scope)
