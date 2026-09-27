"""pwsh 工具渲染（对齐 tool-pwsh/src/render.ts：renderPwshResult/Promoted/JobRead）。"""
from __future__ import annotations

__all__ = ["render_job_read", "render_promoted", "render_pwsh_result"]


def _stream_text(stream: dict) -> str:
    """stream 视图 → 文本（truncated 附 spillPath 提示）。"""
    text = stream.get("text", "")
    if stream.get("truncated"):
        text += "\n[output truncated; full output: " \
                f"{stream.get('spillPath') or '(unavailable)'}]"
    return text


def render_pwsh_result(result: dict, escalation_modes: tuple = ()) -> str:
    """renderPwshResult（render.ts:24-51）：stdout + stderr 段 + 结果标记。"""
    body = _stream_text(result.get("stdout") or {})
    stderr = _stream_text(result.get("stderr") or {})
    if stderr:
        if body and not body.endswith("\n"):
            body += "\n"
        body += f"[stderr]\n{stderr}"
    if not body:
        body = "(no output)"
    markers: list[str] = []
    sandbox = result.get("sandbox")
    if sandbox is not None and sandbox.get("denied"):
        markers.append(f"[sandbox: file access denied under {sandbox['mode']} mode]")
        if escalation_modes:
            markers.append(
                "[sandbox: escalation available — retry this exact command once with "
                "sandbox_permissions (the narrowest wider mode that suffices) + "
                "justification; the approval prompt asks the user]")
    if result.get("timedOut"):
        markers.append(f"[timed out after {result['timeoutMs']}ms]")
    if result.get("stopped"):
        markers.append(f"[stopped: {result['stopped']}]")
    signal = result.get("signal")
    exit_code = result.get("exitCode")
    if signal is not None:
        markers.append(f"[killed by signal: {signal}]")
    elif exit_code is not None and exit_code != 0:
        markers.append(f"[exit code: {exit_code}]")
    if markers:
        if body and not body.endswith("\n"):
            body += "\n"
        body += "\n".join(markers)
    return body


def render_promoted(promoted: dict) -> str:
    """renderPwshPromoted（render.ts:53-64）：提升为后台作业的提示。"""
    body = promoted.get("output", "")
    if body and not body.endswith("\n"):
        body += "\n"
    body += f"[still running after {promoted['timeoutMs']}ms; moved to background job ID]\n"
    body += ("The command keeps running in the background. You will be notified when "
             "it finishes; read newer output with job_output, stop it with job_kill.")
    return body


def render_job_read(delta: str, lossy: bool, spill_paths: list,
                    sandbox: dict | None = None, escalation_modes: tuple = ()) -> str:
    """renderPwshJobRead（render.ts:66-82）：后台读投影。"""
    notices: list[str] = []
    if lossy:
        paths = ", ".join(spill_paths) if spill_paths else "(unavailable)"
        notices.append(f"[some output was dropped from memory; full output: {paths}]")
    if sandbox is not None:
        if sandbox.get("runnerFailed"):
            notices.append(
                f"[sandbox: the sandbox runner itself failed under {sandbox['mode']} "
                "mode — the command did not run; this is a sandbox problem, not a "
                "command failure]")
        elif sandbox.get("denied"):
            notices.append(
                f"[sandbox: file access denied under {sandbox['mode']} mode]")
            if escalation_modes:
                notices.append(
                    "[sandbox: escalation available — retry this exact command once "
                    "with sandbox_permissions (the narrowest wider mode that suffices) "
                    "+ justification; the approval prompt asks the user]")
    if notices:
        if delta and not delta.endswith("\n"):
            delta += "\n"
        delta += "\n".join(notices)
    return delta