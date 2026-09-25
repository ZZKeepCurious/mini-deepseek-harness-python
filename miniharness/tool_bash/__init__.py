"""tool-bash：`ctx.shell` 的模型面 bash 工具（对齐 packages/shell/tool-bash）。

Config `{enableRunInBackground?, promoteOnTimeout?}`（缺省皆 true）；jobs 在场时
前台超时提升为后台作业、支持 `run_in_background`，否则纯前台由执行器 deadline 杀。
装配：`create_bash_tool(ctx, shell, config)` 构造工具；`install_tool_bash(ctx,
config)` 确保 shellEnv 服务并注册（幂等）。
"""
from .background import process_outcome, process_sources, ring_delta, sandbox_notes
from .index import (
    DEFAULT_CONFIG,
    PLUGIN_NAME,
    ToolAborted,
    create_bash_tool,
    install_tool_bash,
    resolve_config,
)
from .render import (
    escalation_hint_marker,
    parse_exit_status,
    render_job_read,
    render_promoted,
    render_result,
    sandbox_denial_marker,
)

# 上游插件同名常量。
name = PLUGIN_NAME
inject = ["tools", "shell", "systemPrompt", "shellEnv"]

__all__ = [
    "DEFAULT_CONFIG",
    "PLUGIN_NAME",
    "ToolAborted",
    "create_bash_tool",
    "escalation_hint_marker",
    "inject",
    "install_tool_bash",
    "name",
    "parse_exit_status",
    "process_outcome",
    "process_sources",
    "render_job_read",
    "render_promoted",
    "render_result",
    "resolve_config",
    "ring_delta",
    "sandbox_denial_marker",
    "sandbox_notes",
]
