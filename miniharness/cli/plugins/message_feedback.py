"""web 组合条目：message-feedback（messageFeedback Remote，L3 feedback）。

config.maxNoteBytes 为可配置旋钮（SettingsForms 可见），缺省 8192。
"""


def apply(ctx, **config):
    from ...feedback import install_message_feedback

    install_message_feedback(ctx, config or None)