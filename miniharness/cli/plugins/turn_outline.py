"""web 组合条目：turn-outline（会话回合大纲投影 M18，L2 session_turn_outline）。"""


def apply(ctx, **config):
    from ...session_turn_outline import install_turn_outline

    install_turn_outline(ctx)