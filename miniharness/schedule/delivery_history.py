"""投递历史的写入裁剪与身份游标分页（对齐 delivery-history.ts）。

* :func:`append_delivery` 在唯一写路径上按配置窗口与条数裁剪并保留
  ``lastDelivery``；``earlierRecordsUnavailable``/``earlierRecordsPruned``
  一旦置位保持（legacy 缺位不算裁剪）。
* :func:`delivery_history_page` 身份游标 ``before``（排除该 messageId），
  追加序、反转为最新在前，limit 1-100，返回 ``nextBefore``（更早仍有记录）。
"""
from __future__ import annotations

from typing import Any

from .domain import _epoch_from_canonical

__all__ = ["append_delivery", "delivery_history_page"]

_DAY_MS = 86_400_000


def _history_of(task: dict) -> dict:
    history = task.get("deliveryHistory")
    if history is not None:
        return history
    last = task.get("lastDelivery")
    return {
        "records": [] if last is None else [last],
        "earlierRecordsUnavailable": True,
    }


def append_delivery(task: dict, receipt: dict, bounds: dict) -> dict:
    """为一次真实回执准备同一次任务写的状态与历史（上游 appendDelivery）。"""
    history = _history_of(task)
    appended = [*history["records"], {**receipt, "prompt": task["record"]["prompt"]}]
    floor = _epoch_from_canonical(receipt["deliveredAt"]) - bounds["days"] * _DAY_MS
    retained = [
        record for record in appended
        if _epoch_from_canonical(record["deliveredAt"]) >= floor
    ][-bounds["records"]:]
    return {
        "lastDelivery": receipt,
        "deliveryHistory": {
            "records": retained,
            "earlierRecordsUnavailable": (
                history["earlierRecordsUnavailable"] or len(retained) != len(appended)),
            "earlierRecordsPruned": (
                history.get("earlierRecordsPruned") is True
                or len(retained) != len(appended)),
        },
    }


def delivery_history_page(task: dict, request: Any, retention: dict) -> dict:
    """读取一个最新在前页面（追加序，独立于墙钟序；上游 deliveryHistoryPage）。"""
    history = _history_of(task)
    before = request.get("before")
    records = history["records"]
    if before is None:
        end = len(records)
    else:
        end = next((index for index, record in enumerate(records)
                    if record["messageId"] == before), -1)
    if end == -1:
        return {"id": request["id"], "code": "delivery_cursor_not_found"}
    start = max(0, end - request["limit"])
    page = []
    for record in reversed(records[start:end]):
        receipt = {key: value for key, value in record.items() if key != "prompt"}
        if "prompt" in record:
            receipt["prompt"] = record["prompt"]
        page.append(receipt)
    result = {
        "id": request["id"],
        "records": page,
        "earlierRecordsUnavailable": history["earlierRecordsUnavailable"],
        "earlierRecordsPruned": history.get("earlierRecordsPruned") is True,
        "retention": dict(retention),
    }
    oldest = page[-1] if page else None
    if oldest is not None and start > 0:
        result["nextBefore"] = oldest["messageId"]
    return result
