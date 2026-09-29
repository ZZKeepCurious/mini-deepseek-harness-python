"""web 组合条目：workspaces（workspace 域服务，L2 workspace）。"""


def apply(ctx, **config):
    from ...workspace import install_workspaces

    install_workspaces(ctx)