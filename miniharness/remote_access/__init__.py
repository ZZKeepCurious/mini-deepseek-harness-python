"""Host 方法级访问控制（对齐 packages/api/remote-access）。

owner-only 部署下 `decide()` 恒 allow、`follow()` 只发 `{type:'baseline', owner:true}`，
与上游无注册非 owner policy 时的可观察行为一致。`register_policy` / `invalidate`
保留扩展面，供未来 principal/peer 与 Gateway 非 owner 门控立项时使用。
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from ..core.scope import Context, Service

__all__ = [
    "RemoteAccessService",
    "install_remote_access",
]


class RemoteAccessService(Service):
    """ctx.remoteAccess：快照 baseline + follow 流（owner 代次）。"""

    provide = "remoteAccess"

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        self._lock = threading.Lock()
        self._policies: dict[str, Any] = {}

    def register_policy(self, kind: str, policy: Any) -> Callable[[], None]:
        if kind == "owner":
            raise ValueError("remote access: cannot register a policy for owner")
        with self._lock:
            if kind in self._policies:
                raise ValueError(f"remote access: principal kind {kind!r} already has a policy")
            self._policies[kind] = policy
        self.invalidate()
        def dispose() -> None:
            with self._lock:
                self._policies.pop(kind, None)
            self.invalidate()
        return dispose

    def invalidate(self, _principal: dict | None = None) -> None:
        return

    def decide(self, request: dict) -> dict:
        principal = request.get("principal") or {}
        if principal.get("kind") in (None, "owner"):
            return {"kind": "allow"}
        action = request.get("action")
        if action == "remoteAccess/follow":
            return {"kind": "allow"}
        entry = self._policies.get(principal.get("kind"))
        if entry is None:
            return {"kind": "deny", "reason": "no-allow"}
        return {"kind": "allow"}

    def snapshot_for(self, principal: dict | None = None) -> dict:
        if principal is None or principal.get("kind") in (None, "owner"):
            return {"type": "baseline", "owner": True}
        return {"type": "baseline", "owner": False, "grants": []}


def install_remote_access(ctx: Context) -> RemoteAccessService:
    existing = ctx.get("remoteAccess")
    if existing is not None:
        return existing
    service = RemoteAccessService(ctx)
    ctx.set("remoteAccess", service)
    return service
