"""web 组合条目：workspace-files（api 组 Remote，L3 workspace_files）。"""


def apply(ctx, **config):
    from ...workspace_files import install_workspace_files

    install_workspace_files(ctx, config or None)