"""web 组合条目：user-questions（模型澄清 service seam，L3 interaction）。"""


def apply(ctx, **config):
    from ...interaction import install_user_questions

    install_user_questions(ctx)