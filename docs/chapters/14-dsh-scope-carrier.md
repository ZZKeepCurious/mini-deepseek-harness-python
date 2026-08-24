# 第 14 章：dsh_scope 载波派发模型

> 对应 dsh 真实源码：`packages/core/scope/src/index.ts` + `store.ts`
> 前置：第 2 章（事件总线四种派发、作用域）、第 13 章（Service / intercept / isolate）。产出文件：`miniharness/core/dsh_scope.py`；消费方 `core/scope.py`（事件派发 `this_arg=` 载波参数）、`core/session_store.py`（owner 路由）、`core/tools.py`（ScopedLayers 注册表）。

## 14.1 这一章要做什么

第 2 章的 `isolate` 解决"同一个服务名在不同作用域解析到不同实现"。但事件派发还想要一种**更精细的路由**：一个监听器只接收"某个具体身份（agent / session / tool）"相关的事件，而不是整个作用域树。

dsh 用一个极小的原语解决这个问题：**不透明的 scope 键（`ScopeKey`）+ 一条 parent 关系 + 一个"仅用于路由"的事件载波（`scopeTarget`）**。这套机制叫 dsh_scope（上游 `@deepseek-ai/dsh-scope`），它已经被 mini 全量对齐，但此前只在 `architecture.md` 映射行登记，没有逐机制解读——本章补上。

核心洞察（也是整章的题眼）：

> **一条 `scopeParents` 关系同时驱动两个方向**：
> - **注册视图向下继承**——子 scope 经 `ScopedLayers` 看到祖先的层（`chainLayers` / `merge`，越近的 scope 条目越晚叠加、胜出）；
> - **事件接纳向上延伸**——挂了祖先 scope 标号的监听器，会收到派发到后裔 key 的事件（`scopeTarget`）。事件只沿链**向上**流，绝不向下。

一句话：**注册往子树看，事件往祖先看。**

## 14.2 概念：ScopeKey 与载波

| 概念 | 作用 |
|---|---|
| `ScopeKey` | 不透明、按身份比较（Python 里是最小可弱引用类，因为裸 `object()` 不可弱引用）的身份键 |
| `scopeParents` | 一个 `WeakMap<key, parentKey>`，记录谁是谁的 enclosing scope；一条关系 |
| `scopeTarget(base, key)` | 铸一个"仅用于路由"的接收器（载波）：它不暴露 `base` 的属性，只携带"路由键 = key" |
| 打标监听器 | 经 `this_arg=` 传一个载波的监听器，按"载波键或其祖先"过滤接纳 |
| 未打标监听器 | 全局接纳（不受 scope 约束） |

为什么需要"载波"而不是直接传 key 给 `emit`？因为 Cordis 的事件派发天然支持 `ctx.on(event, fn)` 注册一个监听器，监听器本身不带"我只关心哪个身份"的信息。dsh 的解法是：把"身份"焊进**接收器对象**（`scopeTarget` 的返回值），派发时检查监听器的接收器是否匹配当前事件的身份链——这样既有全局监听器、又有身份化监听器，且二者走同一条派发路径。

## 14.3 代码 step-by-step

### 步骤 1：`ScopeKey` 与 parent 关系（带环检测）

```python
class ScopeKey:
    __slots__ = ("__weakref__",)
    def __repr__(self):
        return f"<ScopeKey {id(self):x}>"

_scope_parents = weakref.WeakKeyDictionary()

def link_scope_parent(key, parent):
    cursor = parent
    while cursor is not None:
        if cursor is key:
            raise RuntimeError("dsh-scope: scope parent link would form a cycle")
        cursor = _scope_parents.get(cursor)
    _scope_parents[key] = parent

def bind_scope_parent(key, parent):
    if key in _scope_parents:
        raise RuntimeError("dsh-scope: scope key is already bound to a parent; "
                           "re-linking requires the binding returned by the original bind")
    link_scope_parent(key, parent)
    return ScopeParentBinding(key)     # 仅此持有者可 rebind

def scope_parent_of(key):
    return _scope_parents.get(key)

def scope_chain_of(key):               # nearest-first: [key, parent, ...]
    chain = []
    cursor = key
    while cursor is not None:
        chain.append(cursor)
        cursor = _scope_parents.get(cursor)
    return chain
```

对照上游 `index.ts:39-102`：`scopeParents` 是 `WeakMap<ScopeKey, ScopeKey>`；`linkScopeParent` 从 `parent` 沿父链走到根，撞上 `key` 即拒绝（每条链都走到根，确保无环）；`bindScopeParent` 已绑定键抛错，仅返回的 `ScopeParentBinding` 能 `rebind`——blank-session 重组契约由持有者保证。mini 用 `WeakKeyDictionary` 承载弱引用语义，`ScopeKey` 用最小类（裸 `object()` 不可弱引用）。

### 步骤 2：`create_scope` —— 铸一枚带身份的作用域

```python
def create_scope(ctx, key, parent=None, name="scope"):
    if parent is not None:
        bind_scope_parent(key, parent)
    fiber = ctx.plugin({"name": name, "apply": _scope_noop})   # 独立 fiber（对齐 ctx.plugin(scope)）
    scoped = fiber.context
    scoped._scope_key = key                                    # 把身份打在上下文上
    return Scope(scoped, fiber)
```

作用域上下文继承铸造方 fiber 的依赖 API，并**拥有**经它做出的每笔注册（fiber 拆解即逆序回滚，见第 2 章）。`Scope.dispose()` 记忆化 `quiesce_fiber`：先 `fiber.dispose()`，再跟随 `fiber.inertia` 排空（对齐上游 `quiesceFiber`：拆解后继续等异步拆解惯性，mini 按"每个新惯性对象各消费一次"逼近上游"完成后置 undefined"的语义）。

### 步骤 3：`scope_of` —— 读最近的身份标号

```python
def scope_of(ctx):
    node = ctx
    while node is not None:
        key = getattr(node, "_scope_key", None)
        if key is not None:
            return key
        node = getattr(node, "parent", None)
    return None
```

沿 parent 链向上找第一个带 `_scope_key` 的节点（上游 `ctx[kScope]` 的原型继承等价；`Scope` 包装经 `__getattr__` 代理亦可用）。无标号上下文返回 `None`——意味着"全局"。

### 步骤 4：`scope_target` 载波 —— 事件只向上接纳

```python
class _ScopeCarrier:
    __slots__ = ("base", "key", "base_filter", "__weakref__")
    def __init__(self, base, key):
        self.base = base
        self.key = key
        self.base_filter = getattr(base, "_context_filter", None)
    def admit(self, ctx):
        if self.base_filter is not None and not self.base_filter(ctx):
            return False
        tag = scope_of(ctx)
        if tag is None:
            return True                       # 未打标监听器：全局接纳
        cursor = self.key
        while cursor is not None:             # 载波键或其任一祖先 → 接纳
            if cursor is tag:
                return True
            cursor = scope_parent_of(cursor)
        return False                          # 低于派发键的标号：排除

def scope_target(base, key):
    carrier = _ScopeCarrier(base, key)
    _carrier_keys[carrier] = key
    return carrier
```

对照上游 `index.ts:170-185`：`scopeTarget(base, key)` 保留 `base` 自身 Cordis filter，再按 scope 图接纳——**未打标 → 全局；打标 → 键或键的祖先命中才接纳**。注释写得很直白："事件沿 scope 链向上流、绝不向下（a tag BELOW the dispatch key stays excluded）"。

这正是"注册向下、事件向上"的另一半：一个 standing 组合（如宿主 root）挂了祖先 scope 的监听器，就能收到其下每个 agent 的事件，而不必每个 agent 单独注册——这也是第 7/9 章里"一个会话店观察每个被组合 agent"的机制根基。

### 步骤 5：`ScopedLayers` —— 注册表的 scope 感知存储

`dsh_scope` 还提供注册表存储层（上游 `store.ts`），让"按 scope 归属 + 按 scope 继承"的注册表不用每次手写：

```python
class ScopedLayers:
    def __init__(self, create_layer, on_change):
        self.global_layer = create_layer(None)
        self._scoped = {}
    def chain_layers(self, scope):            # 现存覆盖，最远祖先在前、精确 scope 最后
        layers = []
        for key in reversed(scope_chain_of(scope)):
            layer = self._scoped.get(key)
            if layer is not None:
                layers.append(layer)
        return layers
    def merge(self, scope, pick):             # 全局 + 按序 scope 链遮蔽（近者胜）
        merged = dict(pick(self.global_layer).entries())
        for layer in self.chain_layers(scope):
            for name, value in pick(layer).entries():
                merged[name] = value
        return merged
    def effect(self, ctx, action, label, notify=True):
        scope = scope_of(ctx)                 # 可见性 + effect 归属都来自注册上下文
        ...
        return ctx.effect(body, label)
```

`NamedEntries`（插入序、命名、重复诊断、幂等撤销）与 `AnonymousEntries`（插入序、匿名、独立注册身份）是两层的具体存储。关键设计：**读从不创建 scope 层**（`peek` 链盲，只寻址某 scope 自身贡献；需要继承用 `chain_layers`）；注册把一次同步层变更挂到注册上下文（scope 可见性 + effect 归属），层空了才回收。第 3 章工具注册表（`core/tools.py`）正是用它实现"per-agent 工具隔离 + 全局工具兜底"。

## 14.4 mini 怎么用它：三处真实消费

1. **agent/* 事件载波路由**（`core/scope.py` 派发）：所有 `agent/*` 派发点带 `this_arg=scope_target(loop 自身 scope 键)`，未打标 root 监听器全收、打标监听器按"载波键或其祖先"接纳、兄弟作用域隔离。
2. **会话 owner 路由**（`core/session_store.py`）：`session/created|disposed|event|flush` 经 `scope_target(session, scope_of(owner_ctx or self.ctx))` 派发——owner 及其祖先收到、兄弟/后代隔离（对齐上游 `scopeTarget(session, scopeOf(store.ctx))`）。
3. **工具注册表 scope 视图**（`core/tools.py`）：`ScopedLayers` 让 `resolve`/缺省视角 = 注册表 root 的 scope 键，显式 scope 沿父链最近者胜 + 全局层兜底。

## 14.5 验收：硬性规定

`tests/test_dsh_scope.py` 逐条覆盖：

1. `bind_scope_parent` 已绑定键抛错；会成环的链接抛 `RuntimeError`；`scope_parent_of` / `scope_chain_of` 返回 nearest-first 链。
2. `create_scope` 铸出带 `_scope_key` 的上下文；`dispose` 记忆化且逆序回滚注册；`scope_of` 沿父链取最近标号、无标号返回 `None`。
3. `scope_target` 载波：`admit` 对未打标监听器全局接纳；对打标监听器仅在载波键或其祖先命中时接纳；低于派发键的标号排除（事件不向下）。
4. `is_scope_carrier` / `carrier_key_of` 正确识别载波与读键。
5. `ScopedLayers.merge` 近 scope 同名胜出；`effect` 注销回收空层；注册随 fiber 拆解自动移除（HMR 契约）。

```bash
python -m unittest tests.test_dsh_scope -v
```

## 14.6 检查点练习

1. **事件向上、不向下**：建 root → A（parent=root）→ B（parent=A）三个 scope 键；在 root 挂一个 `scope_target(root_key)` 打标监听器，向 B 派发事件，断言监听器收到；反向——在 B 挂 `scope_target(B_key)` 监听器，向 root 派发，断言**收不到**（事件不向下）。
2. **兄弟隔离**：A1、A2 同 parent root；在 A1 挂 `scope_target(A1_key)` 监听器，向 A2 派发，断言收不到（兄弟不互见）。
3. **ScopedLayers 继承**：全局层放 `{"x": g}`，A 层放 `{"x": a}`，断言 `merge(A_key)` 得 `{"x": a}`（近者胜）、`merge(root_key)` 得 `{"x": g}`。

## 14.7 回到 dsh：真实源码对照

打开 `deepseek-harness/packages/core/scope/src`：

- `index.ts:14-204`：`ScopeKey` 类型、`scopeParents` 关系、`bindScopeParent`/`scopeParentOf`/`scopeChainOf`（带环检测）、`createScope`（独立 fiber + `kScope` 打标）、`scopeOf`、`scopeTarget`（保留 base filter + 向上接纳）、`isScopeCarrier`/`carrierKeyOf`。
- `store.ts:30-267`：`NamedEntries` / `AnonymousEntries`（插入序、借值、幂等撤销、单层回收）、`ScopedLayers`（全局层 + 精确 scope 层、`chainLayers`/`merge`/`effect` 把层变更挂到注册上下文的 scope 可见性与 effect 归属）。

## 14.8 收尾

这一章的四个字可以带走：**身份即路由**。dsh_scope 用一条 parent 关系把"服务隔离标签"升级成"事件身份路由"：注册往子树看、事件往祖先看。它与第 2 章的 `isolate`、第 13 章的 `Service`/`intercept` 合在一起，构成了 dsh 插件体系的三层骨架——服务怎么找（reflect + isolate）、插件怎么协作（事件总线 + 载波）、服务怎么长（Service 基类 + intercept 配置）。到这里，Cordis 的核心架构在 mini 里已经全量对齐，且本教程第 2/13/14 章给出了逐机制解读。