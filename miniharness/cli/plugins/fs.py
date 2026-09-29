"""web 组合条目：fs（本地文件系统 seam，L1 fs）。"""


def apply(ctx, **config):
    import os

    from ...fs import install_local_fs

    install_local_fs(ctx, {"cwd": config.get("cwd") or os.getcwd()})