"""workflow-ptc 的校验与解析辅助（对齐 workflow-ptc/src/meta.ts + index.ts 前置段）。

上游锚点：
- `meta.ts:13-81` validateMeta —— 已由 workflow/validate_meta 承载。
- `index.ts:48-94`：META_STATEMENT / assertBodyParses / resolveSubagentProvider /
  resolveMaxTotalAgents。
"""
from __future__ import annotations

import re
from typing import Any

from ..workflow import WorkflowError

__all__ = [
    "META_STATEMENT",
    "assert_body_parses",
    "resolve_max_total_agents",
    "resolve_subagent_provider",
]

#: 脚本内嵌 meta 语句探测（index.ts:48）。
META_STATEMENT = re.compile(r"^\s*export\s+const\s+meta\b", re.MULTILINE)


def assert_body_parses(body: str, name: str) -> None:
    """脚本体语法校验（index.ts:53-63）：Python 用 ast 编译等价；不通过抛 SCRIPT_PARSE。"""
    import ast
    if META_STATEMENT.search(body):
        raise WorkflowError(
            "workflow meta rides the `meta` request field, not the script: "
            "remove the `export const meta = {...}` statement from the body",
            "SCRIPT_PARSE")
    try:
        # 工作流 body 是 async 函数体：以 `async def __wf__():\n<body>` 包装后编译。
        compile("async def __wf__():\n" + _indent(body), f"workflow:{name}", "exec")
    except SyntaxError as error:
        raise WorkflowError(
            f"workflow script does not parse: {error}", "SCRIPT_PARSE") from error


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.splitlines()) or "    pass"


def resolve_subagent_provider(ctx: Any, configured: str | None,
                              requested: str | None) -> str:
    """子代理 provider 决议（index.ts:67-79）：request 覆盖 ?? 配置；校验 + 注册表存在性。"""
    provider = requested if requested is not None else configured
    if provider is None or provider.strip() == "" or provider != provider.strip():
        raise WorkflowError(
            "workflow subagentProvider must be a non-empty normalized string",
            "INVALID_ARGUMENT")
    manager = ctx.get("subagents") or ctx.get("subagentManager")
    if manager is not None and hasattr(manager, "resolve_route"):
        # continuation manager 有 provider 路由面：探测其 provider 集合。
        names = getattr(manager, "provider_names", None)
        if names is not None and callable(names):
            if provider not in names():
                raise WorkflowError(
                    f'no subagent provider registered for "{provider}"',
                    "AGENT_START")
    return provider


def resolve_max_total_agents(requested: Any, ceiling: int) -> int:
    """总 agent 上限决议（index.ts:82-94）：undefined → ceiling；非安全正整数/超上限拒绝。"""
    if requested is None:
        return ceiling
    if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
        raise WorkflowError(
            "workflow maxTotalAgents must be a positive safe integer",
            "INVALID_ARGUMENT")
    if requested > ceiling:
        raise WorkflowError(
            f"workflow maxTotalAgents {requested} exceeds the engine ceiling {ceiling}",
            "INVALID_ARGUMENT")
    return requested