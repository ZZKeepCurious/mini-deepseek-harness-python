"""Harness 用户数据根目录（$DSH_HOME > ~/.dsh）与公共路径助手。

对照上游：packages/util/home-paths（`resolveDshHome` / `expandHomePath` /
`DSH_HOME_ENV`）。mini 此前在 skills/filesystem.py 与 credentials_local.py
各有一份内联等价；本模块收敛为唯一权威，各域复用。

语义（与上游一致）：
  * 优先级：显式配置 > `$DSH_HOME`（非空白）> `~/.dsh`。
  * 支持 `~`、`~/`、`~\\` 前缀展开。
  * 返回绝对、规范化路径（resolve）。
"""
from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "DSH_HOME_DIR_NAME",
    "DSH_HOME_ENV",
    "resolve_dsh_home",
    "dsh_home_path",
]

#: 缺省 harness 目录名（`~/.dsh`）。
DSH_HOME_DIR_NAME = ".dsh"

#: 覆盖缺省 harness 根的环境变量。
DSH_HOME_ENV = "DSH_HOME"


def expand_home_path(value: str) -> str:
    """展开支持的波浪号前缀（`~`、`~/`、`~\\`）；无前缀返回原值。

    对齐上游 `expandHomePath`（home-paths/src/index.ts）：`~` 与 `~/`/`~\\`
    三种前缀展开，其它原样返回。
    """
    if value == "~":
        return os.path.expanduser("~")
    if value.startswith("~/") or value.startswith("~\\"):
        return os.path.join(os.path.expanduser("~"), value[2:])
    return value


def resolve_dsh_home(configured: str | None = None, env: dict | None = None) -> str:
    """解析 harness 根：显式配置 > `$DSH_HOME` > `~/.dsh`（绝对规范化）。

    对齐上游 `resolveDshHome`（home-paths/src/index.ts）：`$DSH_HOME` 空白
    视为未设置，回退缺省。
    """
    environ = env if env is not None else os.environ
    from_env = environ.get(DSH_HOME_ENV)
    if configured is not None:
        selected = configured
    elif from_env is not None and from_env.strip():
        selected = from_env
    else:
        selected = os.path.join(os.path.expanduser("~"), DSH_HOME_DIR_NAME)
    return str(Path(expand_home_path(selected)).expanduser().resolve())


def dsh_home_path(*segments: str) -> str:
    """在 harness 根下拼接路径段（对齐上游 `dshHomePath`）。"""
    return os.path.join(resolve_dsh_home(), *segments)