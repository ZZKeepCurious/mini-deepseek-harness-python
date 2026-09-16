"""timeout-policy 守卫：工具调用超时强制（对齐 upstream packages/guard/timeout-policy）。

上游以 cordis 插件包住 `tools/execute` waterfall：工具声明 timeoutMs
并承诺响应 exec.signal，deadline（@deepseek-ai/dsh-timeout）在计时到点后
把 TOOL_TIMEOUT 结构化错误替换到模型可见结果（timer-wins：即使工具体
返回了它自己的 abort 形状结果也被替换），caller 先取消则保留原结果。

mini 将超时强制内置于管线执行体（pipeline_body / pipeline_async_body）：
超时分支已经置位 signal + 排干到静止点，语义与上游一致。本模块因此只
登记契约常量（实现在 core/tool_timeout，L0 叶；`tool_timeout_result` 工厂在
core.tools/L1——超时强制实际落点，避免 core.tools ↔ guard 循环依赖与
越层），并提供显式注册点 install_timeout_policy（HMR/外部消费者可见的
具名通路，不额外挂 waterfall——超时内建，无空转）。

契约（上游 index.ts:25-48，已核实）：
  * TOOL_TIMEOUT = 'TOOL_TIMEOUT'（既是 deadline 分类码也是结构化错误 code）
  * 模型可见 content.text = 'Error: tool call timed out after {n}ms'
  * error = { message: 'tool call timed out after {n}ms',
              info: { name: 'ToolTimeoutError', code: 'TOOL_TIMEOUT' } }
"""
from __future__ import annotations

from typing import Any

from ..core.scope import Context
from ..core.tool_timeout import TOOL_TIMEOUT

__all__ = [
    "TOOL_TIMEOUT",
    "install_timeout_policy",
]

name = "timeout-policy"


def install_timeout_policy(ctx: Context, **_: Any) -> None:
    """注册 timeout-policy 守卫的具名通路（空操作）。

    超时强制已内建于 core.tools 管线执行体（pipeline_body /
    pipeline_async_body 的超时分支）。保留本注册点是给配置可见的
    语义出口——插件在此声明约定即契约有效，不重复挂钩。
    """