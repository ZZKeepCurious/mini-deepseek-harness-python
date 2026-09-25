"""bash 工具的模型可见渲染（对齐 packages/shell/{shell,tool-bash}/src/render.ts）。

* `parse_exit_status`：从渲染文本尾部回收 `[exit code: N]` / `[killed by signal: X]`。
* `render_result`：已结算前台结果（stdout → 一个 `[stderr]` 段 → 超时/stopped/
  信号/退出标记）。
* `render_promoted`：前台等待到期、命令提升为后台作业时的模型文本。
* `render_job_read`：前台交接到作业时嵌入的一次消费读（丢失 notice + 沙箱 notice）。

纯函数、无状态；沙箱 denial / escalation 标记文本与 fs 域同款（上游同属
`dsh-sandbox`），mini 在此就地定义以避免 shell→fs 的跨域依赖。
"""
from __future__ import annotations

import re

__all__ = [
    "escalation_hint_marker",
    "parse_exit_status",
    "render_job_read",
    "render_promoted",
    "render_result",
    "sandbox_denial_marker",
]

_SIGNAL_RE = re.compile(r"\n\[killed by signal: ([^\]\n]+)\]$")
_EXIT_RE = re.compile(r"\n\[exit code: (\d+)\]$")


def sandbox_denial_marker(mode: str) -> str:
    """模型面 denial 标记（对齐 dsh-sandbox escalation.ts:71-73）。"""
    return f"[sandbox: file access denied under {mode} mode]"


def escalation_hint_marker(subject: str) -> str:
    """同一回合升级提示（escalation.ts:84-88）。"""
    return (f"[sandbox: escalation available — retry this exact {subject} once with "
            "sandbox_permissions (the narrowest wider mode that suffices) + justification; "
            "the approval prompt asks the user]")


def parse_exit_status(text: str) -> dict:
    """拆分渲染文本为正文 + 结构化退出状态（shell/render.ts:37-43）。"""
    signal = _SIGNAL_RE.search(text)
    if signal is not None:
        return {"body": text[:signal.start()], "signal": signal.group(1)}
    exit_match = _EXIT_RE.search(text)
    if exit_match is not None:
        return {"body": text[:exit_match.start()], "exitCode": int(exit_match.group(1))}
    return {"body": text, "exitCode": 0}


def _stream_text(output: dict) -> str:
    """给截断流补上截断 notice（render.ts:12-15）。"""
    if not output.get("truncated"):
        return output["text"]
    return f"{output['text']}\n[output truncated; full output: {output.get('spillPath') or '(unavailable)'}]"


def render_result(result: dict, escalation_modes=()) -> str:
    """一次已结算前台运行的模型文本（render.ts:28-66）。"""
    out = _stream_text(result["stdout"])
    err = _stream_text(result["stderr"])
    body = out
    if err:
        if body and not body.endswith("\n"):
            body += "\n"
        body += f"[stderr]\n{err}"
    if not body:
        body = "(no output)"

    markers: list[str] = []
    sandbox = result.get("sandbox")
    if sandbox is not None and sandbox.get("denied"):
        markers.append(sandbox_denial_marker(sandbox["mode"]))
        if escalation_modes:
            markers.append(escalation_hint_marker("command"))
    if result.get("timedOut"):
        markers.append(f"[timed out after {result['timeoutMs']}ms]")
    if result.get("stopped") is not None:
        markers.append(f"[stopped: {result['stopped']}]")
    if result.get("signal") is not None:
        markers.append(f"[killed by signal: {result['signal']}]")
    elif result.get("exitCode") != 0:
        markers.append(f"[exit code: {result['exitCode']}]")
    if not markers:
        return body
    if not body.endswith("\n"):
        body += "\n"
    return body + "\n".join(markers)


def render_promoted(promoted: dict) -> str:
    """前台等待到期、命令已提升后台的模型文本（render.ts:77-84）。"""
    output = promoted["output"]
    body = output if not output or output.endswith("\n") else f"{output}\n"
    return (f"{body}[still running after {promoted['timeoutMs']}ms; "
            f"moved to background job {promoted['jobId']}]\n"
            "The command keeps running in the background. You will be notified when it "
            "finishes; read newer output with job_output, stop it with job_kill.")


def render_job_read(delta: str, lossy: bool, spill_paths, sandbox=None,
                    escalation_modes=()) -> str:
    """前台交接时嵌入的一次消费读（render.ts:99-120）。"""
    notices: list[str] = []
    if lossy:
        where = ", ".join(spill_paths) if spill_paths else "(unavailable)"
        notices.append(f"[some output was dropped from memory; full output: {where}]")
    if sandbox is not None and sandbox.get("runnerFailed"):
        notices.append(
            f"[sandbox: the sandbox runner itself failed under {sandbox['mode']} mode — "
            "the command did not run; this is a sandbox problem, not a command failure]")
    elif sandbox is not None and sandbox.get("denied"):
        notices.append(sandbox_denial_marker(sandbox["mode"]))
        if escalation_modes:
            notices.append(escalation_hint_marker("command"))
    if not notices:
        return delta
    separator = "\n" if delta and not delta.endswith("\n") else ""
    return f"{delta}{separator}{chr(10).join(notices)}"
