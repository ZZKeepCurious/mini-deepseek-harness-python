"""bash 执行句柄到通用作业词汇的适配（对齐 tool-bash/src/background.ts）。

* `process_outcome`：已结算进程 → `JobOutcome`（killed/completed + detail；沙箱
  runner 失败 / denial 追加进 detail）。
* `process_sources`：句柄的非消耗观测流作为注册表 pull 源（惰性绑定）。
* `ring_delta`：一次消费注册表读的 ring 块按 shell 惯例合成 stdout + `[stderr]` 段。

载体说明：mini 的 `ShellExecution` 在 `execute` 返回时即已建好观测读器，故源惰性
绑定只针对「starter 尚未 spawn」的窗口；无 spillPath。
"""
from __future__ import annotations

from .render import escalation_hint_marker, sandbox_denial_marker

__all__ = ["process_outcome", "process_sources", "ring_delta", "sandbox_notes"]

#: runner 自身失败（命令没跑）的模型面说明（background.ts:21-31）。
_RUNNER_FAILED_NOTE = (
    "[sandbox: the sandbox runner itself failed under {mode} mode — the command did not "
    "run; this is a sandbox problem, not a command failure]")


def sandbox_notes(sandbox: dict | None, escalation_modes=()) -> list[str]:
    """终态 detail 值得追加的沙箱事实，旧的在前（background.ts:21-31）。"""
    if sandbox is None:
        return []
    if sandbox.get("runnerFailed"):
        return [_RUNNER_FAILED_NOTE.format(mode=sandbox["mode"])]
    if sandbox.get("denied"):
        notes = [sandbox_denial_marker(sandbox["mode"])]
        if escalation_modes:
            notes.append(escalation_hint_marker("command"))
        return notes
    return []


def process_outcome(proc, escalation_modes=()) -> dict:
    """已结算进程 → 作业终态（background.ts:44-55）。"""
    if proc.status == "killed":
        detail = (f"signal: {proc.signal}" if proc.signal is not None
                  else "killed before exit")
        base = {"status": "killed", "detail": detail}
    else:
        base = {"status": "completed",
                "detail": f"exit code: {proc.exitCode if proc.exitCode is not None else 0}"}
    notes = sandbox_notes(proc.sandbox, escalation_modes)
    if not notes:
        return base
    return {**base, "detail": f"{base['detail']}; {' '.join(notes)}"}


def process_sources(proc) -> list[dict]:
    """句柄的非消耗观测流作为注册表 pull 源（background.ts:67-76）。"""
    def source(channel: str) -> dict:
        def read(from_byte: int) -> dict:
            live = proc()
            if live is None:
                return {"text": "", "nextOffset": from_byte, "lossy": False}
            return live.observed[channel].read_from(from_byte)
        return {"channel": channel, "read": read}
    return [source("stdout"), source("stderr")]


def ring_delta(chunks: list) -> str:
    """一次消费读的 ring 块 → stdout 文本 + 一个 `[stderr]` 段（background.ts:86-91）。"""
    out = "".join(chunk["text"] for chunk in chunks if chunk.get("channel") != "stderr")
    err = "".join(chunk["text"] for chunk in chunks if chunk.get("channel") == "stderr")
    separator = "\n" if out and not out.endswith("\n") else ""
    return out + (f"{separator}[stderr]\n{err}" if err else "")
