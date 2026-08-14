"""第 2 章：插件上下文 + 事件总线 + 作用域化注册。

对应 dsh 真实源码：vendor/cordis（Context / 事件派发）+ core/scope。

四种派发模式：
  emit       观察式，不等待、无返回值，按注册序同步调用
  waterfall  流水线（around-middleware），必须 next() 委派，不调 next 即短路
  parallel   并行，等待全部监听完成（此处为同步近似）
  serial     串行，按序执行，有返回值
"""
from __future__ import annotations

from typing import Any, Callable


class Context:
    """服务仓库 + 事件总线 + 可逆副作用容器（父子链 = 作用域）。"""

    def __init__(self, parent: "Context | None" = None, name: str = "root"):
        self.parent = parent
        self.name = name
        self._services: dict[str, Any] = {}
        self._listeners: dict[str, list[Callable]] = {}
        self._disposers: list[Callable] = []
        self._disposed = False

    def _assert_alive(self) -> None:
        if self._disposed:
            raise RuntimeError(f"上下文 {self.name} 已销毁，拒绝注册")

    # ---------- 服务（Service） ----------

    def provide(self, key: str, value: Any) -> Callable:
        """提供服务，返回 disposer。同 key 重复提供 = 冲突（fail loud）。"""
        self._assert_alive()
        if key in self._services:
            raise RuntimeError(f"服务 {key} 已在 {self.name} 提供")
        self._services[key] = value
        return self.effect(lambda: self._services.pop(key, None))

    def inject(self, key: str) -> Any:
        """按 key 查找服务：沿父子链向上（作用域可见性）。"""
        if key in self._services:
            return self._services[key]
        if self.parent is not None:
            return self.parent.inject(key)
        raise KeyError(f"服务 {key} 未提供")

    # ---------- 事件派发（四种模式） ----------

    def on(self, event: str, fn: Callable) -> Callable:
        """注册监听器，返回 disposer。"""
        self._assert_alive()
        self._listeners.setdefault(event, []).append(fn)

        def disposer() -> None:
            lst = self._listeners.get(event)
            if lst and fn in lst:
                lst.remove(fn)

        return self.effect(disposer)

    def _listeners_for(self, event: str) -> list[Callable]:
        """收集自身 + 祖先链的监听器（子先于父，各层保持注册序）。"""
        chain: list[Callable] = []
        node: Context | None = self
        while node is not None:
            chain = list(node._listeners.get(event, [])) + chain
            node = node.parent
        return chain

    def emit(self, event: str, payload: Any = None) -> None:
        for fn in self._listeners_for(event):
            fn(payload)

    def waterfall(self, event: str, payload: Any = None) -> Any:
        """around-middleware：监听器签名 fn(payload, next)。
        调用 next(new) 继续下一位；不调用即短路，当前返回值就是最终决策。"""
        listeners = self._listeners_for(event)
        idx = 0

        def step(cur: Any) -> Any:
            nonlocal idx
            if idx >= len(listeners):
                return cur
            fn = listeners[idx]
            idx += 1
            return fn(cur, lambda new=cur: step(new))

        return step(payload)

    def parallel(self, event: str, payload: Any = None) -> list:
        return [fn(payload) for fn in self._listeners_for(event)]

    def serial(self, event: str, payload: Any = None) -> list:
        return [fn(payload) for fn in self._listeners_for(event)]

    # ---------- 副作用与生命周期 ----------

    def effect(self, fn: Callable) -> Callable:
        """登记一个可逆副作用；dispose 时按注册逆序回滚。"""
        self._assert_alive()
        self._disposers.append(fn)
        return fn

    def dispose(self) -> None:
        """卸载：逆序回滚全部副作用，之后拒绝一切注册。"""
        if self._disposed:
            return
        for fn in reversed(self._disposers):
            fn()
        self._disposers.clear()
        self._disposed = True

    # 便利方法
    def create_scope(self, name: str) -> "Context":
        return Context(parent=self, name=name)


class PluginManager:
    """依赖驱动的插件激活：inject 满足才 apply，全部激活或明确报错。

    简化说明：真实 Cordis 由 apply 期间的 provide/effect 动态登记依赖，
    这里用声明式 provides 字段近似（见教程第 2 章 2.3 节）。
    """

    def __init__(self, root: Context):
        self.root = root

    def activate(self, plugins: list[dict]) -> list[tuple[str, Callable]]:
        remaining = [dict(p) for p in plugins]
        provided = set(self.root._services)
        done: list[tuple[str, Callable]] = []
        while remaining:
            progressed = False
            for p in list(remaining):
                if all(k in provided for k in p.get("inject", [])):
                    snapshot = len(self.root._disposers)
                    p["apply"](self.root)
                    disposer = self._collect_after(snapshot)
                    done.append((p["name"], disposer))
                    provided.update(p.get("provides", []))
                    remaining.remove(p)
                    progressed = True
            if not progressed:
                raise RuntimeError(
                    "插件依赖无法满足（可能循环或缺失）: " + ", ".join(p["name"] for p in remaining)
                )
        return done

    def _collect_after(self, snapshot: int) -> Callable:
        added = self.root._disposers[snapshot:]

        def dispose() -> None:
            for fn in reversed(added):
                fn()

        return dispose