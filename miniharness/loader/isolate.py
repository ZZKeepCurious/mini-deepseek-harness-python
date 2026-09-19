"""条目级服务隔离与拦截（对齐 vendor/loader/src/config/isolate.ts）。

条目选项：
- `isolate: {name: true|label}`——为 name 服务在条目上下文挂一个隔离标签：
  `true` 用条目私有 LocalRealm，字符串标签用按标签共享的 GlobalRealm；
- `intercept: {name: config}`——为 name 服务在条目上下文挂 intercept 配置层
  （core/scope 的 `_resolve_intercept` 沿父链合并）。

装载期经 `loader/entry-init`（建立自身层）与 `loader/patch-context` waterfall
（计算标签、落后回调、按分隔标迁移服务实现、通知、清理）生效；条目局部拆解经
`loader/partial-dispose` 做 GlobalRealm 垃圾回收。
"""
from typing import Any


class Realm:
    """服务实现按 name 键控的标签仓库。"""

    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    def access(self, key: str, create: bool = False) -> object | None:
        label = self.store.get(key)
        if label is None and create:
            label = object()
            self.store[key] = label
        return label

    def delete(self, key: str) -> None:
        self.store.pop(key, None)

    @property
    def size(self) -> int:
        return len(self.store)


class LocalRealm(Realm):
    """条目私有 realm（`isolate: {name: true}`）。"""

    def __init__(self, entry: Any) -> None:
        super().__init__()
        self.entry = entry


class GlobalRealm(Realm):
    """按标签共享的 realm（`isolate: {name: '<label>'}`）。"""

    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label


def install_isolate(loader: Any) -> None:
    """在 loader 上下文挂 isolate/intercept 钩子（对齐上游 `ctx.plugin(isolate)`）。"""
    ctx = loader.ctx
    realms: dict[str, GlobalRealm] = {}
    delims: dict[str, object] = {}

    def access(entry: Any, name: str, create: bool = False) -> object | None:
        label = (entry.options.get("isolate") or {}).get(name)
        if not label:
            return None
        if label is True:
            realm = getattr(entry, "_realm", None)
            if realm is None:
                realm = LocalRealm(entry)
                entry._realm = realm
            return realm.access(name, create)
        realm = realms.get(label)
        if realm is None:
            if not create:
                return None
            realm = GlobalRealm(label)
            realms[label] = realm
        return realm.access(name, create)

    def delim_of(node: Any, delim: object) -> object | None:
        while node is not None:
            found = node._delims.get(delim)
            if found is not None:
                return found
            node = getattr(node, "parent", None)
        return None

    def on_patch_context(entry: Any, next_fn: Any) -> Any:
        isolate_opts = entry.options.get("isolate") or {}
        new_map = {name: access(entry, name, True) for name in isolate_opts}
        old_own = entry.ctx._isolate

        # step 2: 计算服务迁移 diff（旧标签 vs 新标签 + 分隔标对比）
        diff: dict[str, tuple] = {}
        for name in list(set(new_map) | set(delims)):
            old = old_own.get(name)
            new = new_map.get(name)
            if old is new:
                continue
            delim = delims.get(name)
            if delim is None:
                delim = object()
                delims[name] = delim
            flag1 = object()
            entry.ctx._delims[delim] = flag1
            for sym in (old, new):
                if sym is None:
                    continue
                impl = ctx.root._reflect_store.get(sym)
                if impl is None:
                    continue
                provider = impl.get("fiber")
                if provider is None:
                    logger = ctx.root.logger
                    if logger is not None:
                        logger.warn(RuntimeError(
                            f"expected service {name} to be implemented"))
                    continue
                flag2 = delim_of(provider.context, delim)
                diff[name] = (old, new, flag1, flag2)
                if flag1 is not flag2:
                    break

        # step 3: 挂自身层（父链仍可上溯）
        entry.ctx._isolate = dict(new_map)
        entry.ctx._intercept = dict(entry.options.get("intercept") or {})

        # step 4: 落后回调（fiber 重载）
        result = next_fn()

        # step 5: 迁移服务实现（同一提供者条目时旧标签 → 新标签）
        store = ctx.root._reflect_store
        for name, (s1, s2, f1, f2) in diff.items():
            if (f1 is f2 and s1 is not None and s2 is not None
                    and store.get(s1) is not None and store.get(s2) is None):
                store[s2] = store.pop(s1)

        # step 6: 通知依赖方（新旧标签两侧都重估）
        labels: list[tuple[str, object]] = []
        for name, (s1, s2, f1, f2) in diff.items():
            if s1 is not None:
                labels.append((name, s1))
            if s2 is not None:
                labels.append((name, s2))
        if labels:
            ctx.root._notify(labels)

        # step 7: 清理已不再 isolate 的分隔标
        for name in list(delims):
            if name not in new_map:
                entry.ctx._delims.pop(delims[name], None)

        return result

    def on_partial_dispose(payload: Any) -> None:
        entry, legacy, active = payload
        current = entry.options.get("isolate") or {}
        for name, label in (legacy.get("isolate") or {}).items():
            if label is True:
                continue
            if active and current.get(name) == label:
                continue
            realm = realms.get(label)
            if realm is None:
                continue
            referenced = False
            for other in loader.entries():
                if (other.options.get("isolate") or {}).get(name) == realm.label:
                    referenced = True
                    break
            if referenced:
                continue
            realm.delete(name)
            if not realm.size:
                realms.pop(realm.label, None)

    ctx.on("loader/patch-context", on_patch_context)
    ctx.on("loader/partial-dispose", on_partial_dispose)
