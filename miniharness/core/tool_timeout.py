"""工具超时契约常量（对齐 upstream packages/guard/timeout-policy，已核实）。

TOOL_TIMEOUT 既是内部 deadline 分类码也是结构化错误 code；模型可见文本
`Error: tool call timed out after {n}ms`。本模块为 L0 叶：只放常量与文本
模板，不依赖 core.tools（tool_timeout_result 工厂在 core.tools / L1——
超时强制在管线执行体的实际落点），供 core.tools 与 guard.timeout_policy
两侧引用而互不越层。
"""
TOOL_TIMEOUT = "TOOL_TIMEOUT"


def timeout_error_message(timeout_ms: int) -> str:
    """超时错误 message（upstream toolTimeoutResult 的 message 字段）。"""
    return f"tool call timed out after {timeout_ms}ms"