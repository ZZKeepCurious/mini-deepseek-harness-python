"""guard 守卫族（对齐 upstream packages/guard）。

L1 层：只依赖 core（session/scope/tools），不 import core/agent_loop——
agent 对象以鸭子类型弱引用为键（AgentLoop 无 __slots__，可弱引用可哈希）。
超时强制内建于 core.tools 管线执行体（pipeline_body / pipeline_async_body），
本包提供契约常量、结果工厂与显式注册点；repeat-tool-reminder 提供逐 agent
重复调用检测提醒（不否决不改写）。
"""
from .repeat_tool_reminder import Config, install_repeat_tool_reminder, name  # noqa: F401
from .timeout_policy import TOOL_TIMEOUT, install_timeout_policy  # noqa: F401