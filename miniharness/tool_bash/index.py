"""bash 工具（ctx.shell 的模型面消费者，对齐 packages/shell/tool-bash/src/index.ts）。

契约要点：
  * 有 `ctx.jobs` 时前台调用作为作业被等待：超时不再杀命令，而是提升为后台作业并
    返回 `{kind:'promoted', jobId, timeoutMs, output}`；`run_in_background` 立即返回
    `{kind:'background', jobId}`。
  * 无 registry 时工具为纯前台，执行器 deadline 杀命令。
  * 前台结算投影为 `{kind:'foreground', exitCode, signal, timedOut, aborted,
    stopped?, timeoutMs, stdout, stderr, sandbox?}`；外部 kill 原因经 `stopped` 呈现。
  * 作业 owner 是调用方 agent id；后台进程经 `process_sources` 把非消耗观测流泵入
    作业 ring。

载体说明（登录 verified-diffs）：mini 无 schemastery 配置链，Config 以普通 dict +
校验承载；无沙箱升级审批面（`sandbox_permissions` 不广告）；工具一次装配——jobs 在
注册时在场即用作业尾巴，否则前台尾巴（上游 registration-as-effect 的 foreground→
job-backed 换装未承载）。
"""
from __future__ import annotations

import asyncio
import os
import threading

from ..core.tools import Tool
from ..jobs import JobDoneBox
from ..shell.types import is_aborted
from .background import process_outcome, process_sources, ring_delta
from .render import render_job_read, render_promoted, render_result

__all__ = [
    "DEFAULT_CONFIG",
    "PLUGIN_NAME",
    "create_bash_tool",
    "install_tool_bash",
    "resolve_config",
]

#: 上游 plugin name（index.ts:33）。
PLUGIN_NAME = "tool-bash"

#: Config 缺省（index.ts:56-59）。
DEFAULT_CONFIG = {
    "enableRunInBackground": True,
    "promoteOnTimeout": True,
}

#: 前台 stdout 捕获预算（字节）——工具层不广告，走执行器缺省。
_BACKGROUND_OUTPUT_PROPERTIES = {
    "kind": {"type": "string", "required": True, "const": "background"},
    "jobId": {"type": "string", "required": True},
}


class ToolAborted(Exception):
    """前台路径在调用方取消时抛出的结构化中止。"""


def resolve_config(config: dict | None) -> dict:
    """解析 tool-bash 配置；未知键取证。"""
    config = dict(config or {})
    unknown = set(config) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError(f"tool-bash: unknown config keys: {sorted(unknown)!r}")
    return {**DEFAULT_CONFIG, **config}


def _output_schema() -> dict:
    """前台/后台/提升三态输出 schema（index.ts:429-491，载体形状）。"""
    output_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": True,
        "properties": {
            "text": {"type": "string", "required": True},
            "truncated": {"type": "boolean", "required": True},
            "spillPath": {"type": "string"},
        },
    }
    return {
        "oneOf": [
            {"type": "object", "additionalProperties": False,
             "properties": dict(_BACKGROUND_OUTPUT_PROPERTIES)},
            {"type": "object", "additionalProperties": False, "properties": {
                "kind": {"type": "string", "required": True, "const": "promoted"},
                "jobId": {"type": "string", "required": True},
                "timeoutMs": {"type": "number", "required": True},
                "output": {"type": "string", "required": True},
            }},
            {"type": "object", "additionalProperties": False, "properties": {
                "kind": {"type": "string", "required": True, "const": "foreground"},
                "exitCode": {"required": True, "oneOf": [{"type": "integer"}, {"type": "null"}]},
                "signal": {"required": True, "oneOf": [{"type": "string"}, {"type": "null"}]},
                "timedOut": {"type": "boolean", "required": True},
                "aborted": {"type": "boolean", "required": True},
                "stopped": {"type": "string"},
                "timeoutMs": {"type": "number", "required": True},
                "stdout": output_schema,
                "stderr": output_schema,
                "sandbox": {"type": "object", "additionalProperties": False, "properties": {
                    "mode": {"type": "string", "required": True},
                    "denied": {"type": "boolean", "required": True},
                    "enforcement": {"type": "string"},
                    "runnerFailed": {"type": "boolean"},
                }},
            }},
        ],
    }


def _canonical(result: dict) -> dict:
    """执行器结果 → 普通 JSON 数据（index.ts:187-210）。"""
    def output(stream: dict) -> dict:
        rendered = {"text": stream["text"], "truncated": stream["truncated"]}
        if stream.get("spillPath") is not None:
            rendered["spillPath"] = stream["spillPath"]
        return rendered

    canonical = {
        "exitCode": result["exitCode"],
        "signal": result["signal"],
        "timedOut": result["timedOut"],
        "aborted": result["aborted"],
        "timeoutMs": result["timeoutMs"],
        "stdout": output(result["stdout"]),
        "stderr": output(result["stderr"]),
    }
    sandbox = result.get("sandbox")
    if sandbox is not None:
        canonical["sandbox"] = {
            "mode": sandbox["mode"],
            "denied": sandbox["denied"],
            **({"enforcement": sandbox["enforcement"]} if "enforcement" in sandbox else {}),
            **({"runnerFailed": True} if sandbox.get("runnerFailed") else {}),
        }
    return canonical


def _resolve_workdir(model_workdir, session, policy) -> str | None:
    """显式 workdir 优先（相对路径按会话工作区解析），否则会话 cwd。"""
    session_cwd = policy.get("workspaceRoot") if policy is not None else None
    if session_cwd is None and session is not None:
        session_cwd = (getattr(session, "meta", None) or {}).get("cwd")
    if model_workdir is None:
        return session_cwd
    if session_cwd is not None and not os.path.isabs(model_workdir):
        return os.path.join(session_cwd, model_workdir)
    return model_workdir


def _validate_args(args: dict) -> None:
    command = args.get("command")
    if not isinstance(command, str) or command.strip() == "":
        raise ValueError("invalid command: expected a non-empty string")
    if not isinstance(args.get("description"), str) or args["description"].strip() == "":
        raise ValueError("invalid description: expected a non-empty string")
    timeout = args.get("timeoutMs")
    if timeout is not None and (isinstance(timeout, bool)
                                or not isinstance(timeout, (int, float))
                                or timeout != timeout or timeout <= 0):
        raise ValueError(
            f"invalid timeoutMs: expected a positive number, got {timeout!r}")


def _start_job(shell, jobs, label: str, owner, spec: dict, escalation_modes):
    """把一个已解析 spec 注册为 bash 作业；返回 {id, process, stopped}。"""
    state = {"proc": None}
    stopped = {"reason": None}
    job_signal = threading.Event()

    def run(_handle):
        proc = shell.execute({**spec, "signal": job_signal})
        state["proc"] = proc
        box = JobDoneBox()
        proc.done.add_done_callback(lambda _f: box.settle(process_outcome(proc, escalation_modes)))

        def cancel(reason=None):
            stopped["reason"] = reason if reason is not None else "stopped"
            job_signal.set()
            proc.kill()

        return {"done": box, "cancel": cancel}

    spec_dict = {
        "kind": "bash",
        "label": label,
        "owner": owner,
        "output": process_sources(lambda: state["proc"]),
        "run": run,
    }
    job_id = jobs.start(spec_dict)
    return {"id": job_id, "process": lambda: state["proc"],
            "stopped": lambda: stopped["reason"]}


def _stop_and_settle(jobs, job_id: str, owner, reason: str) -> dict:
    """取消自己的作业并等它结算，随后移除记录（工具自身账，不发 notice）。"""
    jobs.kill(job_id, owner, reason)
    settled = jobs.wait(job_id, 30_000, owner)
    if settled["status"] not in ("running", "stopping"):
        jobs.remove(job_id, owner)
    return settled


def _wait_on_job(jobs, attached: dict, owner, spec: dict, escalation_modes,
                 signal=None) -> dict:
    """等待已注册前台作业至结算或 timeout（index.ts:326-394）。"""
    timeout_ms = spec["timeoutMs"]
    job_id = attached["id"]
    try:
        view = jobs.wait(job_id, timeout_ms, owner, signal)
    except RuntimeError:
        # 调用方取消：命令随调用结束（等价 deadline kill）。
        _stop_and_settle(jobs, job_id, owner, "tool call aborted")
        raise ToolAborted("tool call aborted")
    if view["status"] in ("running", "stopping") and attached["process"]() is None:
        # 准备阶段就到期：没有可保留的运行进程，按执行器 deadline 的 settled 空结果。
        _stop_and_settle(jobs, job_id, owner, "timed out during preparation")
        return {
            "kind": "foreground", "exitCode": None, "signal": None,
            "timedOut": True, "aborted": False, "timeoutMs": timeout_ms,
            "stdout": {"text": "", "truncated": False},
            "stderr": {"text": "", "truncated": False},
        }
    if view["status"] in ("running", "stopping"):
        read = jobs.read(job_id, owner)
        proc = attached["process"]()
        return {
            "kind": "promoted", "jobId": job_id, "timeoutMs": timeout_ms,
            "output": render_job_read(
                ring_delta(read["chunks"]), read["lossy"],
                read["job"].get("spillPaths") or [],
                proc.sandbox if proc is not None else None, escalation_modes),
        }
    jobs.remove(job_id, owner)
    proc = attached["process"]()
    if proc is None:
        raise RuntimeError(view.get("detail") or "bash job failed before producing a process")
    result = proc.result()
    canonical = {"kind": "foreground", **_canonical(result)}
    stopped = attached["stopped"]()
    if stopped is not None:
        canonical["stopped"] = stopped
    return canonical


def create_bash_tool(ctx, shell, config: dict | None = None) -> Tool:
    """构造 `bash` 工具（jobs 在场时前台等待转为作业并支持提升）。"""
    cfg = resolve_config(config)
    policy_service = ctx.get("sandboxPolicy")
    jobs = ctx.get("jobs") if cfg["enableRunInBackground"] else None
    background_available = jobs is not None
    promote = background_available and cfg["promoteOnTimeout"]
    escalation_modes: tuple = ()

    def agent_of(exec_):
        return getattr(exec_, "agent", None)

    def resolve_policy(exec_) -> dict | None:
        if policy_service is None:
            return None
        session = getattr(agent_of(exec_), "session", None)
        return policy_service.resolve({"session": session} if session is not None else {})

    def build_request(args: dict, exec_) -> dict:
        agent = agent_of(exec_)
        session = getattr(agent, "session", None)
        policy = resolve_policy(exec_)
        workdir = _resolve_workdir(args.get("workdir"), session, policy)
        request: dict = {"command": args["command"],
                         "dshEnv": _collect_dsh_env(ctx, exec_)}
        if workdir is not None:
            request["workdir"] = workdir
        if args.get("timeoutMs") is not None:
            request["timeoutMs"] = args["timeoutMs"]
        if policy is not None:
            request["sandboxPolicy"] = policy
        return request

    async def execute(args: dict, exec_) -> dict:
        _validate_args(args)
        request = build_request(args, exec_)
        agent = agent_of(exec_)
        owner = getattr(agent, "id", None)
        if args.get("run_in_background") is True:
            if not cfg["enableRunInBackground"]:
                raise RuntimeError(
                    "run_in_background is disabled for this deployment "
                    "(enableRunInBackground: false)")
            if jobs is None:
                raise RuntimeError(
                    "background jobs unavailable: load @deepseek-ai/dsh-jobs and "
                    "@deepseek-ai/dsh-tool-jobs")
            if is_aborted(getattr(exec_, "signal", None)):
                raise ToolAborted("tool call aborted")
            spec = shell.resolve({**request, "onExpiry": "none"})
            attached = _start_job(shell, jobs, args["command"], owner, spec, escalation_modes)
            return {"kind": "background", "jobId": attached["id"]}
        if promote:
            spec = shell.resolve({**request, "onExpiry": "none"})
            attached = None
            try:
                attached = _start_job(shell, jobs, args["command"], owner, spec,
                                      escalation_modes)
            except Exception as error:  # noqa: BLE001 - 准入拒绝回退前台 deadline kill
                _warn(ctx, f"bash: job registration refused, running in the foreground "
                           f"with the timeout kill instead: {error}")
            if attached is not None:
                return await asyncio.to_thread(
                    _wait_on_job, jobs, attached, owner, spec, escalation_modes,
                    getattr(exec_, "signal", None))
        spec = shell.resolve({**request, "signal": getattr(exec_, "signal", None)})
        execution = shell.execute(spec)
        result = await asyncio.to_thread(execution.result)
        if result.get("aborted"):
            raise ToolAborted("tool call aborted")
        return {"kind": "foreground", **_canonical(result)}

    def render(args: dict, value: dict) -> list:
        if value["kind"] == "background":
            text = f"started background job {value['jobId']}"
        elif value["kind"] == "promoted":
            text = render_promoted(value)
        else:
            text = render_result(value, escalation_modes)
        return [{"type": "text", "text": text}]

    properties: dict = {
        "command": {"type": "string", "description": "The bash command to execute."},
        "description": {
            "type": "string",
            "description": "Clear, concise description of what this command does in "
                           "active voice, 5-10 words (shown in the UI).",
        },
        "timeoutMs": {
            "type": "number",
            "description": ("Timeout in milliseconds. The executor applies its configured "
                            "default and cap; on expiry the command moves to the background "
                            "as a job instead of being killed." if promote else
                            "Timeout in milliseconds. The executor applies its configured "
                            "default and cap, and kills the command on expiry."),
        },
        "workdir": {
            "type": "string",
            "description": "Working directory for this command. Defaults to the session "
                           "workspace; a relative path is resolved against it.",
        },
    }
    if background_available:
        properties["run_in_background"] = {
            "type": "boolean",
            "description": "Run in the background and return a job id immediately "
                           "(collect with job_output, stop with job_kill). No timeout applies.",
        }

    return Tool(
        name="bash",
        description=(
            "Execute a bash command (`bash -c`) and return its stdout/stderr. Each call runs "
            "in a fresh shell: no state persists between calls. Non-zero exits are reported "
            "as `[exit code: N]`."),
        parameters={"type": "object", "properties": properties,
                    "required": ["command", "description"]},
        output={"schema": _output_schema()},
        execute=execute,
        render=render,
    )


def install_tool_bash(ctx, config: dict | None = None):
    """确保 shellEnv 服务在场并把 `bash` 工具注册进 ctx.tools（幂等）。"""
    shell = ctx.get("shell")
    if shell is None:
        return None
    from ..shell.env import install_shell_env
    install_shell_env(ctx)
    registry = ctx.get("tools")
    if registry is None:
        from ..core.tools import ToolRegistry
        registry = ToolRegistry(ctx)
    tool = create_bash_tool(ctx, shell, config)
    registry.register(tool)
    return tool


def _collect_dsh_env(ctx, exec_) -> dict:
    """经 ctx.shellEnv 收集托管 `DSH_*`；服务缺席时不注入（空快照）。"""
    registry = ctx.get("shellEnv")
    return registry.collect(exec_) if registry is not None else {}


def _warn(ctx, message: str) -> None:
    logger = getattr(ctx, "logger", None)
    if logger is not None and hasattr(logger, "warn"):
        logger.warn(message)
