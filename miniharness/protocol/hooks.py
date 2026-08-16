"""第 12 章：hooks 桥 —— Claude Code 钩子翻译成拦截决策。

对应 dsh 真实源码：packages/hooks/（hook-protocol + hooks-claude-code）。

上游语义（已核实）：
  * codec（codec.ts）：exit 0 且 stdout 以 '{' 开头才尝试 JSON（其余是纯文本，
    不当作错误）；exit 2 → 阻塞（block），stderr 为 reason；其他 exit 是非
    阻塞错误。hookSpecificOutput 有 hookEventName 守卫：缺失或不同名的事件域
    字段被丢弃（顶层字段与声明的判别符仍保留）。permissionDecision
    （allow/deny/ask）覆盖顶层 decision（approve/block 仅限顶层，越界的
    {"decision":"deny"} 无效被忽略）。updatedInput 解析但不执行（日志+警告）。
  * matcher（matcher.ts）：缺失/空/'*' = match-all；claude-code 对纯
    [A-Za-z0-9_|]+ 模式用字面管道交替，其他是未锚定正则（无效正则 = 不匹配，
    配置期由 matcherDiagnostic 拒绝）；codex 恒为正则。
  * merge（merge.ts）：deny > ask > allow（block/deny → deny，approve/allow →
    allow）；第一个 continue:false 粘住（stop + 首个 stopReason）；获胜等级
    的 reason 用 '\\n\\n' 连接；additionalContext/systemMessage 按钩子序累积。
  * 决策映射（hooks-claude-code/src/index.ts）：UserPromptSubmit → pre-step：
    deny → {kind:'reject'}，否则委派（enter 时前置 additionalContext）；
    PreToolUse → pre-execute：deny → {kind:'deny', reason}、ask →
    {kind:'ask', reason?}，否则委派；PostToolUse → post-execute：deny →
    {kind:'block', feedback}，否则委派 + 折叠 context；Stop → deny 强制继续。
  * 会话事件 hook/invoked + hook/result（handlerId 配对，log-only 非 surface）。
  * config（config.ts）：settings 对象（hooks 键）或裸事件映射都接受；非
    command 类型跳过（返回 skipped 供警告）；${CLAUDE_PLUGIN_ROOT} /
    ${CLAUDE_PROJECT_DIR} 替换；UserPromptSubmit/Stop 无匹配主题（matcher 丢弃）；
    无效 matcher 抛 SyntaxError（注册前整体拒绝）。

载体简化：上游经 ctx.shell 异步执行 + signal；mini 用 subprocess 同步近似，
run_fn 可注入（测试不依赖 shell）。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from typing import Any, Callable

BLOCKING_EXIT_CODE = 2
CLAUDE_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse",
                 "PostToolUse", "Stop", "SubagentStart", "SubagentStop"]
CLAUDE_LITERAL = re.compile(r"^[A-Za-z0-9_|]+$")


# ---------- codec ----------

def parse_hook_output(exit_code: int | None, stdout: str, stderr: str,
                      expected_event_name: str | None = None) -> dict:
    """解码钩子进程输出 → 方言中立的 HookOutput（codec.ts 同构）。"""
    trimmed_err = stderr.strip()
    trimmed_out = stdout.strip()
    output: dict = {"exitCode": exit_code, "stderr": trimmed_err, "stdout": trimmed_out}
    if exit_code == BLOCKING_EXIT_CODE:
        output["decision"] = "block"
        if trimmed_err:
            output["reason"] = trimmed_err
    if exit_code == 0 and trimmed_out.startswith("{"):
        try:
            parsed = json.loads(trimmed_out)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            _apply_structured(output, parsed, expected_event_name)
    return output


def _str(obj: dict, key: str) -> str | None:
    v = obj.get(key)
    return v if isinstance(v, str) else None


def _bool(obj: dict, key: str) -> bool | None:
    v = obj.get(key)
    return v if isinstance(v, bool) else None


def _obj(value: Any) -> dict | None:
    return value if isinstance(value, dict) else None


def _top_level_decision(value: str | None) -> str | None:
    return value if value in ("approve", "block") else None


def _permission_decision(value: str | None) -> str | None:
    return value if value in ("allow", "deny", "ask") else None


def _apply_structured(output: dict, parsed: dict, expected_event_name: str | None) -> None:
    cont = _bool(parsed, "continue")
    if cont is not None:
        output["continue"] = cont
    stop_reason = _str(parsed, "stopReason")
    if stop_reason is not None:
        output["stopReason"] = stop_reason
    sys_msg = _str(parsed, "systemMessage")
    if sys_msg is not None:
        output["systemMessage"] = sys_msg
    top_decision = _top_level_decision(_str(parsed, "decision"))
    if top_decision is not None:
        output["decision"] = top_decision
    top_reason = _str(parsed, "reason")
    if top_reason is not None:
        output["reason"] = top_reason
    hso = _obj(parsed.get("hookSpecificOutput"))
    if hso is None:
        return
    event_name = _str(hso, "hookEventName")
    if event_name is not None:
        output["hookEventName"] = event_name   # 判别符始终保留（日志/诊断）
    if expected_event_name is not None and event_name != expected_event_name:
        return   # 缺失或不同名：事件域字段丢弃
    permission = _permission_decision(_str(hso, "permissionDecision"))
    if permission is not None:
        output["decision"] = permission
    permission_reason = _str(hso, "permissionDecisionReason")
    if permission_reason is not None:
        output["reason"] = permission_reason
    add_ctx = _str(hso, "additionalContext")
    if add_ctx is not None:
        output["additionalContext"] = add_ctx
    updated = _obj(hso.get("updatedInput"))
    if updated is not None:
        output["updatedInput"] = updated   # 解析但不执行（上游同样 defer）


# ---------- matcher ----------

def _is_match_all(matcher: str | None) -> bool:
    return matcher is None or matcher == "" or matcher == "*"


def _compile_regex(pattern: str) -> re.Pattern | None:
    try:
        return re.compile(pattern)
    except re.error:
        return None


def matcher_diagnostic(matcher: str | None, mode: str) -> str | None:
    """配置期校验：match-all 哨兵有效；claude-code 字面模式有效；其余必须是合法正则。"""
    if _is_match_all(matcher):
        return None
    pattern = matcher
    if mode == "claude-code" and CLAUDE_LITERAL.match(pattern):
        return None
    return None if _compile_regex(pattern) is not None else \
        f"invalid {mode} regex matcher {pattern!r}"


def matches_matcher(matcher: str | None, query: str, mode: str) -> bool:
    """matcher 是否选中 query（运行期；无效正则返回 False 不抛）。"""
    if _is_match_all(matcher):
        return True
    pattern = matcher
    if mode == "claude-code" and CLAUDE_LITERAL.match(pattern):
        return pattern.split("|").__contains__(query)
    rx = _compile_regex(pattern)
    return rx is not None and rx.search(query) is not None


# ---------- merge ----------

def _rank(decision: str | None) -> int:
    return {"deny": 3, "block": 3, "ask": 2, "approve": 1, "allow": 1}.get(decision, 0)


def _decision_for_rank(max_rank: int) -> str:
    return {3: "deny", 2: "ask", 1: "allow"}.get(max_rank, "none")


def merge_hook_outputs(outputs: list) -> dict:
    """把所有命中钩子的输出折叠成一个最严格结果（merge.ts 同构）。"""
    max_rank = 0
    reasons_by_rank: dict[int, list[str]] = {}
    stop = False
    stop_reason: str | None = None
    additional_context: list[str] = []
    system_messages: list[str] = []
    for out in outputs:
        r = _rank(out.get("decision"))
        if r > max_rank:
            max_rank = r
        if r in (2, 3) and out.get("reason"):
            reasons_by_rank.setdefault(r, []).append(out["reason"])
        if out.get("continue") is False and not stop:
            stop = True
            if out.get("stopReason"):
                stop_reason = out["stopReason"]
        if out.get("additionalContext"):
            additional_context.append(out["additionalContext"])
        if out.get("systemMessage"):
            system_messages.append(out["systemMessage"])
    reasons = reasons_by_rank.get(max_rank, [])
    merged: dict = {"decision": _decision_for_rank(max_rank), "stop": stop,
                    "additionalContext": additional_context,
                    "systemMessages": system_messages}
    if reasons:
        merged["reason"] = "\n\n".join(reasons)
    if stop_reason is not None:
        merged["stopReason"] = stop_reason
    return merged


# ---------- config ----------

def substitute_command(command: str, vars: dict | None = None) -> str:
    """${CLAUDE_PLUGIN_ROOT} / ${CLAUDE_PROJECT_DIR} 替换；未设置的 token 原样保留。"""
    out = command
    if vars:
        if vars.get("pluginRoot") is not None:
            out = out.replace("${CLAUDE_PLUGIN_ROOT}", vars["pluginRoot"])
        if vars.get("projectDir") is not None:
            out = out.replace("${CLAUDE_PROJECT_DIR}", vars["projectDir"])
    return out


def parse_claude_code_config(raw: Any, vars: dict | None = None) -> dict:
    """解析 settings 对象（hooks 键）或裸事件映射 → {config, skipped}。

    畸形条目忽略（不炸 boot）；非 command 钩子进 skipped；UserPromptSubmit/
    Stop 的 matcher 丢弃；带 matcher 的合法组里无效正则抛 SyntaxError。
    """
    config: dict[str, list] = {}
    skipped: list[dict] = []
    root = raw if isinstance(raw, dict) else None
    hooks_map = None
    if root is not None:
        inner = root.get("hooks")
        hooks_map = inner if isinstance(inner, dict) else root
    if hooks_map is None:
        return {"config": config, "skipped": skipped}
    for event in CLAUDE_EVENTS:
        raw_groups = hooks_map.get(event)
        if not isinstance(raw_groups, list):
            continue
        groups: list[dict] = []
        for raw_group in raw_groups:
            if not isinstance(raw_group, dict) or not isinstance(raw_group.get("hooks"), list):
                continue
            commands: list[dict] = []
            for raw_hook in raw_group["hooks"]:
                if not isinstance(raw_hook, dict):
                    continue
                htype = raw_hook.get("type") if isinstance(raw_hook.get("type"), str) else "command"
                if htype != "command":
                    skipped.append({"event": event, "type": htype})
                    continue
                if not isinstance(raw_hook.get("command"), str):
                    continue
                hook: dict = {"command": substitute_command(raw_hook["command"], vars)}
                if isinstance(raw_hook.get("timeout"), (int, float)):
                    hook["timeoutSec"] = raw_hook["timeout"]
                commands.append(hook)
            if not commands:
                continue
            matcher = None
            if event not in ("UserPromptSubmit", "Stop"):
                m = raw_group.get("matcher")
                matcher = m if isinstance(m, str) else None
            diagnostic = matcher_diagnostic(matcher, "claude-code")
            if diagnostic is not None:
                raise SyntaxError(f"{diagnostic} on event {event!r}")
            group: dict = {"hooks": commands}
            if matcher is not None:
                group["matcher"] = matcher
            groups.append(group)
        if groups:
            config[event] = groups
    return {"config": config, "skipped": skipped}


# ---------- runner ----------

def run_hook(command: str, timeout_sec: float | None = None) -> tuple[dict, float]:
    """执行一条钩子命令（同步近似 ctx.shell）。超时 → exitCode None。"""
    start = time.perf_counter()
    try:
        proc = subprocess.run(command, shell=True, capture_output=True, text=True,
                              timeout=timeout_sec)
        output = parse_hook_output(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        output = parse_hook_output(None, "", "hook timed out")
    return output, int((time.perf_counter() - start) * 1000)


def _next_handler_id() -> str:
    return "h_" + uuid.uuid4().hex[:8]


def _audit(session, type_: str, data: dict) -> None:
    if session is not None:
        session.append(type_, data)


# ---------- bridge ----------

class ClaudeCodeBridge:
    """CC hooks.json → 拦截决策（dialect 'claude-code'）。

    run_fn 可注入（测试不依赖 shell）；默认 subprocess 执行。
    """

    def __init__(self, raw_config: Any, vars: dict | None = None):
        self.parsed = parse_claude_code_config(raw_config, vars)
        self.skipped = self.parsed["skipped"]

    def pre_step(self, prompt_text: str, session=None, run_fn: Callable | None = None) -> dict | None:
        """UserPromptSubmit → pre-step 决策：deny → {'kind':'reject'}；否则 None（委派）。"""
        merged = self._run_point("UserPromptSubmit", "", {"prompt": prompt_text},
                                 session, run_fn)
        if merged["decision"] == "deny":
            return {"kind": "reject"}
        return None

    def pre_tool(self, tool_name: str, args: dict | None = None,
                 session=None, run_fn: Callable | None = None) -> dict | None:
        """PreToolUse → pre-execute 决策：deny / ask；否则 None（委派）。"""
        merged = self._run_point("PreToolUse", tool_name,
                                 {"toolName": tool_name, "args": args or {}},
                                 session, run_fn)
        decision = merged["decision"]
        if decision == "deny":
            return {"kind": "deny", "reason": merged.get("reason") or "blocked by PreToolUse hook"}
        if decision == "ask":
            result: dict = {"kind": "ask"}
            if merged.get("reason"):
                result["reason"] = merged["reason"]
            return result
        return None

    def post_tool(self, tool_name: str, result: Any = None,
                  session=None, run_fn: Callable | None = None) -> dict | None:
        """PostToolUse → post-execute 决策：deny → {'kind':'block', feedback}。"""
        merged = self._run_point("PostToolUse", tool_name,
                                 {"toolName": tool_name, "result": result},
                                 session, run_fn)
        if merged["decision"] == "deny":
            return {"kind": "block",
                    "feedback": merged.get("reason") or "blocked by PostToolUse hook"}
        return None

    def stop(self, session=None, run_fn: Callable | None = None) -> dict | None:
        """Stop → 阻塞钩子强制继续（continue:true + reason）。"""
        merged = self._run_point("Stop", "", {}, session, run_fn)
        if merged["decision"] == "deny":
            return {"continue": True,
                    "reason": merged.get("stopReason") or merged.get("reason")
                    or "continue: blocked by Stop hook"}
        return None

    # ---------- 内部 ----------

    def _run_point(self, point: str, match_query: str, payload: dict,
                   session, run_fn: Callable | None) -> dict:
        turn = self._last_turn(session)
        outputs: list[dict] = []
        for group in self.parsed["config"].get(point, []):
            if not matches_matcher(group.get("matcher"), match_query, "claude-code"):
                continue
            for hook in group["hooks"]:
                handler_id = _next_handler_id()
                _audit(session, "hook/invoked", {
                    "turn": turn, "point": point, "dialect": "claude-code",
                    "handlerId": handler_id,
                    **({"matcher": group["matcher"]} if group.get("matcher") else {}),
                })
                output, duration = (run_fn or _run_default)(hook, payload)
                decision = "stop" if output.get("continue") is False else \
                    output.get("decision") or "pass"
                _audit(session, "hook/result", {
                    "turn": turn, "point": point, "handlerId": handler_id,
                    "decision": decision, "durationMs": duration,
                    **({"exitCode": output["exitCode"]} if output.get("exitCode") is not None else {}),
                    **({"stderrSummary": output["stderr"][:200]} if output.get("stderr") else {}),
                })
                outputs.append(output)
        return merge_hook_outputs(outputs)

    def _last_turn(self, session) -> int:
        if session is None:
            return 0
        for event in reversed(session.events):
            if event["type"] == "turn/start":
                return event["data"].get("turn", 0)
        return 0


def _run_default(hook: dict, payload: dict) -> tuple[dict, float]:
    return run_hook(hook["command"], hook.get("timeoutSec"))