"""本地注册表的事件路由（对齐 jobs-local/src/events.ts）。

订阅按 filter 归档，每次提交对每个匹配监听器投递一次并做包含：一个观察者抛错
不能破坏已完成的提交。
"""
from __future__ import annotations

from ..core.dsh_scope import AnonymousEntries, ScopedLayers, scope_of

__all__ = ["JobEventHub", "JobLayer"]


class JobLayer:
    """一个 scope 的贡献：从它挂载的作业 controller，与在那里注册的
    `{owners:'scope'}` 订阅。两张表都是匿名条目（贡献由自己的 disposer 标识，
    同名不可遮蔽）。`isEmpty` 两表全空才算空。"""

    __slots__ = ("controllers", "scoped")

    def __init__(self) -> None:
        self.controllers = AnonymousEntries()
        self.scoped = AnonymousEntries()

    def isEmpty(self) -> bool:
        return self.controllers.isEmpty() and self.scoped.isEmpty()


class JobEventHub:
    """把事件路由给订阅。

    `{owner}` 与 `{owners:'all'}` 订阅无论注册在哪里都针对每个事件求值；
    `{owners:'scope'}` 订阅归入注册 ctx 的 scope 层，只接收该层覆盖的 owner
    （全局层——未打标上下文注册——接收每个 owner）。filter 形状：
    `{"owner": SessionId}` 或 `{"owners": "all" | "scope"}`。
    """

    def __init__(self, layers: ScopedLayers, warn) -> None:
        self._layers = layers
        self._warn = warn
        self._unscoped: list[dict] = []

    def subscribe(self, ctx, filter_: dict, listener) -> object:
        """把 listener 注册为 `ctx` 的 effect；返回注销的 disposer。"""
        subscription = {"filter": filter_, "listener": listener}
        if filter_.get("owners") == "scope":
            return self._layers.effect(
                ctx, lambda layer: layer.scoped.append(subscription),
                "jobs.events.subscribe()")

        def register():
            self._unscoped.append(subscription)

            def dispose() -> None:
                try:
                    self._unscoped.remove(subscription)
                except ValueError:
                    pass

            return dispose

        return ctx.effect(register, "jobs.events.subscribe()")

    def emit(self, event: dict, owner_agent) -> None:
        """把事件投递给每个匹配订阅，逐个包含监听器。"""
        owner_id = getattr(owner_agent, "id", None) if owner_agent is not None else None
        for subscription in list(self._unscoped):
            filter_ = subscription["filter"]
            if "owner" in filter_ and owner_id is not None and owner_id != filter_["owner"]:
                continue
            self._deliver(subscription, event)
        for subscription in list(self._layers.global_layer.scoped.values()):
            self._deliver(subscription, event)
        scope = scope_of(owner_agent.ctx) if owner_agent is not None else None
        for layer in self._layers.chain_layers(scope):
            for subscription in list(layer.scoped.values()):
                self._deliver(subscription, event)

    def _deliver(self, subscription: dict, event: dict) -> None:
        try:
            subscription["listener"](event)
        except Exception as error:  # noqa: BLE001 - 监听器失败被包含，不破坏提交
            self._warn(f"jobs: event listener threw on {event.get('type')}: {error}")
