# 私有 shell 生命周期文件；普通输出永不授权终端回收
# （对齐 upstream subprocess-local/src/shell-activity.ts）。
#
# 仅对非 win32 上的「纯 `bash|zsh -i`」注入私有的 bash `--rcfile` / zsh
# `ZDOTDIR`，由 shell 的 prompt 钩子把状态写进状态文件（`pid:seq:idle|busy`），
# 供 TerminalHandle.inspect_activity 读取。自定义参数或包装过的启动原样不动，
# 返回 None（不支持的启动上报 unknown）。

from __future__ import annotations

import os
import re
import shutil
import tempfile

from .provider import SubprocessTerminalActivity

__all__ = ["ShellActivity", "prepare_shell_activity"]

#: 状态记录词法：`<pid>:<seq>:<idle|busy>`，允许一个尾随换行（shell-activity.ts:38）。
_STATE_RECORD = re.compile(r"^(\d+):(\d+):(idle|busy)\n?$")

#: bash prompt 钩子（逐字移植 shell-activity.ts:72-94）。
_BASH_RC_LINES = [
    "[[ ! -r ~/.bashrc ]] || builtin source ~/.bashrc",
    "if (( BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 4) )) && [[ ! $(declare -p PROMPT_COMMAND PS0 2>/dev/null) =~ declare\\ -[^[:space:]]*r ]]; then",
    "  __dsh_shell_pid=$BASHPID; __dsh_shell_sequence=0",
    "  __dsh_shell_idle() {",
    "    local result=$?",
    "    if [[ $BASHPID == \"$__dsh_shell_pid\" ]]; then",
    "      (( ++__dsh_shell_sequence ))",
    "      local activity=idle",
    "      builtin trap -p >| @@GUARDS@@",
    "      [[ ! -s @@GUARDS@@ ]] || activity=unknown",
    "      builtin printf '%s:%s:%s\\n' \"$BASHPID\" \"$__dsh_shell_sequence\" \"$activity\" >| @@STATE@@",
    "    fi",
    "    return \"$result\"",
    "  }",
    "  if [[ $(declare -p PROMPT_COMMAND 2>/dev/null) == \"declare -a \"* ]]; then",
    "    PROMPT_COMMAND+=(__dsh_shell_idle)",
    "  else",
    "    PROMPT_COMMAND=\"${PROMPT_COMMAND}\"$'\\n'\"__dsh_shell_idle\"",
    "  fi",
    "  PS0+=$(builtin printf '%s:%s:busy' \"$__dsh_shell_pid\" \"$__dsh_shell_sequence\" >| @@STATE@@)",
    "fi",
    "",
]

#: zsh 启动钩子（逐字移植 shell-activity.ts:98-124）。
_ZSH_ENV_LINES = [
    "@@FIRST@@",
    "[[ ! -r ${ZDOTDIR:-$HOME}/.zshenv ]] || builtin source \"${ZDOTDIR:-$HOME}/.zshenv\"",
    "typeset -g __dsh_shell_pid=$$ __dsh_shell_sequence=0",
    "__dsh_shell_activity() {",
    "  (( ZSH_SUBSHELL == 0 && $$ == __dsh_shell_pid )) || return",
    "  (( ++__dsh_shell_sequence ))",
    "  builtin printf '%s:%s:%s\\n' \"$$\" \"$__dsh_shell_sequence\" \"$1\" >| @@STATE@@",
    "  return 0",
    "}",
    "__dsh_shell_idle() {",
    "  { builtin trap; zle -F; } >| @@GUARDS@@",
    "  if [[ $CONTEXT != start || -n $BUFFER ]]; then __dsh_shell_activity busy",
    "  elif [[ -s @@GUARDS@@ || -n ${(k)functions[(I)TRAP*]} ]]; then __dsh_shell_activity unknown",
    "  else __dsh_shell_activity idle; fi",
    "}",
    "__dsh_shell_busy() { __dsh_shell_activity busy }",
    "__dsh_shell_init() {",
    "  autoload -Uz add-zle-hook-widget add-zsh-hook",
    "  add-zle-hook-widget line-init __dsh_shell_idle",
    "  add-zle-hook-widget line-finish __dsh_shell_busy",
    "  add-zsh-hook preexec __dsh_shell_busy",
    "  precmd_functions=(${precmd_functions:#__dsh_shell_init})",
    "}",
    "typeset -ga precmd_functions",
    "precmd_functions+=(__dsh_shell_init)",
    "",
]


def _quote(value: str) -> str:
    """POSIX 单引号转义（shell-activity.ts:7）。"""
    return "'" + value.replace("'", "'\\''") + "'"


def _write_private(path: str, text: str) -> None:
    """以 0600 + O_EXCL 私有写入（上游 `{mode:0o600, flag:'wx'}`）。"""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


class ShellActivity:
    """一条普通交互式 shell 的启动集成与 revision 追踪（shell-activity.ts:10）。"""

    def __init__(self, directory: str, argv: list[str], env: dict[str, str]):
        self.directory = directory
        self.argv = list(argv)
        self.env = dict(env)
        self._revision = 0
        self._observed = ""
        self._invalidated: str | None = None
        self._state = "unknown"

    def invalidate(self) -> None:
        """投递输入或前台信号前作废 prompt 证据（shell-activity.ts:24）。"""
        self._invalidated = self._read()
        self._revision += 1
        self._state = "unknown"

    def inspect(self, pid: int) -> SubprocessTerminalActivity:
        """读取最近一次顶层 shell 迁移，并对输入作栅栏（shell-activity.ts:35）。

        @param pid - 原始 shell 进程 id。
        @returns 生命周期证据；进程归属须另行校验。
        """
        record = self._read()
        if record != self._observed:
            self._observed = record
            self._revision += 1
        match = _STATE_RECORD.match(record)
        if record == self._invalidated or match is None or match.group(1) != str(pid):
            self._state = "unknown"
        else:
            self._state = match.group(3)
        return SubprocessTerminalActivity(self._state, self._revision)

    def dispose(self) -> None:
        """进程静默后移除私有启动与状态文件（shell-activity.ts:45）。"""
        shutil.rmtree(self.directory, ignore_errors=True)

    def _read(self) -> str:
        try:
            with open(os.path.join(self.directory, "state"), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""


def prepare_shell_activity(spec: dict, env: dict[str, str], platform: str) -> ShellActivity | None:
    """为纯非登录交互式 bash/zsh 启动准备可选集成（shell-activity.ts:60）。

    @param spec - 终端请求；自定义参数与包装过的可执行原样不动。
    @param env - 已清洗的目标环境。
    @param platform - 执行平台（`sys.platform`）。
    @returns 私有集成；不支持的启动返回 None。
    """
    if spec.get("shellActivity") is not True or platform.startswith("win"):
        return None
    argv = list(spec.get("argv") or [])
    if len(argv) != 2 or argv[1] != "-i":
        return None
    shell = os.path.basename(argv[0])
    if shell not in ("bash", "zsh"):
        return None
    directory = tempfile.mkdtemp(prefix="dsh-shell-")
    state = _quote(os.path.join(directory, "state"))
    guards = _quote(os.path.join(directory, "guards"))
    try:
        if shell == "bash":
            rc = os.path.join(directory, "bashrc")
            content = "\n".join(_BASH_RC_LINES).replace("@@GUARDS@@", guards).replace("@@STATE@@", state)
            _write_private(rc, content)
            return ShellActivity(directory, [argv[0], "--rcfile", rc, "-i"], env)
        original = env.get("ZDOTDIR")
        first = "unset ZDOTDIR" if original is None else f"ZDOTDIR={_quote(original)}"
        content = ("\n".join(_ZSH_ENV_LINES)
                   .replace("@@FIRST@@", first)
                   .replace("@@STATE@@", state)
                   .replace("@@GUARDS@@", guards))
        _write_private(os.path.join(directory, ".zshenv"), content)
        return ShellActivity(directory, argv, {**env, "ZDOTDIR": directory})
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
