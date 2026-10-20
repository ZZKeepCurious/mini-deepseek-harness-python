"""Harness 用户数据根目录（$DSH_HOME > ~/.dsh）与共享 agents 根（$DSH_AGENTS_HOME > ~/.agents）。

对照上游：packages/util/home-paths（`resolveDshHome`/`resolveAgentsHome`/
`expandHomePath`/`dshHomeDisplay`/`agentsHomeDisplay`/`DSH_HOME_ENV`/
`DSH_AGENTS_HOME_ENV`）。mini 此前在 skills/filesystem.py 与 credentials_local.py
各有一份内联等价；本模块收敛为唯一权威，各域复用。

语义（与上游一致）：
  * 优先级：显式配置 > 环境变量（非空白）> 缺省（`~/.dsh` / `~/.agents`）。
  * 支持 `~`、`~/`、`~\\` 前缀展开。
  * 返回绝对、规范化路径（resolve）。
  * 展示标签：缺省根给 `~/.dsh` / `~/.agents`，被配置的根给 `$DSH_HOME` /
    `$DSH_AGENTS_HOME`（对齐上游 `rootDisplay`）。
"""
from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "AGENTS_HOME_DIR_NAME",
    "AGENTS_HOME_ENV",
    "DEFAULT_AGENTS_HOME_DISPLAY",
    "DEFAULT_DSH_HOME_DISPLAY",
    "DSH_HOME_DIR_NAME",
    "DSH_HOME_ENV",
    "agents_home_display",
    "dsh_home_display",
    "dsh_home_path",
    "expand_home_path",
    "resolve_agents_home",
    "resolve_dsh_home",
]

#: 缺省 harness 目录名（`~/.dsh`）。
DSH_HOME_DIR_NAME = ".dsh"

#: 覆盖缺省 harness 根的环境变量。
DSH_HOME_ENV = "DSH_HOME"

#: 缺省共享 agents 目录名（`~/.agents`）。
AGENTS_HOME_DIR_NAME = ".agents"

#: 覆盖缺省共享 agents 根的环境变量。
AGENTS_HOME_ENV = "DSH_AGENTS_HOME"

#: 缺省根的模型可见展示标签（上游 home-paths/src/index.ts:19-24）。
DEFAULT_DSH_HOME_DISPLAY = f"~/{DSH_HOME_DIR_NAME}"
DEFAULT_AGENTS_HOME_DISPLAY = f"~/{AGENTS_HOME_DIR_NAME}"


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


def resolve_agents_home(configured: str | None = None, env: dict | None = None) -> str:
    """解析共享 agents 根：显式配置 > `$DSH_AGENTS_HOME` > `~/.agents`。

    对齐上游 `resolveAgentsHome`（home-paths/src/index.ts:151）：空白环境变量
    视为未设置，回退缺省；结果绝对规范化。
    """
    environ = env if env is not None else os.environ
    from_env = environ.get(AGENTS_HOME_ENV)
    if configured is not None:
        selected = configured
    elif from_env is not None and from_env.strip():
        selected = from_env
    else:
        selected = os.path.join(os.path.expanduser("~"), AGENTS_HOME_DIR_NAME)
    return str(Path(expand_home_path(selected)).expanduser().resolve())


def _root_display(resolved_home: str, default_home: str, default_display: str,
                  env_name: str) -> str:
    """缺省根给 `~` 风格标签，被配置的根给环境变量名（对齐上游 `rootDisplay`）。"""
    if os.path.abspath(resolved_home) == os.path.abspath(default_home):
        return default_display
    return f"${env_name}"


def dsh_home_display(resolved_home: str) -> str:
    """`dshHomeDisplay`：缺省根 `~/.dsh`，否则 `$DSH_HOME`。"""
    default_home = os.path.join(os.path.expanduser("~"), DSH_HOME_DIR_NAME)
    return _root_display(resolved_home, default_home, DEFAULT_DSH_HOME_DISPLAY, DSH_HOME_ENV)


def agents_home_display(resolved_home: str) -> str:
    """`agentsHomeDisplay`：缺省根 `~/.agents`，否则 `$DSH_AGENTS_HOME`。"""
    default_home = os.path.join(os.path.expanduser("~"), AGENTS_HOME_DIR_NAME)
    return _root_display(resolved_home, default_home, DEFAULT_AGENTS_HOME_DISPLAY,
                         AGENTS_HOME_ENV)