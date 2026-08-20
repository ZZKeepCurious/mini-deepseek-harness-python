"""dsh-scope 全协议（对齐 packages/core/scope/src/index.ts + store.ts，2026-08-20 Phase 4）。

作用域原语：铸一枚把注册打上不透明身份标号（ScopeKey）的 Cordis 上下文，并为该身份
构建"仅用于路由"的事件载波（scopeTarget）。一条 scopeParents 关系同时驱动两个方向：

  * 注册视图向下继承——子 scope 经 ScopedLayers 看到祖先的层（chainLayers/merge）；
  * 事件接纳向上延伸——挂祖先 scope 标号的监听器收到派发到后裔 key 的事件（scopeTarget）。

等价物（上游 1:1）：

  * ScopeKey        —— 不透明、按身份比较的 scope 键（object）
  * bind_scope_parent —— 把 parent 绑定为 key 的 enclosing scope（一次性 + 可 rebind，
    带环检测；rebind 仅原绑定方持有）
  * scope_parent_of / scope_chain_of —— 读一个键的父 / 键到根的链（nearest-first）
  * create_scope(ctx, key, parent) —— 铸 scope：ctx.plugin(noop) 铸独立 fiber，
    fiber 上下文打上 key 标号；返回 {ctx, raw_dispose, dispose}（dispose 记忆化
    quiesceFiber：拆解后继续等 fiber.inertia 排空）
  * scope_of(ctx) —— 读上下文继承链上最近的 scope 标号（无标号 → None）
  * scope_target(base, key) —— 铸不透明载波：保留 base 自身 filter，接受未打标
    监听器（全局）+ 打标监听器中键是 key 或 key 祖先的；base 属性不暴露（事件参数
    携带真实 subject）
  * is_scope_carrier / carrier_key_of —— 载波判定与读键
  * NamedEntries / AnonymousEntries / ScopedLayers —— scope-aware 注册表存储
    （插入序、借值、活跃迭代器、单层回收；ScopedLayers 的 effect 把一次同步层变更
    挂到注册上下文：scope 可见性 + effect 归属）

Python 载体说明（教学便捷，语义对齐）：
  * scope_of 走 Context 的 parent 链近似原型继承（上游 JS 原型链）；
  * Context.create_scope 的 parent 缺省取最近 enclosing scope（上游需显式传参）。
"""
from __future__ import annotations

import asyncio
import inspect
import weakref
from typing import Any, Callable, Iterator

class ScopeKey:
    """不透明、按身份比较、可弱引用的 scope 键（上游 object 的 Python 载体）。

    上游用裸 object 作键；Python 的裸 object() 不可弱引用（WeakKeyDictionary 拒收），
    故以最小类实例承担同一角色：身份比较（is）、无字段、可弱引用。
    """

    __slots__ = ("__weakref__",)

    def __repr__(self) -> str:
        return f"<ScopeKey {id(self):x}>"

_scope_parents: "weakref.WeakKeyDictionary[object, object]" = weakref.WeakKeyDictionary()


def _is_awaitable(value: Any) -> bool:
    return inspect.isawaitable(value)


class ScopeParentBinding:
    """重绑某个 scope 键父链接的特权句柄（对齐上游 ScopeParentBinding）。

    仅原始 bind 的持有者可用；旧父下的产出被保留时重绑无效（blank-session 重组
    契约，由持有者保证）。
    """

    __slots__ = ("_key",)

    def __init__(self, key: ScopeKey):
        self._key = key

    def rebind(self, parent: ScopeKey) -> None:
        """把绑定键重链到新父（与 bind 相同的环检测）。"""
        link_scope_parent(self._key, parent)


def link_scope_parent(key: ScopeKey, parent: ScopeKey) -> None:
    """带环检测的写：从 parent 沿父链走到根，撞上 key 即拒绝（每条链都要走到根）。"""
    cursor = parent
    while cursor is not None:
        if cursor is key:
            raise RuntimeError("dsh-scope: scope parent link would form a cycle")
        cursor = _scope_parents.get(cursor)
    _scope_parents[key] = parent


def bind_scope_parent(key: ScopeKey, parent: ScopeKey) -> ScopeParentBinding:
    """把 parent 绑定为 key 的 enclosing scope（恰一次）。

    已绑定的键抛错：除原始绑定方（唯一持有 rebind 句柄者）外，无人可改祖先。
    """
    if key in _scope_parents:
        raise RuntimeError(
            "dsh-scope: scope key is already bound to a parent; "
            "re-linking requires the binding returned by the original bind")
    link_scope_parent(key, parent)
    return ScopeParentBinding(key)


def scope_parent_of(key: ScopeKey) -> ScopeKey | None:
    """读一个键的 enclosing scope（根 scope 返回 None）。"""
    return _scope_parents.get(key)


def scope_chain_of(key: ScopeKey | None) -> list:
    """键到根祖先的链，nearest-first：`[key, parent, grandparent, ...]`。"""
    chain: list = []
    cursor = key
    while cursor is not None:
        chain.append(cursor)
        cursor = _scope_parents.get(cursor)
    return chain


class Scope:
    """铸出的注册作用域及其静默拆解边界（对齐上游 Scope 接口）。

    ctx          —— 作用域内注册经此上下文进行（fiber-backed）
    raw_dispose  —— 精确 Cordis disposer（用于把本 scope 嵌套进有序复合 effect）
    dispose()    —— 拆解全部作用域注册；并发调用共享同一完成；无运行 loop 时同步
                    拆解（min 便利：上游恒异步，mini 在同步门面下直接跑 fiber 拆解）
    """

    __slots__ = ("ctx", "raw_dispose", "_completion", "_disposed")

    def __init__(self, ctx: Any, fiber: Any):
        self.ctx = ctx
        self.raw_dispose: Callable[[], Any] = fiber.dispose
        self._completion: Any = None
        self._disposed = False

    def dispose(self) -> Any | None:
        """拆解作用域（记忆化 quiesceFiber：拆解后等 fiber.inertia 排空）。"""
        if self._disposed:
            return self._completion
        self._disposed = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            # 无运行 loop：同步拆解（fiber 拆解同步路径）；返回的 inertia 若可 await
            # 已无法在此刻结算，原样保留（调用方通常只关心注册已被逆序回滚）。
            result = self.raw_dispose()
            self._completion = result
            return result
        coro = quiesce_fiber(self.ctx.fiber)
        task = loop.create_task(coro)
        self._completion = task
        return task

    def __getattr__(self, name: str) -> Any:
        """把未知属性代理给底层上下文（scope.scope_key / .emit / .on / .get ...
        直接可用；上游 Scope 无此代理，为 mini 便捷面，语义不变）。"""
        return getattr(self.ctx, name)


async def quiesce_fiber(fiber: Any) -> None:
    """拆解 fiber 并跟随异步拆解直到惯性排空（对齐上游 quiesceFiber：
    `await Promise.resolve(fiber.dispose()); while (inertia) await inertia`）。

    mini 的 Fiber.inertia 在完成不清零（上游完成后置 undefined），故按"每个新
    惯性对象各消费一次"逼近同一语义：拆解链上新产生的惯性（卸载→重装）逐个
    await，直至不再产生。
    """
    result = fiber.dispose()
    if _is_awaitable(result):
        await result
    seen: Any = None
    while fiber.inertia is not None and fiber.inertia is not seen:
        seen = fiber.inertia
        await seen


def _scope_noop(ctx: Any, config: Any = None) -> None:
    """create_scope 的背衬 no-op 插件（对齐上游 createScope = ctx.plugin(scope)）。"""
    return None


def create_scope(ctx: Any, key: ScopeKey,
                 parent: ScopeKey | None = None,
                 name: str = "scope") -> Scope:
    """在 ctx 下铸一个 scope。

    作用域上下文继承铸造方插件 fiber 的依赖 API，并拥有经它做出的每笔注册。
    parent 可选：先经 bind_scope_parent 绑定 enclosing scope（绑定句柄内部保留）。
    """
    if parent is not None:
        bind_scope_parent(key, parent)
    fiber = ctx.plugin({"name": name, "apply": _scope_noop})
    scoped = fiber.context
    scoped._scope_key = key
    return Scope(scoped, fiber)


def scope_of(ctx: Any) -> ScopeKey | None:
    """读上下文继承链上最近的 scope 标号（无标号上下文 → None）。

    沿 parent 链向上找第一个带 _scope_key 的节点（上游 `ctx[kScope]` 的原型继承
    等价；Scope 包装经 __getattr__ 代理亦可用）。
    """
    node = ctx
    while node is not None:
        key = getattr(node, "_scope_key", None)
        if key is not None:
            return key
        node = getattr(node, "parent", None)
    return None


class _ScopeCarrier:
    """scope_target 的载波实现（不透明：不暴露 base 属性，事件参数携带真实 subject）。

    保留 base 自身的 Cordis filter（mini 约定钩子 `_context_filter`），再按 scope
    图接纳：未打标监听器全局接纳；打标监听器须是载波键或载波键的祖先。
    """

    __slots__ = ("base", "key", "base_filter", "__weakref__")

    def __init__(self, base: Any, key: ScopeKey | None):
        self.base = base
        self.key = key
        self.base_filter = getattr(base, "_context_filter", None)

    def admit(self, ctx: Any) -> bool:
        if self.base_filter is not None and not self.base_filter(ctx):
            return False
        tag = scope_of(ctx)
        if tag is None:
            return True
        cursor = self.key
        while cursor is not None:
            if cursor is tag:
                return True
            cursor = scope_parent_of(cursor)
        return False


_carrier_keys: "weakref.WeakKeyDictionary[_ScopeCarrier, object]" = weakref.WeakKeyDictionary()


def scope_target(base: Any, key: ScopeKey | None):
    """铸不透明接收器：保留 base 过滤、全局接纳未打标监听器、按键或键祖先接纳打标监听器。

    事件只沿 scope 链向上流、绝不向下：低于派发键的标号保持排除。
    """
    carrier = _ScopeCarrier(base, key)
    _carrier_keys[carrier] = key
    return carrier


def is_scope_carrier(value: Any) -> bool:
    """测试一个值是否为 scope_target 造的载波。"""
    return isinstance(value, _ScopeCarrier)


def carrier_key_of(value: Any) -> ScopeKey | None:
    """读载波的路由键（非载波 / 无键载波 → None）。"""
    if not is_scope_carrier(value):
        return None
    return _carrier_keys.get(value)


class NamedEntries:
    """插入序命名条目表，调用方负责重复诊断（对齐上游 NamedEntries）。

    值被借用。迭代器在一个非空表代内是活着的；清空整表后与新插入脱离
    （drain 后换新表，旧迭代器不再见后续插入）。每次成功插入返回该条目的幂等撤销。
    """

    def __init__(self, duplicate_error: Callable[[str], Exception]):
        self._data: dict[str, Any] = {}
        self._duplicate_error = duplicate_error

    def insert(self, name: str, value: Any) -> Callable[[], None]:
        data = self._data
        if name in data:
            raise self._duplicate_error(name)
        data[name] = value
        active = True

        def undo() -> None:
            nonlocal active
            if not active:
                return
            active = False
            data.pop(name, None)
            if not data and self._data is data:
                self._data = {}

        return undo

    def get(self, name: str) -> Any | None:
        return self._data.get(name)

    def has(self, name: str) -> bool:
        return name in self._data

    def keys(self) -> Iterator[str]:
        return iter(self._data)

    def entries(self) -> Iterator[tuple[str, Any]]:
        return iter(self._data.items())

    def values(self) -> Iterator[Any]:
        return iter(self._data.values())

    def isEmpty(self) -> bool:
        return not self._data


class AnonymousEntries:
    """插入序匿名条目表，注册身份相互独立（对齐上游 AnonymousEntries）。

    相等的值仍是不同注册。迭代器在一个非空表代内是活着的；清空整表后与新插入脱离。
    """

    def __init__(self):
        self._data: dict[object, Any] = {}
        self._counter = 0

    def append(self, value: Any) -> Callable[[], None]:
        data = self._data
        self._counter += 1
        key = f"anon-{self._counter}"
        data[key] = value
        active = True

        def undo() -> None:
            nonlocal active
            if not active:
                return
            active = False
            data.pop(key, None)
            if not data and self._data is data:
                self._data = {}

        return undo

    def values(self) -> Iterator[Any]:
        return iter(self._data.values())

    def isEmpty(self) -> bool:
        return not self._data


class ScopedLayers:
    """一个注册表的全局层与精确 scope 层（对齐上游 ScopedLayers）。

    读不会创建 scope 层。注册同时派生可见性与 effect 归属（scope_of(ctx)）、在
    通知前收集撤销，并且只回收一个完全空的聚合层。
    """

    def __init__(self, create_layer: Callable[[ScopeKey | None], Any],
                 on_change: Callable[[], None]):
        self._create_layer = create_layer
        self._on_change = on_change
        self.global_layer: Any = create_layer(None)
        self._scoped: dict[object, Any] = {}

    def peek(self, scope: ScopeKey | None) -> Any | None:
        """读一个精确 scope 层的现有覆盖（故意链盲：寻址某 scope 自身的贡献
        不得静默捡起祖先的——需要继承时用 chain_layers）。不存在不创建。"""
        if scope is None:
            return None
        return self._scoped.get(scope)

    def chain_layers(self, scope: ScopeKey | None) -> list:
        """scope 父链上现存覆盖，最远祖先在前、精确 scope 最后（调用方按序叠放
        时最近 scope 的条目作最后裁定）。"""
        layers: list = []
        for key in reversed(scope_chain_of(scope)):
            layer = self._scoped.get(key)
            if layer is not None:
                layers.append(layer)
        return layers

    def merge(self, scope: ScopeKey | None, pick: Callable[[Any], NamedEntries]) -> dict:
        """全局命名条目 + 按序 scope 链遮蔽（最远祖先在前，最近 scope 条目同名胜出）。"""
        merged = dict(pick(self.global_layer).entries())
        for layer in self.chain_layers(scope):
            for name, value in pick(layer).entries():
                merged[name] = value
        return merged

    def effect(self, ctx: Any, action: Callable[[Any], Callable[[], None]],
               label: str, notify: bool = True) -> Any:
        """把一次同步层变更挂到注册上下文（scope 可见性 + effect 归属）。

        变更原子进行并返回其同步撤销；变更抛错且层为新创建且空 → 回收该层。
        成功则 yield 撤销（撤销 = undo + 层空回收 + 通知）并在其后通知。
        返回 ctx.effect 的精确 disposer。
        """
        scope = scope_of(ctx)

        def body():
            if scope is None:
                layer = self.global_layer
                created = False
            else:
                existing = self._scoped.get(scope)
                if existing is None:
                    layer = self._create_layer(scope)
                    self._scoped[scope] = layer
                    created = True
                else:
                    layer = existing
                    created = False

            try:
                undo = action(layer)
            except Exception:
                if scope is not None and created and layer.isEmpty():
                    self._scoped.pop(scope, None)
                raise

            def disposer() -> None:
                undo()
                if scope is not None and layer.isEmpty():
                    self._scoped.pop(scope, None)
                if notify:
                    self._on_change()

            yield disposer
            if notify:
                self._on_change()

        return ctx.effect(body, label)