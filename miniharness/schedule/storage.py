"""Host 全局 Schedule 任务存储域（对齐 packages/schedule/schedule/src/storage.ts）。

权威存储 = `storage-domain` 表 `schedule.tasks`（key = ScheduleId，record =
ScheduleTask）。记录 schema 与上游 zod `scheduleTaskSchema` 等价：严格键集、
record 经 decodeScheduleRecord、status 缺省 active、lastDelivery/deliveryHistory
可选，并 refine「lastDelivery 必须等于历史最后一条回执」。坏记录使开域失败
（default invalid_records=None → 上游 malformed-unit.spec 语义）。
"""
from __future__ import annotations

from typing import Any

from ..storage import DomainSpec, define_domain, domain_table
from . import domain as _domain

__all__ = ["SCHEDULE_DOMAIN_VERSION", "schedule_domain", "schedule_task_schema"]

#: 当前 domain 格式版本（上游 storage.ts version 1）。
SCHEDULE_DOMAIN_VERSION = 1

_TASK_KEYS = ("sessionId", "record", "status", "lastDelivery", "deliveryHistory")
_RECEIPT_KEYS = ("scheduledAt", "deliveredAt", "messageId")
_HISTORY_KEYS = ("records", "earlierRecordsUnavailable", "earlierRecordsPruned")


def _valid_instant(value: Any) -> bool:
    if not isinstance(value, str) or value.startswith("0000-"):
        return False
    if _domain._UTC_INSTANT.match(value) is None:
        return False
    return _domain._format_epoch_ms(_domain._epoch_from_canonical(value)) == value


def _valid_message_id(value: Any) -> bool:
    return isinstance(value, str) and value != "" and value.strip() == value


def _valid_receipt(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if set(value) - set(_RECEIPT_KEYS):
        return False
    if not all(key in value for key in _RECEIPT_KEYS):
        return False
    return (_valid_instant(value["scheduledAt"]) and _valid_instant(value["deliveredAt"])
            and _valid_message_id(value["messageId"]))


def _valid_delivery_record(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if set(value) - set(_RECEIPT_KEYS) - {"prompt"}:
        return False
    if not _valid_receipt(value):
        return False
    if "prompt" in value and not isinstance(value["prompt"], str):
        return False
    return True


def _valid_history(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if set(value) - set(_HISTORY_KEYS):
        return False
    if "records" not in value or "earlierRecordsUnavailable" not in value:
        return False
    if not isinstance(value["records"], list):
        return False
    if not isinstance(value["earlierRecordsUnavailable"], bool):
        return False
    if "earlierRecordsPruned" in value and not isinstance(value["earlierRecordsPruned"], bool):
        return False
    for record in value["records"]:
        if not _valid_delivery_record(record):
            return False
    identities = [record["messageId"] for record in value["records"]]
    return len(set(identities)) == len(identities)


def _latest_matches_last(task: dict) -> bool:
    history = task.get("deliveryHistory")
    if history is None:
        return True
    latest = history["records"][-1] if history["records"] else None
    last = task.get("lastDelivery")
    if latest is None:
        return last is None
    if last is None:
        return False
    return (latest["scheduledAt"] == last["scheduledAt"]
            and latest["deliveredAt"] == last["deliveredAt"]
            and latest["messageId"] == last["messageId"])


def _validate_task(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("schedule task must be an object")
    if set(value) - set(_TASK_KEYS):
        raise ValueError(
            "schedule task must contain only sessionId, record, status, lastDelivery, "
            "and deliveryHistory")
    if not all(key in value for key in ("sessionId", "record")):
        raise ValueError("schedule task requires sessionId and record")
    session_id = value["sessionId"]
    if not isinstance(session_id, str) or session_id == "":
        raise ValueError("schedule task sessionId must be a non-empty string")
    record = _domain.decode_schedule_record(value["record"])
    status = value.get("status", "active")
    if status not in ("active", "inactive"):
        raise ValueError("schedule task status must be active or inactive")
    normalized: dict = {"sessionId": session_id, "record": record, "status": status}
    if "lastDelivery" in value:
        if not _valid_receipt(value["lastDelivery"]):
            raise ValueError("schedule task lastDelivery must be a delivery receipt")
        normalized["lastDelivery"] = value["lastDelivery"]
    if "deliveryHistory" in value:
        if not _valid_history(value["deliveryHistory"]):
            raise ValueError("schedule task deliveryHistory must be a valid history")
        normalized["deliveryHistory"] = value["deliveryHistory"]
    if not _latest_matches_last(normalized):
        raise ValueError("Last delivery must match the latest saved delivery receipt")
    return normalized


class _StandardView:
    def __init__(self, validate: Any) -> None:
        self._validate = validate

    def validate(self, value: Any) -> dict:
        try:
            return {"value": self._validate(value)}
        except Exception as error:  # noqa: BLE001 - durable boundary normalizes
            return {"issues": [{"message": str(error)}]}


class _ValueSchema:
    """把一个纯校验函数包成 storage-domain 可消费的 schema（``~standard`` 协议）。"""

    def __init__(self, validate: Any) -> None:
        self._view = _StandardView(validate)

    def __getitem__(self, key: str) -> Any:
        if key != "~standard":
            raise KeyError(key)
        return self._view


schedule_task_schema = _ValueSchema(_validate_task)

#: 声明 `schedule` domain（单布局，version 1，坏记录拒绝开域）。
schedule_domain = define_domain(DomainSpec(
    name="schedule",
    version=SCHEDULE_DOMAIN_VERSION,
    tables={"tasks": domain_table(schedule_task_schema)},
))
