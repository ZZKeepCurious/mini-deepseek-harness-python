"""web 组合条目：terminal-controller（浏览器终端 Remote 面，L3 terminal_controller）。"""


def apply(ctx, **config):
    from ...terminal_controller import install_terminal_controller

    install_terminal_controller(ctx, config or None)