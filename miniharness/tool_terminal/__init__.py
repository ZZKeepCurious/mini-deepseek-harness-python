"""tool-terminal：terminal 域的六模型工具 + pty-send 后台作业插件面（P3）。

对齐 packages/terminal/tool-terminal：plugin `tool-terminal`（name/inject），
Config {enableRunInBackground?, maxResultBytes?}，注册 terminal_open/send/
read/signal/close/list 六工具 + `tool:pty` 引导节（order 1700），结果按
maxResultBytes 收口；后台 send 经 ctx.jobs 开 `kind='pty-send'` 作业。

装配：`register_terminal_tools(reg, terminals, jobs_getter, config)` 注册工具；
`install_tool_terminal(ctx, config)` 组装面（幂等，缺服务即建）；`apply(ctx,
config)` 为上游插件形状别名。
"""
from .render import (
    TRUNCATED,
    bound_terminal_text,
    render_list,
    render_read,
    render_send,
    render_send_read,
    render_spawn,
)
from .tools import (
    DEFAULT_MAX_RESULT_BYTES,
    INJECT,
    MIN_MAX_RESULT_BYTES,
    PLUGIN_NAME,
    TOOL_PTY_ORDER,
    TOOL_PTY_SECTION,
    apply,
    install_tool_terminal,
    raw_content_text,
    register_terminal_tools,
    require_agent,
    resolve_config,
    send_detail,
    session_id,
)

# 上游插件同名常量（tools.spec.ts plugin shape 断言：name='tool-terminal'、
# inject=['terminals','tools','systemPrompt']）。
name = PLUGIN_NAME
inject = list(INJECT)

__all__ = [
    "DEFAULT_MAX_RESULT_BYTES",
    "INJECT",
    "MIN_MAX_RESULT_BYTES",
    "PLUGIN_NAME",
    "TOOL_PTY_ORDER",
    "TOOL_PTY_SECTION",
    "TRUNCATED",
    "apply",
    "bound_terminal_text",
    "inject",
    "install_tool_terminal",
    "name",
    "raw_content_text",
    "register_terminal_tools",
    "render_list",
    "render_read",
    "render_send",
    "render_send_read",
    "render_spawn",
    "require_agent",
    "resolve_config",
    "send_detail",
    "session_id",
]