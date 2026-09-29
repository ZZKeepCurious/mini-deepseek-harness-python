"""web 组合条目：deepseek-account（账户 Remote 面，L3 deepseek_account）。"""


def apply(ctx, **config):
    from ...deepseek_account import install_deepseek_account

    install_deepseek_account(ctx)