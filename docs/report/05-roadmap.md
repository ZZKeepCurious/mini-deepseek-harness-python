# 05 · 从 0 到吃透的路线图与 Python 复现清单

<p class="lead">分阶段学习路线、概念映射表与迷你复现项目清单——这是把读到的理解变成亲手代码的施工图。</p>

<div class="pill-row">
  <span class="tag t-blue">everything is a plugin</span>
  <span class="tag t-blue">event-sourcing</span>
  <span class="tag t-blue">capability seams</span>
  <span class="tag t-green">TypeScript strict</span>
  <span class="tag t-green">Cordis vendored</span>
  <span class="tag t-amber">Python SDK 存在</span>
</div>

## 6. 从 0 到掌握的完整路线图

下面把学习分为六个阶段。每一阶段都给出：目标、必读文档、必做练习、验收标准。建议用**笔记 + 一个"复现仓库"**贯穿始终（见第 7 章）。

| 阶段 | 目标 | 必读 | 必做 | 验收 |
|---|---|---|---|---|
| **P0 前置知识** | 补齐语言与协议基础 | TypeScript strict 基础；Python typing（Protocol/TypedDict/NewType）；JSON Schema；SSE；异步编程；对话 API 概念（role、tool calling、streaming） | 手写 10 行 SSE 解析器；用 curl 调一次 DeepSeek API 的流式 chat | 能口述"一次流式工具调用回合"的 wire 形状 |
| **P1 建立图景** | 知道系统有哪些部件、如何连接 | README.md → docs/architecture.md → packages/README.md → docs/cordis-primer.md | 画出你自己的分层图 + ctx 服务地图 | 能讲清"为什么替换 Provider 能换掉整个产品" |
| **P2 跑起来** | 可运行、可观察 | docs/development.md；apps/cli/README | `pnpm install` → `pnpm run build` → `pnpm dsh --profile headless "task"`；`pnpm dsh --profile web --dump-config` 观察组合树；`pnpm mock:llm` 无 key 跑通 | 能解释 `--dump-config` 输出的每一层来源 |
| **P3 核心循环** | 完全掌握 turn/step 状态机 | docs/subsystems/session.md、core.md；docs/agent-lifecycle.md | 读 `packages/core/agent-loop/src` 主 driver；在代码里为 6 个核心包各写一段职责注释 | 能手绘完整 turn/step 时序（含 reject、request-error、turn-stopping 分支） |
| **P4 事件溯源** | 掌握唯一数据源与持久化 | docs/persistence.md、docs/persistence-catalog.md；读 `packages/session/session-persistence` 双后端源码 | 手动构造一个 JSONL 会话文件并写脚本回放 `deriveMessages()` 的等价逻辑 | 能解释 `interrupted` turn 与 surface replace 的语义 |
| **P5 Cordis 深入** | 理解插件框架本体 | docs/cordis-tutorial/（动手教程）→ `vendor/cordis/src` 的 fiber/context/events；vendor/README.md 本地修改清单 | 读 18 项本地修改，挑 3 项（fiber 生命周期加固、include patch 语义、事务性协调）对应到测试文件 | 能解释"注册=可逆副作用"与 waterfall 短路语义 |
| **P6 动手写插件 + 横向扩展口** | 从读者变成作者 | docs/cookbook/adding-a-tool.md、adding-a-package.md、adding-an-llm-adapter.md | 写一个最小工具插件并在 preset 中挂载；用 `agent/pre-step` 拦截一次请求；用 `tools/pre-execute` 加权限策略 | 插件能被 `--dump-config` 显示且行为可测 |

!!! success "学习顺序建议"
    不要一上来读所有子系统页。按 **P1 → P3 → P4 → P5 → P2 → P6** 的顺序最有收益：先懂循环与日志（系统的"骨头"），再懂框架（"关节"），再动手跑（"肌肉"）。能力扩展口逐个横向扫读放在 P6 之后。

## 7. 用 Python 深刻掌握与复现技术核心

### 7.1 策略：三条路线并行

<div class="card" markdown>

1. **黑盒路线（官方 SDK，最省力）**：直接安装 `deepseek-harness-sdk`（PyPI，import `deepseek_harness`），用 Python 驱动真实 harness 子进程（stdio JSON-RPC），观察它如何编排 turn/step、工具、持久化。**这是理解"运行时约定"最便宜的方式。**
2. **白盒路线（读 TS，翻译成 Python）**：把第 6 章的阅读路径走完，对每个核心概念写一份"Python 对照笔记"（见 7.2 映射表）。目的不是照抄代码，而是**用 Python 重述约定**。
3. **复现路线（迷你 harness）**：用纯 Python 从零实现一个"最小可用的核心子集"（见 7.3）。这是唯一能证明你真正掌握的方式。

</div>

### 7.2 TS → Python 概念映射表

| dsh 的 TypeScript 概念 | Python 对应物 | 关键要点 |
|---|---|---|
| Service Definition / Provider / Consumer 扩展口 | `abc.ABC` + `Protocol`；Provider 注册在模块级注册表 | 接口要覆盖完整生命周期与错误码；Consumer 只依赖接口不依赖实现 |
| 声明合并（`declare module` 扩展 Map） | 注册表 dict + `Literal` 联合；`get_type_hints` 反射 | "Map → derived-union" 用 `Union[...]` 或 `TypeAlias` 表达；扩展 = 注册新键 |
| `Branded<B>` 品牌化 ID | `typing.NewType`（如 `SessionId = NewType("SessionId", str)`） | 编译期隔离；运行时就是 str |
| 可逆副作用（effects + disposer） | `contextlib.AbstractContextManager` 或 `__enter__/__exit__` | `register()` 返回 `Callback`；退出栈顺序回滚 |
| waterfall / emit / parallel / serial 事件 | 事件总线：四种 dispatch 策略（链式 next、广播、asyncio.gather、顺序 await） | waterfall 必须显式 `next()`；不调 next 即短路 |
| `SessionEventMap` 判别联合 | `TypedDict` + `Literal["turn/start", …]` + `switch/match` | 事件必须可无损 JSON 序列化；追加式日志 seq=len |
| `deriveMessages()` 投影 | 纯函数 `derive_messages(events) -> list[Message]` | surface 节点（append/replace）决定历史顺序；chunk 不投影 |
| 工具 schema DSL + JSON Schema 子集 | `jsonschema` 库 + 自写参数/输出校验器 | 输出必须有 canonical schema；参数先物化再冻结 |
| turn/step 状态机（agent-loop） | `asyncio.Task` 状态机（idle/running） | turn 打开于认领前；"零 step turn"也须持久化 |
| 流式 StreamChunk 协议 | dataclass + `AsyncIterator[StreamChunk]` | usage 在 finish 前；tool 参数保持原始 JSON 字符串 |
| 作用域化注册（per-agent ctx） | 每 agent 一个注册表实例，父子链式查找（继承/遮蔽） | 作用域注册卸载即回滚；restrict 过滤继承工具 |
| JSONL / SQLite 持久化 + 崩溃恢复 | `jsonl` 逐行 + `sqlite3`；启动扫描合成 `interrupted` 结束 | seq 连续；未知事件类型拒绝加载（fail-closed） |
| 组合层（bundle / patch） | YAML 配置 + 按 id 的整段覆盖 + insert 列表 | 补丁算法单一实现，导出纯函数，禁止复制粘贴 |

### 7.3 迷你复现项目清单（MiniHarness，用 Python）

!!! success "升级说明"
    本节已升级为独立的引导式手册：从 0 到 1 实现核心系统的完整 step-by-step 教程（每章 = 概念讲解 → 最小可运行 Python 代码 → 逐段解释 → 硬性规定/测试 → 检查点练习）位于仓库的 <span class="path">docs/chapters/</span>（索引见 <span class="path">docs/index.md</span> 首页章节表），配套可运行代码包 <span class="path">miniharness/</span>。本表是手册的骨架索引。

建议用 6 个递进的项目把核心吃透。每个项目都要**配测试 + 配 README**，最后能跑通一个"文本 + 一个工具 + 会话持久化"的端到端 demo。

<div class="grid2">

<div class="card" markdown>

**① 事件溯源会话（整个框架的地基）**

<span class="tag t-blue">必须</span> <span class="tag t-green">2-3 天</span>

- 实现 `Session`：append-only 事件日志、seq=len、deep-freeze（`types.MappingProxyType` / 自定义冻结）、无损 JSON 校验。
- 实现 `derive_messages()`：按 surface 节点投影；`surfaceOp=replace` 压缩。
- 实现 `turn/start … turn/end` 括号平衡硬性规定。

<p class="toc-hint">验收：给一组事件 + 一次压缩 replace，能输出与手算一致的模型历史。</p>

</div>

<div class="card" markdown>

**② 插件上下文 + 事件总线**

<span class="tag t-blue">必须</span> <span class="tag t-green">2 天</span>

- 实现 `Context.register/dispose`（可逆副作用，disposer 收集）。
- 实现四种派发：emit / waterfall(next 短路) / parallel / serial。
- 实现作用域：`create_scope` + 父子注册表查找 + 卸载回滚。

<p class="toc-hint">验收：写一个"权限策略"插件挂到 pre-execute waterfall，能拒绝并留痕。</p>

</div>

<div class="card" markdown>

**③ 工具执行管线**

<span class="tag t-blue">必须</span> <span class="tag t-green">2 天</span>

- 参数物化 + 冻结 → pre-execute（allow/deny/ask）→ 单调守卫 → execute（around 超时）→ post-execute → result 冻结通知。
- 工具 schema：`parameters`/`output` JSON Schema 校验；`is_concurrency_safe` 并行调度。

<p class="toc-hint">验收：错误/拒绝/超时三条路径都产生结构化结果且不中断回合。</p>

</div>

<div class="card" markdown>

**④ Agent Loop 状态机 + LLM 流式**

<span class="tag t-blue">必须</span> <span class="tag t-green">3 天</span>

- 实现 turn/step 状态机（idle/running、inbox claim、零 step turn）。
- 实现 `AsyncIterator[StreamChunk]` 适配器：SSE 解析 → chunk；`LlmFailure` 统一错误。
- DeepSeek 官方 chat API 的流式 + 工具调用回合跑通。

<p class="toc-hint">验收：输入一个提示 → 模型决定调工具 → 工具结果回灌 → 回合完成，全程记录会话日志。</p>

</div>

<div class="card" markdown>

**⑤ 持久化 + 崩溃恢复 + 组合加载**

<span class="tag t-amber">进阶</span> <span class="tag t-green">2 天</span>

- JSONL 与 SQLite 双后端；`flush` 批量写 + 崩溃合成 `interrupted` turn。
- YAML 配置 → 按 id 覆盖补丁 → 组装工具集与 Prompt 分节。
- 回放：重启后从日志重建历史并继续对话（resume）。

<p class="toc-hint">验收：kill 一个进行中的回合再重启，日志平衡、可继续。</p>

</div>

<div class="card" markdown>

**⑥ 进阶扩展口（任选一）**

<span class="tag t-amber">选做</span> <span class="tag t-rose">各 2-3 天</span>

- **子 agent**：注册表多 Provider + 可续接子会话。
- **沙箱**：subprocess 包裹 + 只读/写策略 + 失败即拒。
- **凭据扩展口**：配置只存引用，每次操作解析。
- **Web 工具**：search/fetch Provider + 工具 Consumer。

<p class="toc-hint">验收：换一个 Provider 不改 Consumer 即换行为。</p>

</div>

</div>

!!! warning "复现的取舍"
    复现目标是**掌握约定与硬性规定**，不是逐行移植。跳过：声明合并、双面打包、生成器门禁、HMR 热重载、typert 类型图、Web 客户端。保留：事件溯源、插件副作用、waterfall、作用域、工具管线、状态机、持久化恢复、配置组合——这些是"技术核心"本身。

## 8. 实操资源索引

### 常用命令

| 命令 | 作用 |
|---|---|
| `pnpm install` | 安装依赖（含 Lefthook 钩子与配对合并驱动） |
| `pnpm run build` | tsc 发 lib/types + tsdown 打包运行时 + Web 构建 |
| `pnpm run typecheck` / `lint` | 类型 / lint（先完成 Host lib 阶段） |
| `pnpm run test` / `test:coverage` | 单元测试 / CI 覆盖率门禁（每文件 100%） |
| `pnpm run test:e2e` / `test:snapshot` | 真实 API e2e（无 key 自跳过）/ 无 key 快照回放 |
| `pnpm dsh --profile web` / `headless` | 从源码启动 Web / 一次性任务（需要 DEEPSEEK_API_KEY） |
| `pnpm dsh --profile web --dump-config` | 打印实际组合树，观察 bundle/patch 层叠 |
| `pnpm mock:llm` | 本地 mock LLM 服务器，无 key 联调 |
| `pnpm run check:all` | 全部门禁本地执行 |
| `python -m pip install deepseek-harness-sdk` | 安装官方 Python SDK（PyPI） |

### 运行环境事实（上手必知）

| 事实 | 说明 |
|---|---|
| `DSH_HOME` / `~/.dsh` | Harness 家目录：`profiles/<name>`、home 级 `cordis.patch.yml`、home 级 `.env`、`profiles/node_modules`（裸插件名解析回退）都在这 |
| `DEEPSEEK_API_KEY` | 真实 DeepSeek 适配器与 demo 的凭据（也可放 gitignored 的根 `.env`）；无 key 时 e2e 自跳过 |
| `DEEPSEEK_BASE_URL` | 可选，覆盖官方 API 端点（`https://api.deepseek.com`） |
| `DSH_SESSION_ROOT` | 会话/持久化数据的根目录（Python SDK 与产品 CLI 共用） |
| `DSH_CORDIS_CONFIG` | Python SDK 注入默认组合配置的方式（指定自定义 cordis.yml 路径） |
| profile 模板 | `web` 与 `headless` 两个 profile 首次使用自动初始化；其它名字需 `initProfile` 显式创建 |

### 关键文档 → 源码对照（学习导航）

| 主题 | 文档 | 首选源码入口 |
|---|---|---|
| 架构总览 / 循环 | <span class="path">docs/architecture.md</span> | <span class="path">packages/core/agent-loop/src</span> |
| Cordis 原语 | <span class="path">docs/cordis-primer.md</span> | <span class="path">vendor/cordis/src/{context,fiber,events}.ts</span> |
| 会话事件溯源 | <span class="path">docs/subsystems/session.md</span> | <span class="path">packages/core/session/src</span> |
| 持久化 | <span class="path">docs/subsystems/persistence.md</span> | <span class="path">packages/session/session-persistence</span> |
| 工具管线 | <span class="path">docs/subsystems/tools.md</span> | <span class="path">packages/core/tools/src</span> |
| LLM 扩展口 | <span class="path">docs/subsystems/llm-streaming.md</span> | <span class="path">packages/llm/llm/src + llm-deepseek/src</span> |
| 时序图 | <span class="path">docs/agent-lifecycle.md</span> | <span class="path">docs/tool-execution-pipeline.md</span> |
| 能力扩展口全景 | <span class="path">docs/capability-seams.md</span>（docs/subsystems/ 各页） | <span class="path">packages/<group>/<pkg>/src</span> |
| 组合 / 启动 | <span class="path">packages/boot/app-boot/README.md</span> | <span class="path">apps/cli/src/{profile-boot,bin}.ts</span> |
| Python SDK | <span class="path">python/sdk/README.md</span> | <span class="path">python/sdk/src/deepseek_harness</span> |

### 学习检查清单

<ul class="checklist">
  <li>能画出 5 层架构与 ctx 服务地图</li>
  <li>能手绘 turn/step 完整时序（含 reject / request-error / turn-stopping 分支）</li>
  <li>能解释"模型可见 ⟺ 已记录"并知道为何新增模型可见输入要加 session 事件</li>
  <li>能解释 waterfall 的 `next()` 语义与四种派发模式</li>
  <li>能说清能力扩展口三角色为什么缺一不可</li>
  <li>能说清 JSONL/SQLite 双后端 + 崩溃恢复 `interrupted` turn</li>
  <li>能用 `--dump-config` 读懂实际组合树</li>
  <li>写过至少一个挂载进 preset 的最小插件</li>
  <li>用 Python 完成 ①–④ 迷你复现并跑通端到端回合</li>
  <li>用官方 Python SDK 驱动真实 harness 完成一次带工具调用 + 持久化的任务</li>
</ul>