"""shell 子进程环境装配（对齐 upstream terminal-bash/src/index.ts:64-98）。

child_environment 的肠道：subprocess provider 提供已 scrub 的环境基座，这里
只叠加有意的终端专属覆盖（TERM/PAGER/DSH_* 与 bash 方言的 PS1/PROMPT_COMMAND，
pwsh 方言换 NO_COLOR + 启动引导随阶段注入的 ENCODING_PREAMBLE/PWSH_PROMPT_SETUP）。
"""

from __future__ import annotations

from ..terminal.sanitize import CONTROLLED_PROMPT, PROMPT_MARKER_PREFIX

__all__ = [
    "ENCODING_PREAMBLE",
    "PWSH_PROMPT_SETUP",
    "child_environment",
    "prompt_command",
]

#: 会话结束/启动时锁定 UTF-8 输出（index.ts:48-49 携于 stdin 首行）。
ENCODING_PREAMBLE = (
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
)

#: pwsh 的 prompt 函数：每行 prompt 前写 OSC `133;D;` + BEL marker（index.ts:97-98）。
PWSH_PROMPT_SETUP = (
    "function prompt { [Console]::Write([char]27 + ']133;D;' + [int]$LASTEXITCODE + [char]7); '"
    + CONTROLLED_PROMPT
    + "' }"
)

#: bash 方言每行 prompt 前先重印 marker、再重置 PS1（index.ts:86）。
def prompt_command() -> str:
    return (
        f'printf "\\033]{PROMPT_MARKER_PREFIX}%s\\007" "$?"; '
        f"PS1='{CONTROLLED_PROMPT}'"
    )


def child_environment(spec: dict, dialect: str) -> dict:
    """terminal 专属环境覆盖（index.ts:64-89）。spec 需含 owner.id 与 sessionId。"""
    common = {
        "TERM": "dumb",
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "DSH_SHELL": "1",
        "DSH_SESSION_ID": spec["owner"].id,
        "DSH_PTY_SESSION_ID": spec["sessionId"],
    }
    if dialect == "pwsh":
        return {**common, "NO_COLOR": "1"}
    return {
        **common,
        "PS1": CONTROLLED_PROMPT,
        "PROMPT_COMMAND": prompt_command(),
        "BASH_SILENCE_DEPRECATION_WARNING": "1",
    }