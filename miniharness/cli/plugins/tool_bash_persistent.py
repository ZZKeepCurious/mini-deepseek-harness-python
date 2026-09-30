"""web/loader 组合条目：持久 `bash` 工具（L3 tool_bash_persistent；opt-in）。

经 owner 作用域持久 PTY 会话运行命令。与 `tool_bash` 的同名 `bash` 工具互为替代，
组合按需挂载其一；依赖 tools + terminals（inject 声明）。
"""

inject = ["tools", "terminals"]


def apply(ctx, **config):
    from ...tool_bash_persistent import install_persistent_bash

    install_persistent_bash(ctx, ctx.get("tools"), config)
