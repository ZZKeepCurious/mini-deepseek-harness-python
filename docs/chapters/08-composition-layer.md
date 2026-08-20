# 08 组合层深读：配置树、loader 与 preset

> 本章回答一个问题：`agent.cordis.yml` 里那棵树是怎么长出来的，一个进程为什么能同时跑多个不同组成的 agent？这是前几章"内核"与第 07 章"入口"之间被跳过的中间层——**组合层**。我们会先拆上游的配置树三层归属与 loader 结算，然后在 mini 里实现一个最小的 preset roster（会话级 agent 组合的选择与挂载）。
>
> 对应 dsh 真实源码：`vendor/cordis`（loader/组合结算）+ `packages/preset`（per-session agent composition）+ `apps/cli/config/agent-presets/`（四种出厂 preset）。mini 复现了 roster + 挂载一条（`miniharness/preset/presets.py`）。

## 8.1 直觉：为什么要有组合层

前几章的内核只有"一个 agent 怎么跑"。但 dsh 的现实是：

- 一个进程要同时服务标准模式、极简模式等多个 agent，它们**工具目录不同、prompt 不同、还能各跑各的**；
- 工具的实现（bash、文件系统、web 搜索）是**进程级单例**，不应该每个会话复制一份；
- 所以必须把"有什么实现"（host plane）和"这个会话用哪些"（preset）**分层**——这正是 `packages/preset/README.md:5` 的一句话：一个 preset 就是"挂在一个 agent 作用域下的一棵小组合树"，让一个进程跑几个不同组成的 agent 而不打架。

## 8.2 上游机制：三层归属与 roster

### 8.2.1 组合树的三层归属

`agent.cordis.yml` 是 YAML 形式的 Cordis 组合描述，最终由 **loader** 结算成内存里的插件树。归属规则只有一条主线（`packages/preset/README.md:14`）：

> **跨会话设施是进程单例，留在 host 组合；preset 只携带"这一个 agent 贡献给它们的东西"。**

落到结构上：

| 层 | 是什么 | 谁能用 |
|---|---|---|
| host plane | 进程级组合（bundle 底座、注册表、tokenMeter、jobs 注册表等） | 所有 agent |
| agent plane | 每个 agent 会话挂载的组合（preset 贡献的工具选择、prompt） | 该 agent 自己 |
| `isolate` realm | 带 `isolate` 的 group，服务行必须挂进去才能隔离 | 该 realm 内 |

realm 规则的直接后果（`apps/cli/config/agent-presets/standard/agent.cordis.yml:11-18` 注释）：service 行不挂进 `isolate` realm 就会落到根 realm，第二个会话想再挂就冲突——所以**命名了进程级全局服务的行在挂载时被拒绝**，而不是让下个会话撞车。

### 8.2.2 loader：从 YAML 到插件树

`vendor/cordis` 的 loader（`vendor/cordis/loader` 与 `vendor/include`）做的事可以压缩成三条：

1. **include 展开**：组合里 `include: 'file.yml'` 的行先被替换成目标文件的内容（递归），这是"一个 preset 引用共享片段"的机制；
2. **插件加载**：每条 entry 的 `plugin` 字段导入真实模块，取回 `inject`/`apply` 元数据（`provides` 已废除——服务在 apply 期动态登记）；
3. **结算**：把整棵树交给插件管理器，按依赖激活——这正是 mini 第 2 章 `RegistryService` 做的事（`miniharness/core/scope.py`），只是上游还有 scope/fiber/carrier 的完整实现。

### 8.2.3 preset roster：目录列表即名单

四种出厂 preset（`apps/cli/config/agent-presets/{standard,code,minimal,cordis}/`）的名单**不维护在代码里**——`packages/preset/README.md:12` 明说：预置名单就是那个目录的列举，目录里有什么就是什么。这避免了"代码里的名单"与"磁盘上的目录"两处漂移。

模式差异（前面报告 04 页议题 1 有完整版，这里只提组合视角）：

- **标准模式**：agent-plane 组合，每进程挂一次，会话经 scope parentage 加入；工具目录跨模式不变（为了请求缓存稳定）；
- **极简模式**：persona `complete: true`（系统提示即全部上下文）+ 双工具（bash + str_replace_editor），无运行时上下文、无压缩——"只换工具集就得到全新产品形态"的示范。

## 8.3 mini 复现：preset roster 与挂载

mini 组合层支持 YAML（`boot/composition.py` 的 pyyaml 可选依赖，缺省退化 JSON）；preset 清单用 JSON 承载（载体简化，契约对齐）。目录结构：

```
miniharness/preset/
├── standard/preset.json    # 标准模式：8 工具 + 运行时上下文
└── minimal/preset.json     # 极简模式：2 工具 + fixed-prompt（complete: true）
```

### 8.3.1 领域对象（`miniharness/preset/presets.py`）

```python
@dataclass(frozen=True)
class PersonaConfig:
    complete: bool = False                       # True = 系统提示即全部上下文
    include_runtime_context: bool = True
    system_prompt: str | None = None

@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    description: str
    order: int
    tools: list[str] = field(default_factory=list)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    provides: list[str] = field(default_factory=list)   # 进程级服务声明（默认空）
```

### 8.3.2 roster：目录列表即名单

```python
class PresetRoster:
    def _scan(self) -> dict[str, Preset]:
        presets: dict[str, Preset] = {}
        for child in sorted(self.root.iterdir()):
            if not child.is_dir():
                continue
            if not (child / "preset.json").is_file():
                continue
            p = load_preset(child)
            if p.id in presets:
                raise RuntimeError(f"roster 发现重复 preset id: {p.id}")
            presets[p.id] = p
        return presets
```

`ids()` 按 `order` 排序返回名单；`resolve(preset_id)` 未知 id → `KeyError`（fail loud）。新增一个 preset = 新建一个目录，roster 代码一行不改。

### 8.3.3 挂载：只在 agent 作用域开视图

`Preset.mount` 是把"host 实现"与"会话选择"粘起来的关键，三条不变量逐条执行：

```python
def mount(self, ctx, agent_scope, host_tools) -> ToolRegistry:
    missing = [t for t in self.tools if host_tools.resolve(t) is None]
    if missing:
        raise RuntimeError(f"preset {self.id} 声明了 host 未提供的工具: {', '.join(missing)}")
    for key in self.provides:
        if ctx.get(key) is not None:
            raise RuntimeError(
                f"preset {self.id} 声明进程级服务 {key}，但 host 已提供（拒绝挂载）")
    view = ToolRegistry(agent_scope)
    for name in self.tools:
        view.register(host_tools.resolve(name), scope=agent_scope)
    return view
```

- **不造实现**：工具本体留在 host 的 `ToolRegistry`，preset 只把引用注册到 agent 作用域（`register(tool, scope=agent_scope)`，复用第 3 章的作用域化注册表）；
- **host 缺工具 → fail loud**：preset 声明了 host 没有的东西，就是组合坏了，立刻报错；
- **进程级冲突 → 拒绝挂载**：preset 声明 `provides` 命中 host 已有服务时拒绝——这就是上游"命名进程级全局服务的行在挂载时被拒绝"的 mini 版。

### 8.3.4 与 loop 对接

挂载返回的 `ToolRegistry` 视图可直接喂给 `AgentLoop`：

```python
roster = builtin_roster()
agent_scope = host.create_scope("agent:1")
view = roster.resolve("minimal").mount(host, agent_scope, registry)
loop = AgentLoop(session, adapter, view, ctx,
                 system_prompt=roster.resolve("minimal").persona.system_prompt)
```

两个 preset 用同一个 host 注册表、两个 agent 作用域——一个进程同时跑标准与极简 agent 的形态就成立了。

## 8.4 硬性规定（被测试钉住）

1. **roster = 目录列表**：发现 = 扫描目录，新增 preset 不碰代码；重复 id fail loud。
2. **挂载只开视图**：host 注册表不变，agent 作用域只看到 preset 声明的工具。
3. **host 缺工具 fail loud**：preset 声明了 host 没有的工具 → `RuntimeError`。
4. **进程级冲突拒绝挂载**：`provides` 命中 host 已有服务 → `RuntimeError`，而不是覆盖。
5. **未知 preset fail loud**：`resolve` 未知名 → `KeyError`。

验证：`python -m unittest tests.test_presets -v`（9 个用例，全部通过）。

## 8.5 检查点

- [ ] 说出组合树三层归属，并解释"进程级服务挂载时被拒绝"为什么比"下个会话撞车"好；
- [ ] 给 `PresetRoster` 新增一个 preset 目录，`ids()` 自动包含它（一行代码不改）；
- [ ] 手动构造一个 `provides` 与 host 冲突的 preset，观察挂载被拒绝且 host 服务未被覆盖；
- [ ] 说出 mini 相对上游的载体简化（YAML→JSON）与语义对齐（组合选择语义不变）。

> 下一章：Agent 干预面——宿主怎么唤醒、转向、取消一个运行中的 agent（steer/inject/cancel/whenIdle）。