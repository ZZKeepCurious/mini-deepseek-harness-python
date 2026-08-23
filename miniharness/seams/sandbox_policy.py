"""sandbox-policy —— 沙箱策略服务（ctx.sandboxPolicy）：部署缺省 + 逐会话决议。

对应 dsh 真实源码：packages/sandbox/sandbox-policy/src/index.ts（SandboxPolicyService）
+ session-mode.ts（`sandbox/mode` 事件 fold 与写路径）。

职责（上游模块 docstring 同构）：
  * 唯一持有部署缺省（mode，fail-safe 缺省 read-only）与无会话 cwd 时的
    workspace-write 回退根；每次能力调用经 resolve(request) 决议完整策略
    {mode, workspaceRoot, sessionId?}——bash / fs 等强制面读同一份决议，
    会话的模式日志与不可变 cwd 一起到达每个执行点。
  * 会话覆盖以**会话日志为存储**：运行时切换 = 追加恰一条 `sandbox/mode`
    log-only 事件（set_sandbox_mode 是唯一写路径）；effective = 最后一条
    覆盖 ?? 部署缺省。重放即状态，重启无需追赶机制，两会话互不可见。
  * workspace 根先 canonical（realpath）后词法规范化（abspath）：符号链接
    在词法折叠抹掉敏感分量之前解析（上游 resolveWorkspaceRoot 同序）。
    会话 cwd 即其 workspace-write 边界；agentless 调用回退配置根。

与 ctx.sandbox（seams/sandbox_local.py）的分工：policy 是「决定用什么模式
和根」，provider 是「把模式物化为具体 runner 的 argv」；消费者（shell 层
bash 执行器）逐调用把两者接起来。
"""

from __future__ import annotations

import json
import os

from ..core.scope import Context, Service
from ..core.session.session import Session
from .sandbox_local import SandboxUnavailableError, canonical_path

__all__ = [
    "SANDBOX_MODES",
    "SandboxPolicyService",
    "effective_sandbox_mode",
    "render_policy_context",
    "set_sandbox_mode",
]

SANDBOX_MODES: tuple[str, ...] = ("read-only", "workspace-write", "danger-full-access")


def effective_sandbox_mode(events) -> str | None:
    """会话的沙箱模式覆盖：日志中最后一条 `sandbox/mode` 的 mode，无则 None。

    纯 fold（上游 effectiveSandboxMode）：逆序扫描，其余事件类型跳过；
    冷恢复不需要任何追赶机制——重放日志本身即是状态。
    """
    for event in reversed(events):
        if event["type"] == "sandbox/mode":
            return event["data"]["mode"]
    return None


def set_sandbox_mode(session: Session, mode: str) -> None:
    """会话沙箱覆盖的唯一写路径：追加恰一条 `sandbox/mode` 事件。

    切换即事件本身，不存在带外可变的状态；对后续受限调用生效
    （消费者每次读都重新 fold）。非 SANDBOX_MODES 内的字符串 fail loud
    （上游为类型联合编译期保证，mini 在此运行期校验不受信输入）。
    """
    if mode not in SANDBOX_MODES:
        raise ValueError(f"unknown sandbox mode: {mode!r} (known: {list(SANDBOX_MODES)})")
    session.append("sandbox/mode", {"mode": mode})


def _resolve_workspace_root(path: str) -> str:
    """先 canonical 后词法规范化（上游 resolveWorkspaceRoot：resolve(canonicalPath(p))）。

    realpath 解析失败（根缺失/不可读）时保留原拼写——缺失的根匹配不到任何
    探测路径，是保守结局；发明回退值会授予调用方从未点名的路径。
    """
    return os.path.abspath(canonical_path(path))


def render_policy_context(policy: dict) -> str:
    """渲染策略上下文文本（不声明挂载了哪些能力；三档文案对齐上游逐字）。"""
    mode = policy["mode"]
    if mode == "read-only":
        return ("Current DSH file policy: read-only. Any available operation enforced by "
                "the DSH file sandbox cannot modify files in the standing mode. Do not "
                "refuse a required modification from this policy alone: try an available "
                "tool normally and follow any denial and escalation guidance it returns.")
    if mode == "workspace-write":
        return (f"Current DSH file policy: workspace-write. Any available operation enforced by "
                f"the DSH file sandbox may modify files under the session workspace: "
                f"{json.dumps(policy['workspaceRoot'])}. Some platform temporary areas may also be writable.")
    if mode == "danger-full-access":
        return ("Current DSH file policy: danger-full-access. The DSH file sandbox does not "
                "restrict file modifications by available operations.")
    raise SandboxUnavailableError(mode, f"unreachable sandbox mode: {mode}")


class SandboxPolicyService(Service):
    """ctx.sandboxPolicy（上游 SandboxPolicyService）。

    Config：{mode?: 沙箱缺省（read-only），workspaceRoot?: 无会话 cwd 的
    回退根（进程 cwd）}。runner 选择不在这里——那是 ctx.sandbox provider
    的配置；这是唯一的共享策略家。
    """

    provide = "sandboxPolicy"

    def __init__(self, ctx: Context, config: dict | None = None):
        config = dict(config or {})
        mode = config.get("mode", "read-only")
        if mode not in SANDBOX_MODES:
            raise ValueError(f"unknown sandbox mode: {mode!r} (known: {list(SANDBOX_MODES)})")
        self.default_mode: str = mode
        self.workspace_root: str = _resolve_workspace_root(
            config.get("workspaceRoot") or os.getcwd())
        super().__init__(ctx, "sandboxPolicy")
        # 策略进模型可见上下文：装配时按调用方会话决议当前模式与根
        # （上游 ctx.inject(['systemPrompt'], …) 同款接线）。模型历史经
        # request/header 快照重建同一决议，稳定系统提示无需改写。
        ctx.inject(["systemPrompt"], self._mount_prompt_section)

    def _mount_prompt_section(self, ctx: Context, _config=None) -> None:
        prompt = ctx.get("systemPrompt")
        if prompt is None:
            return
        prompt.context(
            name="sandbox:policy",
            order=110,
            text=lambda context: self._section_text(context),
        )

    def _section_text(self, context: dict) -> str:
        session = (context or {}).get("session")
        if session is None:
            return ""
        return render_policy_context(self.resolve({"session": session}))

    def resolve(self, request: dict | None = None) -> dict:
        """一次能力调用的完整策略决议。

        approved 显式 mode > 会话最后一条 `sandbox/mode` > 部署缺省；
        会话 cwd 是 workspace-write 边界，配置根是 agentless 回退；
        有会话时附带 sessionId。
        """
        request = dict(request or {})
        session = request.get("session")
        override = self.override_of(session) if session is not None else None
        policy: dict = {
            "mode": request.get("mode") or override or self.default_mode,
            "workspaceRoot": _resolve_workspace_root(
                (session.meta.get("cwd") if session is not None else None)
                or self.workspace_root),
        }
        if session is not None:
            policy["sessionId"] = session.session_id
        return policy

    def override_of(self, session: Session) -> str | None:
        """只读会话覆盖：不施加部署缺省（无覆盖返回 None 由调用方兜底）。"""
        return effective_sandbox_mode(session.events)
