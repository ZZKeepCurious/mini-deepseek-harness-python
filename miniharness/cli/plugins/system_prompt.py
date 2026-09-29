"""web 组合条目：system-prompt（L1 core/system_prompt）。"""


def apply(ctx, **config):
    from ...core.system_prompt import install_system_prompt

    install_system_prompt(ctx, config or None)