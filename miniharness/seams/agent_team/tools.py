"""Agent-scoped 模型侧 Team 工具 + team:policy 提示节（对齐上游 tool-agent-team/src/index.ts）。

契约要点：
  * 9 工具 + 一个常驻提示节，全部安装在调用方 agent 自己的 scope（scoped tools /
    scoped systemPrompt）；execute 经 exec_.agent 恢复精确 live 调用者（上游
    callingAgent），Team 域再经 service.membership 授权。
  * canonical value schema 全部是 fixed record，render 统一 JSON.stringify
    （上游 jsonOutput）：额外字段剔除靠 schema 里 additionalProperties:false
    对齐；本体用 as_dict 严格序列化（只输出声明字段）。
  * wait_agent 的 no-active-peer 快捷分支（上游 index.ts:238-260）：先校验
    timeout 范围，再单同步跨度读 active peer + 注册——无其它运行/供给成员则
    立即返回 noProgress{reason:'no-active-peer'}，不进入 wait。
  * spawn_teammate provider：context==='fork' 用 fork_provider，否则
    fresh_provider（上游 Config 默认 freshProvider='spawn' forkProvider='fork'）。
    mini 仅内建 fake 续跑适配器 → fresh 默认 provider='fake'（上游 'spawn' 的
    载体差异，verified-diffs §2.29）；'fork' 保留上游默认名以暴露 UNAVAILABLE
    失败路径。
"""

from __future__ import annotations

import json
from typing import Any

from ...core.tools import Tool, ToolExec
from .service import TeamService
from .types import (
    CreateTeamTaskRequest,
    SendTeamMessageRequest,
    SpawnTeammateRequest,
    UpdateTeamTaskRequest,
)

__all__ = ["install_agent_team_tools", "POLICY", "ACTIVE_WAIT_STATUSES"]

# 逐字上游 policy（index.ts:31-37）
POLICY = """Agent Teams is available in this session, but create teammates only when the user explicitly asks to use Agent Teams or teammates.

The Team Lead and all teammates share the same working directory and filesystem. Edits are immediately visible to every member. Split write work into disjoint scopes, record expected write scopes on shared tasks, and use task dependencies when work must be ordered. Write-scope overlap is advisory, not a lock.

Prefer read/edit/write for file changes. If a file operation returns FS_STALE_VERSION, read the current file, rebase your intended change onto the new content, and retry. Bash, formatters, code generators, and scripts are not fully protected by the filesystem version guard; coordinate them explicitly and have the Lead review the final diff and run tests.

send_message steers a running target at its nearest step boundary, starts an idle target, and cold-resumes an inactive teammate. A delivered peer item starts with its stable message id and sender name. A successful send is already durable even when its result says queued; do not resend it. Shared-task workflow is list, get, claim with the current revision, perform the work, then complete. Task readiness never starts an owner. Before wait_agent, use list_agents and make sure another required member is running or provisioning; use send_message first when the required member is inactive. wait_agent observes only changes after that call starts, never wakes a member, and returns noProgress immediately when no other member can produce a change. Re-list after wakeup or timeout. The Lead must wait for required teammates before giving the final answer."""

ACTIVE_WAIT_STATUSES = frozenset({"running", "provisioning"})

_NO_ACTIVE_PEER_MESSAGE = (
    "No other Team member is running or provisioning. wait_agent cannot make progress or wake "
    "inactive teammates. Re-list with list_agents and team_task_list, then use send_message to "
    "wake each required inactive teammate before waiting again."
)


def _member_dict(member) -> dict:
    out = {"id": member.id, "name": member.name, "role": member.role,
           "status": member.status, "diagnostics": list(member.diagnostics)}
    if member.description is not None:
        out["description"] = member.description
    if member.provider is not None:
        out["provider"] = member.provider
    if member.context is not None:
        out["context"] = member.context
    if member.model is not None:
        out["model"] = member.model
    return out


def _task_dict(task) -> dict:
    out = {"id": task.id, "revision": task.revision, "subject": task.subject,
           "description": task.description, "status": task.status,
           "blockedBy": list(task.blockedBy), "writeScopes": list(task.writeScopes),
           "ready": task.ready, "writeScopeWarnings": list(task.writeScopeWarnings)}
    if task.ownerName is not None:
        out["ownerName"] = task.ownerName
    return out


def _render(value) -> list:
    return [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]


def _calling_agent(exec_: ToolExec, tool_name: str):
    agent = exec_.agent
    if agent is None:
        raise RuntimeError(f"{tool_name} requires a calling Agent")
    return agent


def _section_text(service: TeamService, agent) -> str:
    membership = service.membership(agent)
    return (
        f"{POLICY}\n\nYour Team role is {membership.role}; your Team name is {membership.name}; "
        f"Team id is {membership.id}."
    )


def install_agent_team_tools(
    ctx: Any,
    reg: Any,
    service: TeamService,
    fresh_provider: str = "fake",
    fork_provider: str = "fork",
) -> None:
    """在一个 Team 成员 scope 安装 9 工具 + team:policy 提示节。"""

    def policy(assembly):
        agent = assembly.get("agent")
        if agent is None:
            return POLICY
        try:
            return _section_text(service, agent)
        except Exception:
            return POLICY

    svc = ctx.get("systemPrompt")
    if svc is not None and getattr(svc, "section", None) is not None:
        svc.section("team:policy", 99, policy)

    tools = [
        _spawn_teammate_tool(service, fresh_provider, fork_provider),
        _send_message_tool(service),
        _list_agents_tool(service),
        _wait_agent_tool(service),
        _interrupt_agent_tool(service),
        _team_task_create_tool(service),
        _team_task_list_tool(service),
        _team_task_get_tool(service),
        _team_task_update_tool(service),
    ]
    for tool in tools:
        reg.register(tool)


def _spawn_teammate_tool(service, fresh_provider: str, fork_provider: str) -> Tool:
    async def execute(args: dict, exec_: ToolExec):
        agent = _calling_agent(exec_, "spawn_teammate")
        context = args.get("context") or "fresh"
        result = await service.spawn_teammate_async(agent, SpawnTeammateRequest(
            name=args["name"],
            description=args["description"],
            prompt=({"type": "text", "text": args["prompt"]},),
            context=context,
            provider=fork_provider if context == "fork" else fresh_provider,
        ))
        return {"member": _member_dict(result.member)}

    return Tool(
        name="spawn_teammate",
        description=(
            "Create one named, durable teammate. Only the Team Lead may call this tool."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique lower-kebab-case teammate name."},
                "description": {"type": "string", "description": "Short description of the delegated responsibility."},
                "prompt": {"type": "string", "description": "Complete initial task for the teammate."},
                "context": {
                    "type": "string", "enum": ["fresh", "fork"],
                    "description": "fresh starts without Lead history; fork inherits completed Lead turns. Defaults to fresh.",
                },
            },
            "required": ["name", "description", "prompt"],
        },
        execute=execute,
        render=_render,
    )


def _send_message_tool(service) -> Tool:
    def execute(args: dict, exec_: ToolExec):
        agent = _calling_agent(exec_, "send_message")
        result = service.send_message(agent, SendTeamMessageRequest(
            target=args["target"],
            content=({"type": "text", "text": args["message"]},),
        ))
        return {"messageId": result.messageId, "status": result.status}

    return Tool(
        name="send_message",
        description=(
            "Send one durable message to another Team member. A running target receives it at the "
            "nearest step boundary; an idle target starts a turn; an inactive teammate cold-resumes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Team member name, or lead."},
                "message": {"type": "string", "description": "Self-contained message for the target."},
            },
            "required": ["target", "message"],
        },
        execute=execute,
        render=_render,
    )


def _list_agents_tool(service) -> Tool:
    def execute(args: dict, exec_: ToolExec):
        agent = _calling_agent(exec_, "list_agents")
        return [_member_dict(m) for m in service.list_members(agent)]

    return Tool(
        name="list_agents",
        description="List the Lead and every durable teammate with current runtime status.",
        parameters={"type": "object", "properties": {}},
        execute=execute,
        render=_render,
    )


def _wait_agent_tool(service) -> Tool:
    async def execute(args: dict, exec_: ToolExec):
        caller = _calling_agent(exec_, "wait_agent")
        timeout_ms = (args.get("timeout_ms")
                      if args.get("timeout_ms") is not None else 30_000)
        if (not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool)
                or not (10_000 <= timeout_ms <= 3_600_000)):
            # 交给 service 权威校验（活动层同款范围检查，可能抛 TeamError）
            return _wait_result(service.wait_for_change(caller, timeout_ms))
        has_active_peer = any(
            m.id != caller.id and m.status in ACTIVE_WAIT_STATUSES
            for m in service.list_members(caller)
        )
        if not has_active_peer:
            return {"timedOut": False, "noProgress": {
                "reason": "no-active-peer", "message": _NO_ACTIVE_PEER_MESSAGE,
            }}
        return _wait_result(service.wait_for_change(caller, timeout_ms))

    return Tool(
        name="wait_agent",
        description=(
            "Wait for the next teammate status, mailbox, or shared-task change after this call "
            "starts. This never wakes inactive members and returns noProgress immediately when no "
            "other member is running or provisioning. Re-list after wakeup or timeout instead of polling."
        ),
        parameters={
            "type": "object",
            "properties": {
                "timeout_ms": {
                    "type": "integer",
                    "description": "Wait duration in milliseconds, from 10000 through 3600000. Defaults to 30000.",
                },
            },
        },
        execute=execute,
        render=_render,
    )


def _wait_result(result) -> dict:
    return {"timedOut": result.timed_out}


def _interrupt_agent_tool(service) -> Tool:
    def execute(args: dict, exec_: ToolExec):
        agent = _calling_agent(exec_, "interrupt_agent")
        return {"previousStatus": service.interrupt(agent, args["target"])["previousStatus"]}

    return Tool(
        name="interrupt_agent",
        description="Interrupt one teammate's current turn while preserving its pending inbox. Team Lead only.",
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Teammate name."},
            },
            "required": ["target"],
        },
        execute=execute,
        render=_render,
    )


def _team_task_create_tool(service) -> Tool:
    async def execute(args: dict, exec_: ToolExec):
        agent = _calling_agent(exec_, "team_task_create")
        request = CreateTeamTaskRequest(
            subject=args["subject"],
            description=args["description"],
            blockedBy=tuple(args.get("blocked_by") or ()),
            writeScopes=tuple(args.get("write_scopes") or ()),
        )
        return _task_dict(service.create_task(agent, request))

    return Tool(
        name="team_task_create",
        description="Create one unowned pending task on the shared Team task board.",
        parameters={
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Concise task title."},
                "description": {"type": "string", "description": "Complete task details and acceptance criteria."},
                "blocked_by": {"type": "array", "items": {"type": "string"}, "description": "Task ids that must complete first."},
                "write_scopes": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Advisory workspace-relative file or directory prefixes this task expects to modify.",
                },
            },
            "required": ["subject", "description"],
        },
        execute=execute,
        render=_render,
    )


def _team_task_list_tool(service) -> Tool:
    def execute(args: dict, exec_: ToolExec):
        caller = _calling_agent(exec_, "team_task_list")
        status = args.get("status")
        owner = args.get("owner")
        ready = args.get("ready")
        filtered = [
            t for t in service.list_tasks(caller)
            if (status is None or t.status == status)
            and (owner is None or ("unowned" if t.ownerName is None else t.ownerName) == owner)
            and (ready is None or t.ready == ready)
        ]
        cursor = args.get("cursor") if args.get("cursor") is not None else 0
        limit = args.get("limit") if args.get("limit") is not None else 50
        if (not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0):
            raise RuntimeError("cursor must be a non-negative safe integer")
        if (not isinstance(limit, int) or isinstance(limit, bool)
                or not (1 <= limit <= 100)):
            raise RuntimeError("limit must be an integer from 1 through 100")
        page = [_task_dict(t) for t in filtered[cursor:cursor + limit]]
        out = {"tasks": page}
        if cursor + limit < len(filtered):
            out["nextCursor"] = cursor + limit
        return out

    return Tool(
        name="team_task_list",
        description="List shared tasks, including readiness, owner, revision, blockers, and write-scope warnings.",
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"], "description": "Optional exact status filter."},
                "owner": {"type": "string", "description": "Optional member-name filter; use unowned for tasks without an owner."},
                "ready": {"type": "boolean", "description": "Optional readiness filter."},
                "cursor": {"type": "integer", "description": "Zero-based result offset. Defaults to 0."},
                "limit": {"type": "integer", "description": "Number of rows, 1 through 100. Defaults to 50."},
            },
        },
        execute=execute,
        render=_render,
    )


def _team_task_get_tool(service) -> Tool:
    def execute(args: dict, exec_: ToolExec):
        agent = _calling_agent(exec_, "team_task_get")
        return _task_dict(service.get_task(agent, args["task_id"]))

    return Tool(
        name="team_task_get",
        description="Read the complete latest value of one shared task before changing or executing it.",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Shared task id."},
            },
            "required": ["task_id"],
        },
        execute=execute,
        render=_render,
    )


def _team_task_update_tool(service) -> Tool:
    async def execute(args: dict, exec_: ToolExec):
        agent = _calling_agent(exec_, "team_task_update")
        request = UpdateTeamTaskRequest(
            taskId=args["task_id"],
            expectedRevision=args["expected_revision"],
            action=args["action"],
            subject=args.get("subject"),
            description=args.get("description"),
            blockedBy=tuple(args.get("blocked_by")) if args.get("blocked_by") is not None else None,
            writeScopes=tuple(args.get("write_scopes")) if args.get("write_scopes") is not None else None,
            owner=args.get("owner"),
        )
        return _task_dict(service.update_task(agent, request))

    return Tool(
        name="team_task_update",
        description=(
            "Compare-and-set a shared task action using the latest revision from team_task_get or team_task_list."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Shared task id."},
                "expected_revision": {"type": "integer", "description": "Current task revision used as the CAS precondition."},
                "action": {
                    "type": "string",
                    "enum": ["claim", "release", "edit", "set_dependencies", "complete", "reopen", "reassign", "delete"],
                    "description": "Task transition to apply.",
                },
                "subject": {"type": "string", "description": "Replacement title for edit."},
                "description": {"type": "string", "description": "Replacement details for edit."},
                "blocked_by": {"type": "array", "items": {"type": "string"}, "description": "Complete blocker list for set_dependencies."},
                "write_scopes": {"type": "array", "items": {"type": "string"}, "description": "Replacement advisory write scopes for edit."},
                "owner": {"type": "string", "description": "Member name for Lead-only reassign; omit to unassign."},
            },
            "required": ["task_id", "expected_revision", "action"],
        },
        execute=execute,
        render=_render,
    )