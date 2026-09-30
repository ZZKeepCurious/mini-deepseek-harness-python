"""web/loader 组合条目：持久 `pwsh` 工具（L3 tool_pwsh_persistent；opt-in）。

与 `tool_pwsh` 的同名 `pwsh` 工具互为替代；依赖 tools + terminals（inject 声明）。
"""

inject = ["tools", "terminals"]


def apply(ctx, **config):
    from ...tool_pwsh_persistent import install_persistent_pwsh

    install_persistent_pwsh(ctx, ctx.get("tools"), config)
