"""tool-workflow：模型面 workflow 工具（对齐上游 tool-workflow/src/index.ts）。

模型写一个 **Python 工作流脚本体**（载体差异：上游为 JavaScript），经
`ctx.workflowEngine.start` 执行；前台调用等整个脚本完成，`run_in_background`
注册后台作业并立即返回 job id。顶层调用记录四个 durable 事件：
`tool-workflow/run-start` / `agent-start` / `agent-end` / `run-end`。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from ..core.tools import Tool, ToolExec
from ..jobs import JobDoneBox
from ..workflow import WorkflowError, is_fatal_workflow_error

__all__ = [
    "DEFAULT_CONFIG",
    "PLUGIN_NAME",
    "create_workflow_tool",
    "install_tool_workflow",
    "resolve_config",
]

#: 上游 plugin name（index.ts:40）。
PLUGIN_NAME = "tool-workflow"

#: Config 缺省（index.ts:44-63）。
DEFAULT_CONFIG = {
    "toolName": "workflow",
    "maxResultChars": 50_000,
    "enableRunInBackground": True,
}

_BACKGROUND_CLOSING = (
    "Set `run_in_background: true` for a long run: the call returns a job id "
    "immediately, the run keeps orchestrating in the background, and its return "
    "value arrives with the job's completion notice (check on it with `job_output`, "
    "stop it with `job_kill`)."
)
_FOREGROUND_ONLY_CLOSING = (
    " The run executes in the foreground: this call returns when the whole script "
    "finishes."
)


def resolve_config(config: dict | None) -> dict:
    config = dict(config or {})
    unknown = set(config) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError(f"tool-workflow: unknown config keys: {sorted(unknown)!r}")
    max_chars = config.get("maxResultChars", DEFAULT_CONFIG["maxResultChars"])
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
        raise ValueError("tool-workflow: maxResultChars must be a positive integer")
    return {**DEFAULT_CONFIG, **config}


def _description(background_enabled: bool) -> str:
    base = (
        "Run a Python workflow script that orchestrates subagents at scale. Use this "
        "for work that fans out across many independent pieces — an audit over many "
        "files, a migration, multi-angle research, adversarial verification of findings "
        "— where you write the orchestration as a script instead of delegating turn by "
        "turn.\n\n"
        "The workflow's identity rides the `meta` parameter as JSON: required `name` "
        "(short kebab-case) and `description` strings, optional `whenToUse` string and "
        "`phases` array (`{title, detail?, provider?, model?}`). The `script` parameter "
        "is the plain Python body ONLY (NO `export const meta` statement — meta is a "
        "parameter, not code), running with top-level `await`; end with `return <value>` "
        "— the value must be JSON-serializable and is this tool's result.\n\n"
        "Script-body hooks:\n"
        "- `agent(prompt, opts?)` — run one subagent to completion. Without `opts.schema` "
        "it resolves to the child's final text; with `opts.schema` (an object-rooted JSON "
        "Schema using ONLY type/properties/required/additionalProperties/items/enum/const/"
        "oneOf) it resolves to the validated object. Resolves `None` when the child fails "
        "(filter with `filter(None)`). Other opts: `label` (display), `phase` (progress "
        "group), and independent `provider`/`model` LLM target overrides (either may be "
        "provided alone). Anything else (`effort`/`isolation`/`agentType`) is rejected "
        "loudly.\n"
        "- `pipeline(items, *stages)` — run each item through the stages independently "
        "with NO barrier between stages. Each stage receives `(prev, item, index)`. An "
        "ordinary stage throw drops that ITEM to `None` and skips its remaining stages.\n"
        "- `parallel(thunks)` — run zero-argument functions concurrently and await ALL of "
        "them (a barrier). A throwing thunk resolves to `None`.\n"
        "- `phase(title)` — start a progress phase; `log(message)` — narrate progress; "
        "`args` — the tool call's `args` input, verbatim.\n\n"
        "Misused hooks (bad arguments, unknown options, unsupported schemas, tripped "
        "caps) throw errors that ALWAYS kill the script — they never dissolve into a "
        "per-item `None`."
    )
    return base + (BACKGROUND_SCRIPT if background_enabled else FOREGROUND_ONLY)


BACKGROUND_SCRIPT = (
    "\n\nSet `run_in_background: true` for a long run: the call returns a job id "
    "immediately, the run keeps orchestrating in the background, and its return value "
    "arrives with the job's completion notice (check on it with `job_output`, stop it "
    "with `job_kill`)."
)
FOREGROUND_ONLY = (
    " The run executes in the foreground: this call returns when the whole script "
    "finishes."
)


def _validate_args(args: dict) -> None:
    script = args.get("script")
    if not isinstance(script, str) or script.strip() == "":
        raise ValueError("invalid script: expected a non-empty string")
    meta = args.get("meta")
    if not isinstance(meta, dict):
        raise ValueError("invalid meta: expected an object")
    for field in ("name", "description"):
        value = meta.get(field)
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError(f"invalid meta.{field}: expected a non-empty string")


def _render_result(name: str, agents_started: int, value: Any, max_chars: int) -> str:
    """前台结果渲染（index.ts:248-255）。"""
    rendered = json.dumps(value, indent=2, ensure_ascii=False)
    if len(rendered) > max_chars:
        clipped = rendered[:max_chars]
        return (f'workflow "{name}" completed ({agents_started} agent(s)).\n'
                f"Return value:\n{clipped}\n… [truncated: "
                f"{len(rendered) - max_chars} more characters]")
    return (f'workflow "{name}" completed ({agents_started} agent(s)).\n'
            f"Return value:\n{rendered}")


def _stop_reason_error(result: dict) -> str | None:
    """结局 → 前台错误（index.ts:474-486）。"""
    stop = result.get("stopReason")
    if stop == "completed":
        return None
    if stop == "cancelled":
        error = result.get("error")
        return f"workflow run was cancelled{f' ({error})' if error else ''}"
    if stop == "error":
        return f"workflow run failed: {result.get('error') or 'unknown error'}"
    return f"workflow run ended abnormally ({stop})"


def create_workflow_tool(ctx, config: dict | None = None) -> Tool:
    """构造 `workflow` 工具（执行在 ctx.workflowEngine）。"""
    cfg = resolve_config(config)
    tool_name = cfg["toolName"]
    background_enabled = cfg["enableRunInBackground"]
    jobs = ctx.get("jobs")
    engine = ctx.get("workflowEngine")

    def agent_of(exec_):
        return getattr(exec_, "agent", None)

    def _start_run(args, exec_) -> Any:
        parent = agent_of(exec_)
        if parent is None:
            raise RuntimeError(
                "workflow tool requires a calling agent (exec.agent was undefined)")
        run = engine.start({
            "script": args["script"],
            "meta": args["meta"],
            **({} if "args" not in args else {"args": args["args"]}),
            "parent": parent,
            "signal": getattr(exec_, "signal", None),
        })
        return run

    def _recorder_start(run, exec_, records_run: bool):
        if not records_run:
            return
        session = agent_of(exec_).session
        session.append("tool-workflow/run-start", {"runId": run.id, "name": run.meta["name"]})
        return session

    def _recorder_finish(session, run, stop_reason: str):
        if session is None:
            return
        session.append("tool-workflow/run-end", {"runId": run.id, "stopReason": stop_reason})

    def _record_agents(session, run, run_id: str):
        """订阅 run 的 agent-start/agent-end 事件 → durable 记录（仅跟踪的 run）。"""
        if session is None:
            return []

        def on_agent_start(info, agent):
            data = {"runId": run_id, "seq": agent["seq"], "label": agent["label"],
                    "childId": agent["childId"]}
            if agent.get("phase"):
                data["phase"] = agent["phase"]
            session.append("tool-workflow/agent-start", data)

        def on_agent_end(info, agent):
            session.append("tool-workflow/agent-end", {
                "runId": run_id, "seq": agent["seq"], "outcome": agent["outcome"]})

        from ..core.scope import Context
        ctx.on("workflow/agent-start", lambda info, agent: on_agent_start(info, agent)
               if info.get("id") == run_id else None)
        ctx.on("workflow/agent-end", lambda info, agent: on_agent_end(info, agent)
               if info.get("id") == run_id else None)
        return []

    async def execute(args: dict, exec_) -> dict:
        _validate_args(args)
        parent = agent_of(exec_)
        run_in_background = args.get("run_in_background") is True
        if run_in_background:
            if not background_enabled:
                raise RuntimeError(
                    "run_in_background is disabled for this deployment "
                    "(enableRunInBackground: false)")
            if jobs is None:
                raise RuntimeError(
                    "background jobs unavailable: load @deepseek-ai/dsh-jobs and "
                    "@deepseek-ai/dsh-tool-jobs")
        records_run = exec_.parent is None  # 仅顶层传输执行记录 durable 事件
        run = _start_run(args, exec_)
        session = _recorder_start(run, exec_, records_run)
        _record_agents(session, run, run.id)
        if run_in_background:
            job = _start_background_job(ctx, args, parent, run, records_run, session,
                                        cfg["maxResultChars"])
            return {"kind": "background", "jobId": job["id"], "runId": run.id}
        try:
            result = await asyncio.to_thread(run.result)
        except Exception:
            _recorder_finish(session, run, "error")
            raise
        error = _stop_reason_error(result)
        _recorder_finish(session, run, result.get("stopReason"))
        if error is not None:
            raise RuntimeError(error)
        return {"kind": "foreground", "runId": run.id,
                "agentsStarted": result.get("agentsStarted", 0),
                "result": result.get("value")}

    def _start_background_job(ctx, args, parent, run, records_run, session, max_chars):
        box = JobDoneBox()

        def work(_job):
            try:
                result = run.result()
                stop = result.get("stopReason")
                if records_run:
                    session.append("tool-workflow/run-end",
                                   {"runId": run.id, "stopReason": stop})
                if stop == "completed":
                    box.settle({"status": "completed",
                                "detail": f"{result.get('agentsStarted', 0)} agent(s)",
                                "result": _render_result(
                                    args["meta"]["name"], result.get("agentsStarted", 0),
                                    result.get("value"), max_chars)})
                elif stop == "cancelled":
                    box.settle({"status": "killed"})
                else:
                    box.settle({"status": "failed",
                                "detail": result.get("error") or "unknown error"})
            except BaseException as error:  # noqa: BLE001 - 后台失败转 failed
                box.fail(error)

        worker = __import__("threading").Thread(
            target=work, name=f"workflow-{run.id}", daemon=True)
        job_id = jobs.start({
            "kind": "workflow",
            "label": args["meta"]["name"],
            "owner": parent.id,
            "run": lambda _job: {"done": box,
                                 "cancel": lambda reason=None: run.cancel(
                                     reason or "background workflow job killed")},
        })
        worker.start()
        return {"id": job_id}

    def render(args: dict, value: dict) -> list:
        if value["kind"] == "background":
            text = (f'workflow "{args["meta"]["name"]}" started in the background as '
                    f"job {value['jobId']}. Its return value arrives with the completion "
                    "notice; check on it with job_output, stop it with job_kill.")
        elif value["kind"] == "foreground":
            text = _render_result(args["meta"]["name"], value["agentsStarted"],
                                  value["result"], cfg["maxResultChars"])
        else:
            text = _render_result(args["meta"]["name"], value["agentsStarted"],
                                  value["result"], cfg["maxResultChars"])
        return [{"type": "text", "text": text}]

    def present_call(args: dict) -> dict:
        return {"card": "generic", "title": f"workflow: {args['meta']['name']}",
                "rawInput": args.get("script", "")}

    def present_result(args: dict, value: dict) -> dict:
        return {"card": "generic"}

    properties: dict = {
        "script": {"type": "string",
                   "description": "The plain-Python workflow script body (top-level "
                                  "await allowed; NO `export const meta` statement; "
                                  "end with `return <json-value>`)."},
        "meta": {"type": "object",
                 "description": "Workflow meta: `name` (string), `description` "
                                "(string), `whenToUse` (string), `phases` (array of "
                                "{title, detail?, provider?, model?})."},
        "args": {"type": "object",
                 "description": "Optional JSON input exposed to the script as the "
                                "`args` global (wrap a bare list as a field, e.g. "
                                '{"files": [...]}).'},
    }
    if background_enabled:
        properties["run_in_background"] = {
            "type": "boolean",
            "description": "Run in the background and return a job id immediately."}

    description = _description(background_enabled)
    return Tool(
        name=tool_name,
        description=description,
        parameters={"type": "object", "properties": properties,
                    "required": ["script", "meta"]},
        output={"schema": {"type": "object",
                           "properties": {"kind": {"type": "string", "const": "foreground"}}}},
        execute=execute,
        render=render,
        present_call=present_call,
        present_result=present_result,
    )


def install_tool_workflow(ctx, config: dict | None = None):
    """把 `workflow` 工具注册进 ctx.tools（要求 ctx.workflowEngine + ctx.tools 在场）。"""
    engine = ctx.get("workflowEngine")
    if engine is None:
        return None
    registry = ctx.get("tools")
    if registry is None:
        from ..core.tools import ToolRegistry
        registry = ToolRegistry(ctx)
    tool = create_workflow_tool(ctx, config)
    registry.register(tool)
    return tool