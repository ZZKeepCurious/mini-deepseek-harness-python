"""web 组合条目：authorization（凭据获取授权 seam，L3 seams）。"""


def apply(ctx, **config):
    from ...seams.authorization import install_authorization

    install_authorization(ctx)
