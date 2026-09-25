"""注册表读取的模型可见渲染（对齐 tool-jobs/src/render.ts）。

消费读的 delta 按 shell 工具一贯的形态呈现（stdout，随后一个标记的 stderr 段），
状态行与工具 schema 暴露的公开作业投影也在此。
"""
from __future__ import annotations

__all__ = ["job_detail", "public_job", "render_model_delta", "status_line"]


def job_detail(job: dict):
    """状态旁的一行限定词：运行中是 live 进度，结算后是终态原因；都无则 None。"""
    return job.get("progress") if job.get("progress") is not None else job.get("detail")


def public_job(job: dict) -> dict:
    """去掉 owner/偏移/上限，得到模型安全的投影。"""
    out: dict = {
        "id": job["id"],
        "kind": job["kind"],
        "label": job["label"],
        "status": job["status"],
        "startedAt": job["startedAt"],
    }
    detail = job_detail(job)
    if detail is not None:
        out["detail"] = detail
    if job.get("finishedAt") is not None:
        out["finishedAt"] = job["finishedAt"]
    return out


def status_line(snapshot: dict) -> str:
    """带可选 detail 的 `[status: ...]` 行。"""
    detail = snapshot.get("detail")
    return (f"[status: {snapshot['status']}, {detail}]" if detail is not None
            else f"[status: {snapshot['status']}]")


def render_model_delta(chunks: list, lossy: bool, spill_paths: list) -> str:
    """把一次消费读渲染给模型：stdout 与无标签块，随后全部 stderr 合成一个
    `[stderr]` 段。`log` 块只给观察者，永不进模型。丢失字节（游标落后于保留
    窗口，或模型可见块带 producer 侧 gap）在读尾追加 dropped-output notice，
    并列出作业源当前持有的落盘文件。
    """
    visible = [chunk for chunk in chunks if chunk.get("channel") != "log"]
    out = "".join(chunk["text"] for chunk in visible if chunk.get("channel") != "stderr")
    err = "".join(chunk["text"] for chunk in visible if chunk.get("channel") == "stderr")
    separator = "\n" if out and not out.endswith("\n") else ""
    body = out + (f"{separator}[stderr]\n{err}" if err else "")
    if not lossy and not any(chunk.get("gapBefore") is True for chunk in visible):
        return body
    where = ", ".join(spill_paths) if spill_paths else "(unavailable)"
    notice = f"[some output was dropped from memory; full output: {where}]"
    return f"{body}{'' if not body or body.endswith(chr(10)) else chr(10)}{notice}"
