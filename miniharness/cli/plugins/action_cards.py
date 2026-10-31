"""experimental 组合条目：action-cards producer（opt-in，默认 web profile 不挂载）。"""


def apply(ctx, **config):
    from ...experimental.action_cards import install_action_cards

    install_action_cards(ctx)
