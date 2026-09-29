"""web 组合条目：credentials（本地凭据服务，L3 seams）。"""


def apply(ctx, **config):
    from ...seams.credentials_local import install_credentials

    install_credentials(ctx)