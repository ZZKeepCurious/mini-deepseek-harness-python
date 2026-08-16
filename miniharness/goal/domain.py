"""目标域：`goal/change` 事件词汇、严格解码与重放 fold。

上游对照：packages/goal/goal/src/fold.ts（纯重放 fold + 严格解码）+ domain.ts
（GoalChangeMeta / GoalMessageSource）+ runtime.ts（GOAL_CHANGE_VERSION、GoalId）。

契约（与上游一致）：
  * durable 事件只有 `goal/change`（全快照或 clear 墓碑，version 1）；round 是
    消息 source 字段（{kind:'goal', goalId, revision, round}）与快照计数，
    不存在 goal/claim / goal/round 事件。
  * 严格重放：坏 goal/change fail loud（操作/字段/版本/计数/时间戳/阶段迁移校验）；
    goal 来源 user 消息必须是当前 active 目标的下一个轮次
    （round == roundsStarted + 1 且 <= maxGoalRounds），否则 replay 抛错。
"""
from __future__ import annotations

import re

from ..core.session.json import thaw

__all__ = [
    "GOAL_CHANGE_VERSION",
    "GoalError",
    "apply_goal_change",
    "apply_goal_event",
    "decode_goal_change",
    "fold_goal",
    "goal_change_ref",
]

#: goal/change 载荷版本（上游 GOAL_CHANGE_VERSION=1，未支持版本 fail loud）。
GOAL_CHANGE_VERSION = 1
#: 快照操作（除 clear 外；上游 SNAPSHOT_OPERATIONS）。
SNAPSHOT_OPERATIONS = frozenset({"create", "edit", "pause", "resume", "complete", "block"})
#: 目标相位全集（上游 PHASES）。
PHASES = frozenset({"active", "paused", "blocked", "complete"})


class GoalError(ValueError):
    """目标域错误（上游 GoalError extends HarnessError）：带稳定 code。"""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def _positive_integer(value, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"goal change {field} must be a positive safe integer")
    return value


def _non_negative_integer(value, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"goal change {field} must be a non-negative safe integer")
    return value


def _exact_keys(value: dict, keys: list) -> bool:
    return sorted(value.keys()) == sorted(keys)


def decode_block_reason(value) -> dict:
    """解码一个 canonical blocker 解释（上游 decodeBlockReason）。"""
    if not isinstance(value, dict) or sorted(value.keys()) != ["code", "message"]:
        raise ValueError("goal change goal.blockedReason must have exactly code and message fields")
    code = value.get("code")
    if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", code):
        raise ValueError("goal change goal.blockedReason.code must be lower-kebab-case")
    message = value.get("message")
    if not isinstance(message, str) or message.strip() == "" or message != message.strip():
        raise ValueError("goal change goal.blockedReason.message must be non-empty and normalized")
    return {"code": code, "message": message}


def decode_snapshot(value) -> dict:
    """解码并校验一个全快照（上游 decodeSnapshot）。"""
    if not isinstance(value, dict):
        raise ValueError("goal change goal must be a record")
    gid = value.get("id")
    if not isinstance(gid, str) or gid == "":
        raise ValueError("goal change goal.id must be a non-empty string")
    objective = value.get("objective")
    if not isinstance(objective, str) or objective.strip() == "" or objective != objective.strip():
        raise ValueError("goal change goal.objective must be non-empty and normalized")
    phase = value.get("phase")
    if not isinstance(phase, str) or phase not in PHASES:
        raise ValueError("goal change goal.phase is invalid")
    expected = (["blockedReason", "id", "maxGoalRounds", "objective", "phase", "revision"]
                if phase == "blocked"
                else ["id", "maxGoalRounds", "objective", "phase", "revision"])
    if not _exact_keys(value, expected):
        raise ValueError(f"goal change goal for phase {phase} must have exactly {','.join(expected)} fields")
    goal = {
        "id": gid,
        "revision": _positive_integer(value.get("revision"), "goal.revision"),
        "objective": objective,
        "phase": phase,
        "maxGoalRounds": _positive_integer(value.get("maxGoalRounds"), "goal.maxGoalRounds"),
    }
    if phase == "blocked":
        goal["blockedReason"] = decode_block_reason(value.get("blockedReason"))
    return goal


def decode_ref(value) -> dict:
    """解码一个 clear 墓碑 ref（上游 decodeRef）。"""
    if not isinstance(value, dict) or sorted(value.keys()) != ["id", "revision"]:
        raise ValueError("goal clear tombstone must have exactly id and revision fields")
    gid = value.get("id")
    if not isinstance(gid, str) or gid == "":
        raise ValueError("goal clear tombstone id must be a non-empty string")
    return {"id": gid, "revision": _positive_integer(value.get("revision"), "cleared.revision")}


def decode_goal_change(value) -> dict | None:
    """解码自称 goal change 的值；无关值返回 None，坏 change replay fail loud。

    上游 decodeGoalChange（fold.ts:134-172）同语义：version 不符 / 操作非法 /
    字段集不符 → 抛错。先解冻（日志事件是冻结结构，上游为 frozen JSON）。
    """
    value = thaw(value)
    if not isinstance(value, dict) or value.get("kind") != "goal/change":
        return None
    if value.get("version") != GOAL_CHANGE_VERSION:
        raise ValueError(f"unsupported goal change version {value.get('version')!r}")
    if value.get("operation") == "clear":
        allowed = ["cleared", "clearedAt", "kind", "operation", "version"]
        if not _exact_keys(value, allowed):
            raise ValueError(f"goal clear change must have exactly {','.join(sorted(allowed))} fields")
        return {
            "kind": "goal/change",
            "version": GOAL_CHANGE_VERSION,
            "operation": "clear",
            "cleared": decode_ref(value.get("cleared")),
            "clearedAt": _non_negative_integer(value.get("clearedAt"), "clearedAt"),
        }
    operation = value.get("operation")
    if not isinstance(operation, str) or operation not in SNAPSHOT_OPERATIONS:
        raise ValueError("goal change operation is invalid")
    allowed = ["createdAt", "goal", "kind", "operation", "roundsStarted", "updatedAt", "version"]
    if not _exact_keys(value, allowed):
        raise ValueError(f"goal snapshot change must have exactly {','.join(sorted(allowed))} fields")
    created_at = _non_negative_integer(value.get("createdAt"), "createdAt")
    updated_at = _non_negative_integer(value.get("updatedAt"), "updatedAt")
    if updated_at < created_at:
        raise ValueError("goal change updatedAt cannot precede createdAt")
    return {
        "kind": "goal/change",
        "version": GOAL_CHANGE_VERSION,
        "operation": operation,
        "goal": decode_snapshot(value.get("goal")),
        "roundsStarted": _non_negative_integer(value.get("roundsStarted"), "roundsStarted"),
        "createdAt": created_at,
        "updatedAt": updated_at,
    }


def goal_source(source: dict | None) -> dict | None:
    """把模型消息来源收窄为合法 goal 来源；非法 source fail loud。

    上游 goalSource（fold.ts:175-183）：kind=='goal' 且 goalId 非空、
    revision/round 为正整数。先解冻（日志内 source 是冻结结构）。
    """
    source = thaw(source)
    if not isinstance(source, dict) or source.get("kind") != "goal":
        return None
    gid = source.get("goalId")
    if not isinstance(gid, str) or gid == "":
        raise ValueError("goal message source is invalid")
    if not isinstance(source.get("revision"), int) or isinstance(source.get("revision"), bool) \
            or source.get("revision") < 1:
        raise ValueError("goal message source is invalid")
    if not isinstance(source.get("round"), int) or isinstance(source.get("round"), bool) \
            or source.get("round") < 1:
        raise ValueError("goal message source is invalid")
    return source


def _require_same_definition(current: dict, next_: dict, operation: str) -> None:
    """edit 之外的操作不得改 objective / maxGoalRounds（上游 requireSameDefinition）。"""
    if next_.get("objective") != current.get("objective") \
            or next_.get("maxGoalRounds") != current.get("maxGoalRounds"):
        raise ValueError(f"goal {operation} cannot change objective or maxGoalRounds")


def _require_next_revision(current: dict, next_: dict, operation: str) -> None:
    """任何非 create 操作必须把当前目标精确推进一个 revision（上游 requireNextRevision）。"""
    if next_.get("id") != current.get("id") or next_.get("revision") != current.get("revision") + 1:
        raise ValueError(f"goal {operation} must advance the current goal by one revision")


def _validate_snapshot_transition(state: dict, change: dict, current: dict) -> None:
    """校验一次非 create 快照操作与前序投影（上游 validateSnapshotTransition）。"""
    next_ = change["goal"]
    _require_next_revision(current, next_, change["operation"])
    if state["updatedAt"] is None:
        raise ValueError("current goal fold lacks updatedAt")
    if change["createdAt"] != state["createdAt"] \
            or change["updatedAt"] < state["updatedAt"] \
            or change["roundsStarted"] != state["roundsStarted"]:
        raise ValueError(
            f"goal {change['operation']} does not preserve the current counters and timestamps")
    op = change["operation"]
    if op == "edit":
        if next_["phase"] != current["phase"] \
                or next_.get("blockedReason") != current.get("blockedReason"):
            raise ValueError("goal edit cannot change phase or blocked reason")
    elif op == "pause":
        _require_same_definition(current, next_, op)
        if current["phase"] != "active" or next_["phase"] != "paused":
            raise ValueError("goal pause has an invalid phase transition")
    elif op == "resume":
        _require_same_definition(current, next_, op)
        if current["phase"] not in ("active", "paused", "blocked") or next_["phase"] != "active" \
                or state["roundsStarted"] >= next_["maxGoalRounds"]:
            raise ValueError("goal resume has an invalid phase transition or exhausted round budget")
    elif op == "complete":
        _require_same_definition(current, next_, op)
        if current["phase"] == "complete" or next_["phase"] != "complete":
            raise ValueError("goal complete has an invalid phase transition")
    elif op == "block":
        _require_same_definition(current, next_, op)
        if current["phase"] != "active" or next_["phase"] != "blocked":
            raise ValueError("goal block has an invalid phase transition")
    else:
        raise ValueError("unknown goal snapshot operation")


def goal_change_ref(change: dict) -> dict:
    """返回快照或墓碑携带的 revision 身份（上游 goalChangeRef）。"""
    if change["operation"] == "clear":
        return {"id": change["cleared"]["id"], "revision": change["cleared"]["revision"]}
    return {"id": change["goal"]["id"], "revision": change["goal"]["revision"]}


def _empty_state() -> dict:
    """空的严格重放累加器（上游 emptyGoalFoldState）。"""
    return {
        "goal": None,
        "roundsStarted": 0,
        "createdAt": None,
        "updatedAt": None,
        "lastRef": None,
        "seenGoalIds": set(),
    }


def apply_goal_change(state: dict, change: dict) -> None:
    """校验并应用一次解码后的变更到可变累加器（上游 applyGoalChange）。"""
    ref = goal_change_ref(change)
    if change["operation"] == "clear":
        current = state["goal"]
        if current is None:
            raise ValueError("goal clear requires a current goal")
        _require_next_revision(current, change["cleared"], "clear")
        if state["updatedAt"] is None:
            raise ValueError("current goal fold lacks updatedAt")
        if change["clearedAt"] < state["updatedAt"]:
            raise ValueError("goal clear timestamp cannot precede the current goal update")
        state["goal"] = None
        state["roundsStarted"] = 0
        state["createdAt"] = None
        state["updatedAt"] = None
        state["lastRef"] = ref
        return
    if change["operation"] == "create":
        goal = change["goal"]
        if goal["revision"] != 1 or goal["phase"] != "active" or change["roundsStarted"] != 0 \
                or (state["goal"] is not None and state["goal"]["phase"] != "complete") \
                or goal["id"] in state["seenGoalIds"]:
            raise ValueError("goal create requires a fresh active revision-one goal with zero rounds")
        state["seenGoalIds"].add(goal["id"])
    else:
        current = state["goal"]
        if current is None:
            raise ValueError(f"goal {change['operation']} requires a current goal")
        _validate_snapshot_transition(state, change, current)
    state["goal"] = change["goal"]
    state["roundsStarted"] = change["roundsStarted"]
    state["createdAt"] = change["createdAt"]
    state["updatedAt"] = change["updatedAt"]
    state["lastRef"] = ref


def apply_goal_event(state: dict, event: dict) -> None:
    """把一条会话事件应用到严格 durable goal fold（上游 applyGoalEvent）。"""
    if event["type"] == "goal/change":
        change = decode_goal_change(event.get("data"))
        if change is None:
            raise ValueError(f"goal change at session event {event['seq']} has an invalid kind")
        apply_goal_change(state, change)
        return
    if event["type"] == "user/message":
        source = goal_source(event["data"].get("source"))
        if source is None:
            return
        current = state["goal"]
        if current is None or current["phase"] != "active" or source["goalId"] != current["id"] \
                or source["revision"] != current["revision"] \
                or source["round"] != state["roundsStarted"] + 1 \
                or source["round"] > current["maxGoalRounds"]:
            raise ValueError(
                f"goal round at session event {event['seq']} is not the next admitted round of the active goal")
        state["roundsStarted"] = source["round"]


def fold_goal(events: tuple) -> dict:
    """从连续会话日志折叠当前目标状态（上游 foldGoal）。

    @returns 新 dict：{goal?, roundsStarted, createdAt?, updatedAt?, lastRef?}；
    activation 有意缺席（进程内态，上游同注释）。
    """
    state = _empty_state()
    for event in events:
        apply_goal_event(state, event)
    result = {"roundsStarted": state["roundsStarted"]}
    if state["goal"] is not None:
        result["goal"] = dict(state["goal"])
    if state["createdAt"] is not None:
        result["createdAt"] = state["createdAt"]
    if state["updatedAt"] is not None:
        result["updatedAt"] = state["updatedAt"]
    if state["lastRef"] is not None:
        result["lastRef"] = dict(state["lastRef"])
    return result
