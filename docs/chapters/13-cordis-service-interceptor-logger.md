# 第 13 章：Cordis 进阶 —— Service 基类、intercept 与 LoggerService

> 对应 dsh 真实源码：`vendor/cordis/src/service.ts` + `context.ts` + `logger.ts`
> 前置：第 2 章（Context 服务仓库 + 事件总线 + 四种派发）。产出文件：`miniharness/core/scope.py`（`Service` / `LoggerService` / `Context.extend` / `Context.isolate` / `Context.intercept`）。

## 13.1 这一章要做什么

第 2 章把 `Context` 讲成"服务仓库 + 事件总线 + 副作用栈"三合一容器：服务就是往 `_reflect_store` 里塞一个值，靠 `provide`/`get` 按隔离标签存取。这对"一个函数、一张表、一个客户端"足够了，但 dsh 里大多数服务是有形状的：

- 它要在构造时自动登记、**随拥有 fiber 卸载自动注销**（你不该手动记得去 dispose 它）；
- 它可能是**可调用的**（`ctx.logger(name)` 返回一个具名 Logger，而不是 `ctx.logger` 自己就是 Logger）；
- 它可能有**可用性谓词**（某些服务只在特定条件下才"存在"）；
- 它可能携带**每插件可覆写的配置**（同一服务在不同作用域下读到不同参数）。

这些约定如果每个服务自己手写一遍，既重复又容易漂移。Cordis 用两样东西把它们收口：

> **(1) `Service` 基类**：服务的"构造即注册、可调用、可配置"模板，对齐 `vendor/cordis/src/service.ts`。子类几乎只填 `_invoke` / `_check` / `_init`，其余由基类兜底。
>
> **(2) `intercept` / `extend` / `isolate` 三兄弟**：不改父上下文、只给孩子上下文叠一层"额外配置 / 自有属性 / 隔离标签"的子上下文工厂（对齐 `vendor/cordis/src/context.ts`）。其中 `intercept` 专门给服务注入 per-plugin 配置，是 2.4 讲的"`_resolve_config` 沿祖先链合并"那套机制的入口。

本章把这三块讲透。它们已经在 mini 里全量对齐，但此前只在 `architecture.md` 的映射行登记，没有像第 2 章那样逐机制解读——本章补上。

## 13.2 概念：服务为什么需要基类

裸 `provide` 只解决"值存在哪、怎么取"。服务想要的更多：

| 诉求 | 裸 `provide` | `Service` 基类 |
|---|---|---|
| 构造时登记、随 fiber 注销 | 需手动 `effect(disposer)` | 构造即 `provide`，fiber 拆解自动收回 |
| 可调用（如 `ctx.logger(name)`） | 值本身得是 callable | 定义 `_invoke` 即可，基类 `__call__` 转发 |
| 可用性谓词（条件存在） | 无 | 定义 `_check`，透传给 `provide` |
| 每插件可覆写配置 | 无 | `intercept` 注入 + `_resolve_config` 合并 |

核心洞察：**服务是"带生命周期的对象"，不是"一个值"**。`Service` 基类把"生命周期"从每个服务里抽出来，统一交给 fiber——你写服务只关心行为，注册/注销交给 `super().__init__`。

## 13.3 代码 step-by-step

### 步骤 1：`Service` 基类 —— 构造即登记

```python
class Service:
    provide: str | None = None          # 缺省服务名（name 缺省时取用）

    def __init__(self, ctx, name=None):
        name = name or type(self).provide
        if name is None:
            raise TypeError("service must declare a name")
        self.ctx = ctx
        self.name = name
        ctx.provide(name, self, getattr(self, "_check", None))
        init = getattr(self, "_init", None)
        if callable(init):
            init()
```

对照上游 `service.ts:42-59`：`constructor(ctx, name)` 里 `self.ctx.reflect.provide(name, self, this[Service.check])`。差异只在 Python 把 `symbols.check` 写成普通属性 `_check`——语义一致：子类可定义 `_check` 作为可用性谓词，透传给 `provide`。

两个子类约定：

- **`_invoke`**：定义后实例可调用。基类 `__call__` 直接转发：

```python
def __call__(self, *args, **kwargs):
    invoke = getattr(self, "_invoke", None)
    if invoke is None:
        raise TypeError(f"service {self.name!r} is not callable")
    return invoke(*args, **kwargs)
```

这解释了为什么 `ctx.logger`（一个 `LoggerService` 实例）既能被 `ctx.logger(name)` 调用返回具名 Logger，又能直接 `ctx.logger.warn(...)`——后者是 `_invoke` 默认产出当前 fiber 名的 Logger 后再调 `.warn`（见步骤 3）。

- **`_init`**：构造后运行（上游 `symbols.init`，类插件场景）。mini 在 `__init__` 末尾 `init()` 调用，对齐"构造后"语义。

为什么"构造即注册"而不是"调用方负责注册"？因为服务实例一旦被创建，它的存在就该立刻对依赖它的 fiber 可见（触发 epoch 重载，见 2.5 步骤 5）。如果交给调用方手动 `provide`，很容易漏注册或重复注册——基类把这条路径焊死。

### 步骤 2：`_resolve_config` —— 沿 intercept 链合并配置

服务常常需要"可覆写配置"。Cordis 的做法是：配置不放在服务自己身上，而是散布在**祖先上下文的 `intercept` 表里**，服务在被调用时现合并。

```python
def _resolve_config(self, base=None, head=None, ctx=None):
    ctx = ctx or self.ctx
    configs = list(ctx._resolve_intercept(self.name))   # 祖先链，近根者优先
    if base is not None:
        configs.insert(0, base)
    if head is not None:
        configs.append(head)
    merged = {}
    for config in configs:
        if config:
            merged.update(config)
    return merged
```

`_resolve_intercept`（在 `Context` 上）沿 parent 链收集 `name` 的 intercept 条目，近根者优先（对齐 `service.ts:86-102` 的 prototype 链 `unshift` 走查）：

```python
def _resolve_intercept(self, name):
    nodes, node = [], self
    while node is not None:
        nodes.append(node)
        node = node.parent
    configs = []
    for node in reversed(nodes):              # 近根者先
        config = getattr(node, "_intercept", {})
        if config and name in config:
            configs.append(config[name])
    return configs
```

合并顺序是**近根者优先、base 最前、head 最后**（上游同款：祖先条目先 `unshift`，base 前置、head 后置）。这意味着"越靠近被调用点的 intercept 越有话语权"——父级给默认，子级能覆盖。

### 步骤 3：`LoggerService` —— 内置可调用的日志服务

`LoggerService` 是 `Service` 基类的标准样板，也是理解 `_invoke` / `intercept` / exporter 三件事的最佳实例（上游 `logger.ts:194-270`）。

```python
class LoggerService(Service):
    provide = "logger"
    buffer_size = 1000

    def __init__(self, ctx):
        self.buffer = []
        self.exporters = {}
        self._sn_message = 0
        self._sn_exporter = 0
        super().__init__(ctx, "logger")                 # 构造即登记 "logger"
        self.exporter({"colors": 3, "export": self._buffer_append})  # 默认缓冲导出器

    def _invoke(self, name=None, ctx=None):
        ctx = ctx or self.ctx
        config = self._resolve_config(ctx=ctx)          # 解析该上下文的 intercept 配置
        fiber = ctx.fiber
        name = name or config.get("name") or _hyphenate(fiber.name)
        return Logger({"name": name, "level": config.get("level"),
                       "meta": {"fiber": fiber}}, self)
```

要点：

1. **可调用**：`ctx.get("logger")("agent")` 经 `_invoke` 铸一个具名 `Logger`；`ctx.get("logger").warn(...)` 则 `_invoke` 用当前 fiber 名（hyphenate）铸 Logger 后调 `.warn`。
2. **配置来自 intercept**：`name` / `level` 不在服务上写死，而是从 `_resolve_config(ctx=ctx)` 解析——所以不同作用域下 `ctx.logger("x")` 能读出不同级别（上游 `logger.ts:251-261` 同款）。
3. **exporter 注册即 effect**：`exporter()` 经 `ctx.effect` 登记，随 fiber 注销自动移除（对齐 `logger.ts:232-237`）。默认导出器把消息压进 `buffer`（环形，超 `buffer_size` 截断），这是其它导出器（文件、stdout、测试捕获器）之外的兜底。

`Logger` 门面本身把 `error/info/warn/debug` 铸成方法，内部构造一条 `Message`（`sn`/`ts`/`name`/`type`/`level`/`args`/`fiber`），遍历所有 exporter，按 `exporter.levels[name] ?? levels.default ?? self.level ?? INFO` 过滤后 `export`（对齐 `logger.ts:141-161`）。格式化走 printf 风格占位符 `%s %d %f %o %c %%`（上游 `defaultFormatters`），`%o` 走 `JSON.stringify`，Error 自动展开栈、AggregateError 递归展开 `errors`——这些在 mini 里以等价 Python 实现。

4. **`ctx.logger` 是绑定视图，不是裸服务**。上游 `ctx.logger` 经 traceable 代理：调用 `_invoke` 时以**访问方** ctx 解析 intercept（而非服务构造时的根 ctx）。mini 用 `_LoggerView` 显式承载同一语义：

```python
class _LoggerView:
    def __init__(self, service, ctx):
        self._service, self._ctx = service, ctx
    def __call__(self, name=None):
        return self._service._invoke(name, self._ctx)   # 以访问方 ctx 解析配置
    def warn(self, *a):
        self().warn(*a)
```

这解释了为什么第 9 章"Agent 干预面"里 `agent.ctx.logger("steer")` 记出的名字是 `agent` 作用域的，而不是根——配置解析跟着"谁在调用"走。

### 步骤 4：`extend` / `isolate` / `intercept` 三兄弟

三者都是"不改父、给孩子叠一层"的子上下文工厂（上游 `context.ts:99-145`），区别在于叠什么：

| 方法 | 叠的东西 | 用途 |
|---|---|---|
| `extend(meta)` | 自有属性（含 symbol 键） | 共享父 fiber 的裸子上下文，携带额外属性（第 4 章 scope 链） |
| `isolate(name, label)` | `name` 的新隔离标签 | 让 `name` 服务在子上下文解析到不同标签（per-agent 一套）；同 label 传两次 = 共享作用域（第 3 章工具隔离、continuation 子代理） |
| `intercept(name, config)` | `name` 的一条 intercept 配置 | 其下装载的插件看到 `config` 合并进该服务的 resolved config（近根优先） |

mini 的 `extend` 是父链近似（非 JS 原型链），`isolate`/`intercept` 各自在子上下文写 `_isolate`/`_intercept` 遮蔽层，`_label_of` / `_resolve_intercept` 沿父链上溯解析——与上游 `Object.create` 原型继承等价。三者都不修改父上下文，所以"父不受影响、子可覆盖"的组合语义是可靠的。

### 步骤 5：组装一个可配置服务（示例）

把上面三块拼起来：定义一个带 `_invoke` + `_check` + intercept 配置的服务。

```python
class Greeter(Service):
    provide = "greeter"
    def _check(self):                       # 可用性谓词：config 缺 name 时不提供
        return True
    def _invoke(self, who="world"):
        cfg = self._resolve_config()
        prefix = cfg.get("prefix", "hi")
        return f"{prefix} {who}"

root = Context()
ctx_a = root.intercept("greeter", {"prefix": "hello"})
assert root.get("greeter")("x") == "hi x"        # 默认 prefix
assert ctx_a.get("greeter")("x") == "hello x"    # 子上下文 intercept 覆盖
```

`ctx_a` 下的 `greeter` 调用读到 `prefix=hello`，根下读到 `hi`——同一服务、不同作用域、不同配置，不靠任何全局变量。

## 13.4 验收：硬性规定

本章机制在 `tests/test_bus.py` / `tests/test_fiber.py` 中逐条有对应断言：

1. `Service` 子类构造即 `provide`，同名在已注册标签下冲突（fail loud）；fiber 卸载自动收回（不再可见）。
2. 定义 `_invoke` 的服务实例可调用；未定义则 `__call__` 抛 `TypeError`。
3. `_resolve_config` 沿祖先链合并，近根优先、`base` 前置、`head` 后置；`_resolve_intercept` 只收集命中 `name` 的条目。
4. `LoggerService`：`ctx.logger(name)` 铸具名 Logger；`ctx.logger.warn(...)` 以当前 fiber 名记录；exporter 注册即 effect、随 fiber 注销；默认缓冲导出器兜底。
5. `_LoggerView`：`ctx.logger` 以访问方 ctx 解析 intercept（不是服务构造时的根 ctx）。
6. `extend` / `isolate` / `intercept` 都不修改父上下文；`isolate` 同 label 两调用共享作用域。

```bash
python -m unittest tests.test_bus -v
```

## 13.5 检查点练习

1. **写一个有 `_check` 的服务**：定义 `FeatureGate(Service)`，只在 `ctx` 的某 intercept 配置 `enabled=True` 时通过 `_check` 返回真；断言关闭时 `ctx.get("feature")` 严格模式下返回 `None`（服务"不存在"），开启后返回实例。
2. **intercepts 链覆盖**：根 `intercept("svc", {"a": 1})`，子 `intercept("svc", {"a": 2, "b": 3})`，断言子下 `_resolve_config` 得到 `{"a": 2, "b": 3}`（近调用点优先）、根下得到 `{"a": 1}`。
3. **Logger 视图**：在子上下文 `ctx_b.intercept("logger", {"name": "custom"})` 后，`ctx_b.logger()` 产出的 Logger 名字为 `custom` 而非 fiber 名——验证配置解析随访问方走。

## 13.6 回到 dsh：真实源码对照

打开 `deepseek-harness/vendor/cordis/src`：

- `service.ts:11-114`：`Service` 抽象基类——`constructor` 即 `reflect.provide`、静态 `init/check/config/invoke/extend/resolveConfig` symbol 键、`[symbols.resolveConfig]` 沿 prototype 链合并、`createCallable` 让带 `_invoke` 的实例可调用。
- `context.ts:99-145`：`extend`（原型继承 + 自有属性遮蔽）、`isolate`（独立服务作用域标签）、`intercept`（叠加一条服务配置，不修改父）。
- `logger.ts:194-270`：`LoggerService` 可调用服务 + 默认缓冲 exporter；`Logger` 门面（printf 格式化、级别过滤、`error/cause`/`AggregateError` 展开）；`_resolveConfig` 从 `ctx[symbols.intercept]` 走查；`ctx.logger` 经 traceable 代理以访问方 ctx 解析。
- `reflect.ts`：`provide`/`get`/`set`/`isolate` 仓库与通知，配合上文 `_resolve_intercept`。

## 13.7 收尾

这一章的四个字可以带走：**服务即对象**。Service 把"构造即注册、可调用、可配置"从每个服务里抽出来焊死，`intercept`/`isolate`/`extend` 提供"不改父、只改子"的组合手段。下一章（14）把作用域从"隔离标签"再推进一层：dsh 专属的 `scope_key` 身份键与 `scopeTarget` 载波派发模型——它用一条 parent 关系同时驱动"注册向下继承"和"事件向上接纳"两个方向，是 agent 组合、会话 owner 路由、agent/* 事件隔离的根基。
