"""StateListener 同步载体（对齐 session-controller state/listener.ts）。"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

__all__ = ["StateListenerSync"]


class StateListenerSync:
    """revision 合并 + 可选 refreshMs 的 passive state 采样。"""

    def __init__(self, api, session_id: str, read_state: Callable[[], dict],
                 *, refresh_ms: int | None = None):
        self._api = api
        self._session_id = session_id
        self._read_state = read_state
        self._refresh_ms = refresh_ms
        self._revision = 0
        self._closed = False
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._disposers: list[Any] = []

    @property
    def closed(self) -> bool:
        return self._closed

    def is_current(self, revision: int) -> bool:
        with self._lock:
            return not self._closed and revision == self._revision

    def _notify(self, agent_id: str | None = None) -> None:
        if agent_id is not None and agent_id != self._session_id:
            return
        with self._lock:
            if self._closed:
                return
            self._revision += 1
        self._wake.set()

    def start(self) -> None:
        ctx = self._api.ctx
        self._disposers = [
            ctx.on("session/event", lambda p: self._notify(
                getattr(p.get("session"), "session_id", None)), global_=True),
            ctx.on("agent/status", lambda p: self._notify(
                getattr(p.get("agent"), "id", None)), global_=True),
            ctx.on("session/created", lambda p: self._notify(
                getattr(p.get("session"), "session_id", None)), global_=True),
            ctx.on("session/disposed", lambda p: self._notify(
                getattr(p.get("session"), "session_id", None)), global_=True),
            ctx.on("agent/created", lambda p: self._notify(
                getattr(p.get("agent"), "id", None)), global_=True),
            ctx.on("agent/disposed", lambda p: self._notify(
                getattr(p.get("agent"), "id", None)), global_=True),
        ]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._wake.set()
        for dispose in self._disposers:
            dispose()
        self._disposers = []

    def iter_versioned_states(self):
        last_revision = -1
        while not self.closed:
            with self._lock:
                current_revision = self._revision
            if last_revision == current_revision:
                self._wake.clear()
                if self._refresh_ms is None:
                    self._wake.wait()
                else:
                    self._wake.wait(self._refresh_ms / 1000.0)
                if self.closed:
                    return
                with self._lock:
                    if (self._refresh_ms is not None
                            and last_revision == self._revision
                            and not self._closed):
                        self._revision += 1
                        current_revision = self._revision
                    else:
                        current_revision = self._revision
            if not self.is_current(current_revision):
                return
            last_revision = current_revision
            state = self._read_state()
            if self.is_current(last_revision):
                yield {"state": state, "revision": last_revision}
