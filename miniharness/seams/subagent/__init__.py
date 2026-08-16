"""第 6 章：进阶接缝 —— 沙箱 / 凭据 / 子 agent（选做，各 2~3 天）。

三个接缝演示同一句话：**换一个 Provider，不改 Consumer，即换行为**。
"""
from __future__ import annotations

import os
import shlex
import subprocess
from typing import Any, Callable


# ---------- 1) 沙箱接缝 ----------

class Sandbox:
    """Service Definition：把 argv 包裹进受限执行环境。"""

    def wrap(self, argv: list[str]) -> list[str]:
        raise NotImplementedError


class PassthroughSandbox(Sandbox):
    """本地直通（danger：不设防，等同 danger-full-access）。"""

    def wrap(self, argv):
        return argv


class ReadOnlySandbox(Sandbox):
    """模拟只读沙箱：含写操作标志的命令直接拒绝（失败即拒）。"""

    WRITE_MARKERS = ("-w ", "--write", "-o ", ">", ">>", "rm ", "mv ", "touch ", "mkdir ")

    def wrap(self, argv):
        cmd = " ".join(argv)
        if any(marker in cmd for marker in self.WRITE_MARKERS):
            raise PermissionError(f"只读沙箱拒绝写操作: {cmd}")
        return argv


class CommandConsumer:
    """Consumer：只依赖 Sandbox 接口。换 Provider 即换行为。"""

    def __init__(self, sandbox: Sandbox):
        self._sandbox = sandbox

    def run(self, command: str) -> str:
        if os.name == "nt":
            # Windows 无 bash，命令经 cmd.exe 执行（内建命令可用）
            argv = self._sandbox.wrap([command])
            proc = subprocess.run(argv[0], shell=True, capture_output=True, text=True, timeout=10)
        else:
            argv = self._sandbox.wrap(shlex.split(command))
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return proc.stdout.strip() or proc.stderr.strip()


# ---------- 2) 凭据接缝 ----------

class CredentialProvider:
    """Service Definition：按操作解析凭据；配置只存引用，绝无明文。"""

    def resolve(self, key: str) -> str:
        raise NotImplementedError


class EnvCredentialProvider(CredentialProvider):
    """env-over-.env：配置项 -> 环境变量名（引用），每次调用解析。"""

    def __init__(self, mapping: dict[str, str] | None = None):
        self._mapping = mapping or {"api_key": "DEEPSEEK_API_KEY"}

    def resolve(self, key):
        env_name = self._mapping[key]
        value = os.environ.get(env_name, "")
        if not value:
            raise KeyError(f"凭据 {key}（环境变量 {env_name}）未配置")
        return value


# ---------- 3) 子 agent 接缝 ----------

class SubAgent:
    def run(self, task: str) -> str:
        raise NotImplementedError


class SubAgentProvider:
    """Service Definition：子 agent 工厂。"""

    def spawn(self, name: str, system_prompt: str) -> SubAgent:
        raise NotImplementedError


class InProcessSubAgentProvider(SubAgentProvider):
    """in-process Provider：复用主循环（简化版，真实还有 fork / ACP / Codex）。"""

    def __init__(self, make_loop: Callable[[str], Any]):
        self._make_loop = make_loop

    def spawn(self, name, system_prompt):
        return _InProcessSubAgent(self._make_loop(system_prompt))


class _InProcessSubAgent(SubAgent):
    def __init__(self, loop):
        self._loop = loop

    def run(self, task):
        self._loop.followup(task)
        return self._loop.last_response()


# ---------- 可继续子代理（A7 durable 子会话 + 冷恢复；A8 异步事件驱动） ----------

from .continuation import (  # noqa: E402
    CONTEXT_SUMMARY_MAX_CHARS,
    SubagentContinuationManager,
    SubagentError,
    bound_context_summary,
    delegation_depth_of,
    epoch_stop_reason,
    final_assistant_output,
    install_subagent_control_tools,
    settlement_summary,
)
from .descriptor import (  # noqa: E402
    SUBAGENT_DESCRIPTOR_VERSION,
    fold_subagent_descriptor,
    parse_subagent_descriptor,
    seed_descriptor_turn,
    snapshot_subagent_descriptor,
)