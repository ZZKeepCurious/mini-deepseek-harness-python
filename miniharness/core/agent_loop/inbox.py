"""Inbox：上游 Inbox（packages/core/agent/src/inbox.ts）的同步复刻。

双队列（next-turn / next-step）+ 持久化 `agent/inbox/spliced` 事件：
每次入队 / 认领 / 清除都作为 splice 落会话日志（seq 单调推进，冷恢复可
重放重建 live 状态），并触发 inserted / discarded / claimed 通知。mini 无
上游 session/event 总线，live 状态由自身在每次 mutate 中同步维护。

契约（与上游逐条一致）：
  * target ∈ ('next-turn', 'next-step')；splice 事件形状
    {target, start, removedCount?, inserted, outcome?}；
    只有"丢弃式删除"（clear / remove / replace，discardRemoved=true）
    才写 outcome:'canceled' 并触发 discarded 通知；认领（claim）是纯删除
    （discardRemoved=false），无 outcome、无 discarded 通知。
  * claim(target)：永远清空 next-step，target='next-turn' 时再从 next-turn
    取一条；按认领序触发 claimed 通知。
  * 每轮 turn 首个认领目标必须是 'next-turn'（上游 agent.ts:261），
    后续 step 用 'next-step'。
"""
from __future__ import annotations

from typing import Any, Callable

__all__ = ["INBOX_TARGETS", "Inbox"]

INBOX_TARGETS = ("next-turn", "next-step")


class Inbox:
    """持有两列待处理 user 消息的日志一致队列。"""

    def __init__(self, session: Any, notifications: dict | None = None):
        self._session = session
        self._notify: dict[str, Callable] = notifications or {}
        self._state: dict[str, list[dict]] = {"next-turn": [], "next-step": []}
        # 冷恢复：重放既有 splice 事件重建 live 状态
        for event in session.events:
            if event["type"] == "agent/inbox/spliced":
                self._apply(event["data"])

    # ---------- 只读视图 ----------

    @property
    def next_turn(self) -> list[dict]:
        return list(self._state["next-turn"])

    @property
    def next_step(self) -> list[dict]:
        return list(self._state["next-step"])

    @property
    def has_pending(self) -> bool:
        return bool(self._state["next-turn"] or self._state["next-step"])

    def __len__(self) -> int:
        return len(self._state["next-turn"]) + len(self._state["next-step"])

    def __bool__(self) -> bool:
        return self.has_pending

    def __iter__(self):
        return iter(self.next_turn + self.next_step)

    # ---------- 公开操作（对齐上游 splice 族） ----------

    def append(self, target: str, message: dict) -> None:
        """追加一条消息到目标队列（对应上游 splice 尾部插入，无 outcome）。"""
        self.splice(target, len(self._state[target]), 0, [message])

    def prepend(self, target: str, message: dict) -> None:
        """头部插入一条消息（对应上游 prepend）。"""
        self.splice(target, 0, 0, [message])

    def replace(self, message_id: str, new_message: dict) -> bool:
        """按 id 替换一条消息（丢弃旧消息 → outcome 'canceled'）。"""
        location = self._locate(message_id)
        if location is None:
            return False
        self.splice(location["target"], location["index"], 1, [new_message])
        return True

    def remove(self, message_id: str) -> bool:
        """按 id 移除一条消息（丢弃 → outcome 'canceled'）。"""
        location = self._locate(message_id)
        if location is None:
            return False
        self.splice(location["target"], location["index"], 1, [])
        return True

    def clear(self) -> None:
        """清空两列（丢弃式删除：next-step 先、next-turn 后，对齐上游 clear）。"""
        self.splice("next-step", 0, len(self._state["next-step"]), [])
        self.splice("next-turn", 0, len(self._state["next-turn"]), [])

    def claim(self, target: str, turn: int | None = None) -> list[dict]:
        """认领下一步输入：清空 next-step，target='next-turn' 时再取一条 next-turn。

        @param target - 本轮 step 的认领目标（首个 step 必须 'next-turn'）。
        @param turn - 供 claimed 通知携带的回合号（无则省略）。
        @returns 按认领序的已认领消息列表（可能为空）。
        """
        claimed = self._mutate("next-step", 0, len(self._state["next-step"]), [],
                               discard_removed=False)
        if target == "next-turn":
            claimed.extend(self._mutate("next-turn", 0, 1, [], discard_removed=False))
        notify = self._notify.get("claimed")
        if notify is not None:
            for message in claimed:
                notify(message, turn)
        return claimed

    def splice(self, target: str, start: int, delete_count: int,
               inserted: list[dict], discard_removed: bool = True) -> list[dict]:
        """公开 splice：插入 + 删除一个区间；discard_removed 决定 outcome 与 discarded 通知。

        @returns 被移除的消息列表。
        """
        return self._mutate(target, start, delete_count, list(inserted),
                            discard_removed=discard_removed)

    # ---------- 内部 ----------

    def _locate(self, message_id: str) -> dict | None:
        for target in INBOX_TARGETS:
            for index, message in enumerate(self._state[target]):
                if message.get("id") == message_id:
                    return {"target": target, "index": index}
        return None

    def _mutate(self, target: str, start: int, delete_count: int,
                inserted: list[dict], discard_removed: bool = True) -> list[dict]:
        if target not in INBOX_TARGETS:
            raise TypeError(f'inbox splice target must be one of {list(INBOX_TARGETS)}, got {target!r}')
        inbox = self._state[target]
        actual_start = max(0, min(start, len(inbox)))
        actual_delete = max(0, min(delete_count, len(inbox) - actual_start))
        if actual_delete == 0 and not inserted:
            return []
        splice: dict[str, Any] = {"target": target, "start": actual_start, "inserted": inserted}
        if actual_delete:
            splice["removedCount"] = actual_delete
        if discard_removed and actual_delete:
            splice["outcome"] = "canceled"
        self._session.append("agent/inbox/spliced", splice)
        removed = inbox[actual_start:actual_start + actual_delete]
        del inbox[actual_start:actual_start + actual_delete]
        inbox[actual_start:actual_start] = list(inserted)
        if discard_removed:
            notify = self._notify.get("discarded")
            if notify is not None:
                for message in removed:
                    notify(message)
        notify = self._notify.get("inserted")
        if notify is not None:
            for message in inserted:
                notify(message)
        return removed

    def _apply(self, splice: dict) -> None:
        """重放一条 splice 事件（冷恢复路径，不触发通知、不再落盘）。"""
        target = splice["target"]
        inbox = self._state[target]
        start = splice["start"]
        removed_count = splice.get("removedCount", 0)
        del inbox[start:start + removed_count]
        inbox[start:start] = [dict(message) for message in splice.get("inserted", [])]