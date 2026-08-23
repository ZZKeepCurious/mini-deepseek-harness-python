"""subprocess 接缝的环境清洗面（对齐 `packages/subprocess/subprocess/src/index.ts`）。

上游语义（已核实 index.ts:44,60-66 + types.ts:13）：
  * `SENSITIVE_ENV_PATTERN = /KEY|PASSWORD|SECRET|TOKEN/i` —— 凭据形状的
    环境名**不**转发给子进程（harness 自己的 `DEEPSEEK_API_KEY`/秘密不得
    隐式泄漏进被 spawn 的进程）；一个启发式服务所有 spawner。
  * `DSH_ENV_PREFIX = 'DSH_'` —— 环境名（经大写归一）以此前缀开头的全部
    剔除。两次剔除都大小写不敏感：Windows 环境名大小写不敏感，父代的
    `dsh_*` 条目否则会存活并在子代读回 `$env:DSH_*`；POSIX 上刻意的
    小写 `dsh_*` 名字不合常理。
  * `scrubbed_parent_env()` = 父环境 − 凭据形状名字 − `DSH_*` 名字——每个
    harness 子进程的规范基底。`PATH`、`HOME`、locale 与代理变量存活，子
    CLI 正常运行；harness 身份绝不隐式泄漏。刻意转发的凭据或当前 `DSH_*`
    事实走 spec 的显式 env，在 scrub **之后**合并（调用点形态
    `{...scrubbedParentEnv(), ...spec.env}`，`subagent-dsh-sdk/src/run.ts:123`
    与 `subagent-claude-code/src/run.ts:186` 同款）。
"""
from __future__ import annotations

import os
import re
from typing import Mapping

__all__ = ["DSH_ENV_PREFIX", "SENSITIVE_ENV_PATTERN", "scrubbed_parent_env"]

#: harness 托管环境命名空间前缀（上游 types.ts:13）。
DSH_ENV_PREFIX = "DSH_"

#: 凭据形状环境名启发式（上游 index.ts:44，JS regex test ≙ Python search）。
SENSITIVE_ENV_PATTERN = re.compile(r"KEY|PASSWORD|SECRET|TOKEN", re.IGNORECASE)


def scrubbed_parent_env(
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """父环境减去凭据形状名字与 `DSH_*` 名字后的全新副本。

    @param base - 待清洗的环境映射；缺省读 `os.environ`。
    @returns 可直接交给子进程 spawn 的环境 dict（显式 env 在其上合并）。
    """
    source: Mapping[str, str] = os.environ if base is None else base
    env: dict[str, str] = {}
    for key, value in source.items():
        if value is None:
            continue
        if SENSITIVE_ENV_PATTERN.search(key):
            continue
        if key.upper().startswith(DSH_ENV_PREFIX):
            continue
        env[key] = value
    return env
