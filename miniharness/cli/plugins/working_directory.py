"""web 组合条目：working-directory（会话当前工作目录服务，L1 working_directory）。

对齐上游 packages/session/working-directory（static inject = fs /
sessionProjections / systemPrompt）。工具 `working_directory` 由 host 的
`default_tools` 在 ctx.workingDirectory 在场时收编（上游经 tool-working-directory
插件挂载）。
"""


def apply(ctx, **config):
    from ...working_directory import install_working_directory

    install_working_directory(ctx, config)
