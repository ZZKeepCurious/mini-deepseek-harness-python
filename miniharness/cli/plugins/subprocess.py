"""web 组合条目：subprocess（ctx.subprocess 服务，L3 subprocess）。

对齐上游 dsh-base 默认挂载 `subprocess-local`（M17）：收编可执行解析 / 终端环境 /
环境清洗。lsp-stdio 等消费者依赖它。
"""


def apply(ctx, **config):
    from ...subprocess import install_subprocess

    install_subprocess(ctx)