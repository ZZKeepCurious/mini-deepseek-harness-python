"""web 组合条目：workspace-changes（顶回合工作区变更摘要与比较）。"""


def apply(ctx, **config):
    from ...workspace_changes import install_workspace_changes

    install_workspace_changes(ctx, dict(config) or None)
