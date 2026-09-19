"""文件系统观测策略（对齐 packages/fs/fs-observation-policy）。

只注册事件监听，不提供服务：按 owner（`actor.agent.session`，弱引用）记录每次权威
present/absent 观测；`fs/write-intent`（waterfall）由观测态派生写意图、
`fs/edit-intent`（waterfall）派生编辑版本守卫、`fs/observed`（emit）记录观测。
未装本插件时，工具保留裸后端的无条件变更行为。

事件载荷约定（mini 单载荷派发）：
- `fs/write-intent` waterfall payload = `(target, actor)`，监听器不调 next 即占槽；
- `fs/edit-intent` waterfall payload = `(target, actor)`；
- `fs/observed` emit payload = `(target, observation, actor)`。
"""
from __future__ import annotations

import weakref
from typing import Any

from ..core.scope import Context
from .types import FsError, FsObservation, FsTarget, FsWriteIntent

__all__ = ["ObservedStateGate", "install_fs_observation_policy", "name"]

name = "fs-observation-policy"


class ObservedStateGate:
    """每上下文观测态 + 三个 `fs/*` 决策。"""

    def __init__(self) -> None:
        self._observed: "weakref.WeakKeyDictionary[object, dict[str, FsObservation]]" = (
            weakref.WeakKeyDictionary())

    @staticmethod
    def owner(actor: Any) -> Any:
        """从事件 actor 派生观测态 owner（`actor.agent.session`）。"""
        agent = getattr(actor, "agent", None)
        return getattr(agent, "session", None) if agent is not None else None

    def _get(self, owner: object, target_key: str) -> FsObservation | None:
        return self._observed.get(owner, {}).get(target_key)

    def _set(self, owner: object, target_key: str, observation: FsObservation) -> None:
        by_target = self._observed.get(owner)
        if by_target is None:
            by_target = {}
            self._observed[owner] = by_target
        by_target[target_key] = observation

    def clear(self) -> None:
        """丢弃全部记录态（HMR 安全 / 拆解）。"""
        self._observed = weakref.WeakKeyDictionary()

    def write_intent(self, target: FsTarget, actor: Any) -> FsWriteIntent:
        """未观测或已确认缺失 → createIfAbsent；已确认存在 → replaceIfVersion。"""
        owner = self.owner(actor)
        prior = self._get(owner, target.target_key) if owner is not None else None
        if prior is not None and prior.kind == "present":
            return FsWriteIntent("replaceIfVersion", prior.version)
        return FsWriteIntent("createIfAbsent")

    def edit_intent(self, target: FsTarget, actor: Any) -> dict:
        """未观测 → FS_NOT_OBSERVED；确认缺失 → FS_NOT_FOUND；存在 → 观测版本。"""
        owner = self.owner(actor)
        prior = self._get(owner, target.target_key) if owner is not None else None
        if owner is None or prior is None:
            raise FsError(f'edit requires reading "{target.display_path}" first',
                          "FS_NOT_OBSERVED")
        if prior.kind == "absent":
            raise FsError(f'cannot edit "{target.display_path}": not found', "FS_NOT_FOUND")
        return {"version": prior.version}

    def observe(self, target: FsTarget, observation: FsObservation, actor: Any) -> None:
        owner = self.owner(actor)
        if owner is not None:
            self._set(owner, target.target_key, observation)


def install_fs_observation_policy(ctx: Context) -> ObservedStateGate:
    """注册三个 `fs/*` 监听（对齐上游 `apply(ctx)`）；返回 gate 供测试/宿主观察。"""
    gate = ObservedStateGate()

    ctx.effect(lambda: lambda: gate.clear(),
               "fs-observation-policy observed-state teardown")

    def on_write_intent(payload: Any, next_fn: Any) -> Any:
        target, actor = payload
        return gate.write_intent(target, actor)

    def on_edit_intent(payload: Any, next_fn: Any) -> Any:
        target, actor = payload
        return gate.edit_intent(target, actor)

    def on_observed(payload: Any) -> None:
        target, observation, actor = payload
        gate.observe(target, observation, actor)

    ctx.on("fs/write-intent", on_write_intent)
    ctx.on("fs/edit-intent", on_edit_intent)
    ctx.on("fs/observed", on_observed)
    return gate
