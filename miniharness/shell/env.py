"""shell 环境注册表（ctx.shellEnv，对齐 packages/shell/shell-env/src/index.ts）。

为模型 shell 调用提供受信任的、逐执行的 `DSH_*` 事实：内置事实（`DSH_HOME`、
`DSH_SHELL`、`DSH_SESSION_ID`，以及 profile 上下文在场时的 `DSH_PROFILE` /
`DSH_PROFILE_DIR`）由注册表自身拥有，插件可注册额外可枚举事实。保留键不得被
贡献者占用；命名/键语法与重复归属 fail loud。

载体说明（登录 verified-diffs）：mini 无 schemastery 配置链，dshHome 以普通
参数承载；贡献者以 dict 声明（name / variables / resolve），注册经 ctx.effect
随 fiber 注销。
"""
from __future__ import annotations

import re

from ..core.home_paths import DSH_HOME_ENV, resolve_dsh_home
from ..core.scope import Context, Service
from ..seams.subprocess_env import DSH_ENV_PREFIX

__all__ = [
    "BASH_ENV_KEY_SUFFIX",
    "DSH_PROFILE_DIR_KEY",
    "DSH_PROFILE_KEY",
    "DSH_SESSION_ID_KEY",
    "DSH_SHELL_KEY",
    "RESERVED_BASH_ENV_KEYS",
    "ShellEnvRegistry",
    "install_shell_env",
]

#: 内置 `DSH_*` 键（registry-owned，contributor 不得声明）。
DSH_SHELL_KEY = f"{DSH_ENV_PREFIX}SHELL"
DSH_SESSION_ID_KEY = f"{DSH_ENV_PREFIX}SESSION_ID"
DSH_PROFILE_KEY = f"{DSH_ENV_PREFIX}PROFILE"
DSH_PROFILE_DIR_KEY = f"{DSH_ENV_PREFIX}PROFILE_DIR"

#: 保留键闭集（upstream index.ts:76-82）。
RESERVED_BASH_ENV_KEYS = frozenset({
    DSH_HOME_ENV, DSH_SHELL_KEY, DSH_SESSION_ID_KEY,
    DSH_PROFILE_KEY, DSH_PROFILE_DIR_KEY,
})

#: 贡献键后缀语法（upstream index.ts:83）。
BASH_ENV_KEY_SUFFIX = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ShellEnvRegistry(Service):
    """`ctx.shellEnv`：逐执行构建受信任 `DSH_*` 快照的注册表。"""

    provide = "shellEnv"

    def __init__(self, ctx: Context, config: dict | None = None):
        config = dict(config or {})
        self.dsh_home = resolve_dsh_home(config.get("dshHome"))
        self._contributors: dict = {}
        self._key_owners: dict = {}
        super().__init__(ctx, "shellEnv")

    def register(self, contributor: dict):
        """注册一个环境贡献者；返回随 fiber 生效的注销 disposer。"""
        name = contributor.get("name")
        if not isinstance(name, str) or name.strip() == "":
            raise ValueError("bash env contributor name must be non-empty")
        variables = contributor.get("variables") or {}
        for key, variable in variables.items():
            if not key.startswith(DSH_ENV_PREFIX) \
                    or not BASH_ENV_KEY_SUFFIX.match(key[len(DSH_ENV_PREFIX):]):
                raise ValueError(
                    f'bash env contributor "{name}" declared invalid key "{key}"')
            if key in RESERVED_BASH_ENV_KEYS:
                raise ValueError(
                    f'bash env contributor "{name}" cannot own reserved key "{key}"')
            if not isinstance(variable, dict) or not str(variable.get("description", "")).strip():
                raise ValueError(f'bash env contributor "{name}" must describe "{key}"')

        def teardown() -> None:
            self._contributors.pop(name, None)
            for key in variables:
                self._key_owners.pop(key, None)

        def setup():
            if name in self._contributors:
                raise ValueError(
                    f'bash env contributor "{name}" is already registered')
            for key in variables:
                owner = self._key_owners.get(key)
                if owner is not None:
                    raise ValueError(
                        f'bash env key "{key}" is already owned by contributor "{owner}"; '
                        f'contributor "{name}" cannot also own it')
            self._contributors[name] = contributor
            for key in variables:
                self._key_owners[key] = name
            return teardown

        return self.ctx.effect(setup, "shellEnv.register()")

    def collect(self, execution) -> dict:
        """为一个 shell 工具执行构建受信任 `DSH_*` 快照（排序后冻结）。"""
        values: dict = {
            DSH_HOME_ENV: self.dsh_home,
            DSH_SHELL_KEY: "1",
        }
        session_id = _session_id(getattr(execution, "agent", None))
        if session_id is not None:
            values[DSH_SESSION_ID_KEY] = session_id
        profile = self._profile_context()
        if profile is not None:
            name = _attr(profile, "name")
            directory = _attr(profile, "dir")
            if name is not None:
                values[DSH_PROFILE_KEY] = name
            if directory is not None:
                values[DSH_PROFILE_DIR_KEY] = directory

        for contributor in sorted(self._contributors.values(), key=lambda c: c["name"]):
            resolved = contributor["resolve"](execution)
            for key, value in resolved.items():
                if key not in contributor.get("variables", {}):
                    raise RuntimeError(
                        f'bash env contributor "{contributor["name"]}" returned '
                        f'undeclared key "{key}"')
                if not isinstance(value, str):
                    raise RuntimeError(
                        f'bash env contributor "{contributor["name"]}" returned a '
                        f'non-string value for "{key}"')
                values[key] = value
        return dict(sorted(values.items()))

    def list(self) -> list[dict]:
        """枚举贡献者声明的变量（不执行 resolver）。"""
        out = []
        for contributor in self._contributors.values():
            for key, variable in (contributor.get("variables") or {}).items():
                out.append({"contributor": contributor["name"],
                            "description": variable.get("description"), "key": key})
        return sorted(out, key=lambda item: item["key"])

    def _profile_context(self):
        getter = getattr(self.ctx, "get", None)
        return getter("profileContext") if callable(getter) else None


def install_shell_env(ctx: Context, config: dict | None = None) -> "ShellEnvRegistry":
    """装配 ctx.shellEnv（幂等；已存在即返回）。"""
    existing = ctx.get("shellEnv")
    if existing is not None:
        return existing
    return ShellEnvRegistry(ctx, config)


def _session_id(agent) -> str | None:
    if agent is None:
        return None
    session = getattr(agent, "session", None)
    if session is None:
        return None
    return getattr(session, "session_id", None) or getattr(session, "id", None) \
        or getattr(agent, "id", None)


def _attr(target, name):
    if isinstance(target, dict):
        return target.get(name)
    return getattr(target, name, None)
