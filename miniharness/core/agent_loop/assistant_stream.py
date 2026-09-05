"""Agent 内一次模型 attempt 的流式封装：压缩 + 组装 + 终态结算。

对应 dsh 真实源码：packages/core/agent-loop/src/assistant-stream.ts
（AssistantStreamAttempt）。

同时持有两个独立消费者：
  * `AssistantStreamAccumulator`——压缩原始 chunk → `AssistantStreamRecord[]`
    （compact stream），供 `stream` getter 写入持久事件。
  * `BlockAssembler`——增量组装原始 chunk → ContentBlock[]，供
    `blocks()`/`interrupted_blocks()`/`usage`/`finish`/`replay_state`。

`settle()` 保证终态：append 成功 → committed；append 抛错 → abandon；同时
发布终态帧。`abandon()` 发布 abandoned 终态。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from ...llm import AssistantStreamAccumulator, BlockAssembler


def _now_ms() -> int:
    return int(time.time() * 1000)


def _default_emit(frame: dict) -> None:
    """默认终端帧发布点为 no-op（mini 无进程内实时 wire 帧消费方）。"""


class AssistantStreamAttempt:
    """一次模型 attempt 的流式封装（上游 assistant-stream.ts AssistantStreamAttempt）。"""

    def __init__(
        self,
        session_id: str,
        attempt: int,
        turn: int,
        step: int,
        emit: Callable[[dict], None] | None = None,
    ):
        self.attempt_id = f"{session_id}:{attempt}"
        self.turn = turn
        self.step = step
        self._emit = emit or _default_emit
        self._accumulator = AssistantStreamAccumulator()
        self._assembler = BlockAssembler()
        self._index = 0
        self._revision = 0
        self._terminal = False

    @property
    def ended(self) -> bool:
        return self._terminal

    def _next_revision(self) -> int:
        self._revision += 1
        return self._revision

    def start(self) -> None:
        """发布 opening 标记（首块投递之前）。"""
        self._emit({
            "type": "start",
            "attemptId": self.attempt_id,
            "revision": self._next_revision(),
            "turn": self.turn,
            "step": self.step,
        })

    def push(self, chunk: dict) -> None:
        """一次快照：同时喂压缩、组装与实时发布。"""
        timed = self._accumulator.push_chunk_time(_now_ms(), chunk)
        self._assembler.push(timed.chunk)
        self._emit({
            "type": "chunk",
            "attemptId": self.attempt_id,
            "revision": self._next_revision(),
            "index": self._index,
            "time": timed.time,
            "chunk": timed.chunk,
        })
        self._index += 1

    def settle(self, event_type: str, append: Callable[[], int]) -> None:
        """持久事件提交成功后发布 committed 终态。

        event_type: 'assistant/message' | 'assistant/attempt'
        append: 同步持久 append，返回提交的 seq。
        """
        try:
            seq = append()
        except Exception:
            self.abandon()
            raise
        self._terminal = True
        self._emit({
            "type": "end",
            "attemptId": self.attempt_id,
            "revision": self._next_revision(),
            "index": self._index,
            "outcome": {"kind": "committed", "eventType": event_type, "seq": seq},
        })

    def abandon(self) -> None:
        """无持久事件可提交时发布 abandoned 终态。"""
        self._terminal = True
        self._emit({
            "type": "end",
            "attemptId": self.attempt_id,
            "revision": self._next_revision(),
            "index": self._index,
            "outcome": {"kind": "abandoned"},
        })

    @property
    def stream(self) -> list[dict]:
        """落事件用的 compact stream（AssistantStreamRecord[]）。"""
        return self._accumulator.snapshot()

    def blocks(self) -> list[dict]:
        return self._assembler.blocks()

    def interrupted_blocks(self) -> list[dict]:
        return self._assembler.interrupted_blocks()

    @property
    def usage(self) -> dict | None:
        return self._assembler.usage

    @property
    def finish(self) -> dict | None:
        return self._assembler.finish

    @property
    def replay_state(self) -> Any | None:
        return self._assembler.replay_state
