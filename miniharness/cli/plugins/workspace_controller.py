"""web 组合条目：workspace-controller（api 组 Remote，L3 workspace_controller）。"""


def apply(ctx, **config):
    from ...workspace_controller import install_workspace_controller

    install_workspace_controller(ctx, config or None)