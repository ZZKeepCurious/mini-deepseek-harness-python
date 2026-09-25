"""客户端安全的作业词汇：JobView / JobChunk / JobChannel（对齐 jobs/src/view.ts）。

JobView 是一条作业的只读投影（每次调用新对象，绝不给活注册表状态），模型工具、
浏览器名册、观察流共享同一形状：
  * output.total —— 下一块 chunk 的绝对偏移（未写过为 0）
  * output.earliest —— 最老的保留字节；> 0 恰好表示 retention 丢过头
  * output.spillPaths —— pull 源当前持有的落盘文件，按源序去重；chunk 全被驱逐
    后仍保留，故低于 earliest 的读者仍能报出字节去向

`job` 家族并入 Workspace 归档准入的 SessionActivityKindMap（对齐上游 view.ts 的
declaration merging）；mini 在 `workspace.SessionActivityKindMap` 显式登记该键。

载体说明（登录 verified-diffs）：上游本模块是纯类型叶；mini 把投影构造函数一并
放此，供注册表与工具共用同一 JobView 形状。
"""
from __future__ import annotations

__all__ = [
    "JOB_CHANNELS",
    "TERMINAL_STATUSES",
    "build_view",
    "chunk_from_ring",
]

#: 输出块流标签（对齐 view.ts JobChannel）；未识别标签按缺省处理。
JOB_CHANNELS = frozenset({"stdout", "stderr", "log"})

#: 终态集合（本地判读用；与 types.TERMINAL_STATUSES 同语义）。
TERMINAL_STATUSES = frozenset({"completed", "killed", "failed"})


def _optional(mapping: dict, key: str, value) -> None:
    """仅当 value 非 None 时写入键（对齐上游可选字段的省略语义）。"""
    if value is not None:
        mapping[key] = value


def build_view(*, id: str, kind: str, label: str, owner, output_limit_bytes,
               status: str, progress, detail, started_at: int, finished_at,
               total: int, earliest: int, spill_paths: list) -> dict:
    """构造一条 JobView 投影（可选字段按上游省略规则写入）。

    `owner` 是 SessionId（unowned 为 None）；`progress` 为 live 进度行；`detail`
    为终态原因；`finished_at` 仅在结算后存在。
    """
    view: dict = {
        "id": id,
        "kind": kind,
        "label": label,
        "status": status,
        "startedAt": started_at,
        "output": {
            "total": total,
            "earliest": earliest,
            **({"spillPaths": list(spill_paths)} if spill_paths else {}),
        },
    }
    _optional(view, "owner", owner)
    _optional(view, "outputLimitBytes", output_limit_bytes)
    _optional(view, "progress", progress)
    _optional(view, "detail", detail)
    _optional(view, "finishedAt", finished_at)
    return view


def chunk_from_ring(chunk: dict) -> dict:
    """内部 ring 条目 → 对外 JobChunk（保留 at/text/channel/gapBefore）。"""
    out: dict = {"at": chunk["at"], "text": chunk["text"]}
    if chunk.get("channel") is not None:
        out["channel"] = chunk["channel"]
    if chunk.get("gapBefore"):
        out["gapBefore"] = True
    return out
