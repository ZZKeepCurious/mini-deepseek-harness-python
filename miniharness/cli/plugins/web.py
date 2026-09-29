"""web 组合条目：web（模型可见 web 工具族，L2 web_tools）。"""


def apply(ctx, **config):
    from ...web_tools import install_web

    install_web(ctx, config or None)