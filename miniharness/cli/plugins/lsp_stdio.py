"""web 组合条目：lsp-stdio（ctx.lsp 的 stdio provider 表，L2 lsp_stdio）。

opt-in（不在默认 web 组合内，上游默认组合亦不挂载 lsp）：用户经 profile
`cordis.patch.yml` 顶层 insert 本条目并提供 `config.servers`（provider id → 本地语言
服务器命令/扩展名表）即可启用语言服务；`default_tools` 在 `ctx.lsp` 在场时自动收编
`lsp` 模型工具。依赖 fs + subprocess（inject 声明）。
"""

inject = ["fs", "subprocess"]


def apply(ctx, **config):
    from ...lsp_stdio import install_lsp_stdio

    install_lsp_stdio(ctx, config)