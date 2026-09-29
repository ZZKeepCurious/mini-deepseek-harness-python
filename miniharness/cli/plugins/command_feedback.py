"""web 组合条目：command-feedback（/feedback 命令 + sessionFeedback，L3 feedback）。"""


def apply(ctx, **config):
    from ...feedback import install_command_feedback

    install_command_feedback(ctx)