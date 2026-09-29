"""web 组合条目：session-projections（投影注册表 M7，L1 session_projection）。"""


def apply(ctx, **config):
    from ...session_projection import install_session_projections

    install_session_projections(ctx)