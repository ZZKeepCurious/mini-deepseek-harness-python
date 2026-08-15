# 从 0 到 1 实现 DeepSeek Harness 核心系统

> 引导式 step-by-step 教程手册：每章 = 概念讲解 → 最小可运行 Python 代码 → 逐段解释 → 硬性规定/测试验证 → 检查点练习。
> 配套文档：体系化分析报告（`report/index.html` 阅读地图 + 六个主题子页）——本手册是"施工图纸"，报告是"地图"。

## 这是什么

本手册用**纯 Python（标准库 only，零第三方依赖）**从零实现一个 `MiniHarness`：一个最小可用的 Agent 运行时核心子集，逐条复现 DeepSeek Harness（`dsh`）的**约定与硬性规定**。

复现目标不是逐行移植 TypeScript，而是**掌握约定**：

| 保留（技术核心本身） | 跳过（非核心） |
|---|---|
| 事件溯源会话 + `deriveMessages` 投影 | 声明合并（`declare module`） |
| 插件可逆副作用 + waterfall 短路 | 双面打包 / 生成器门禁 |
| 能力扩展口三角色 | HMR 热重载 |
| 作用域化注册 + 工具管线 | typert 类型图 |
| turn/step 状态机 + LLM 流式协议 | Web 客户端 / UI 卡片 |

## 学习地图

```mermaid
flowchart LR
  A["00 环境准备"] --> B["01 事件溯源会话&lt;br/&gt;（整个框架的地基）"]
  B --> C["02 插件上下文 + 事件总线"]
  C --> D["03 工具执行管线"]
  D --> E["04 Agent Loop + LLM 流式"]
  E --> F["05 持久化 + 崩溃恢复 + 组合"]
  F --> G["06 进阶扩展口（选做）"]
  F --> H["07 外部入口：表面 + 协议（选做）"]
  H --> I["08 组合层深读（vendor/cordis loader + 配置树 + preset）"]
  H --> J["09 Agent 干预面（steer/cancel/approval/quiescence）"]
  H --> K["10 轨迹投影引擎（Trajectory 折叠）"]
  H --> L["11 运行时自我修改（动态插件）"]
```

| 章节 | 内容 | 对应 dsh 真实源码 | 预计 |
|---|---|---|---|
| [00 环境准备](chapters/00-setup.md) | 运行环境、包结构、跑通测试 | — | 1 小时 |
| [01 事件溯源会话](chapters/01-event-sourced-session.md) | `Session` 追加式日志、seq 连续、deep-freeze、`derive_messages` 投影、崩溃修复 | `packages/core/session` | 2-3 天 |
| [02 插件上下文 + 事件总线](chapters/02-plugin-context-and-event-bus.md) | `Context` 服务仓库、四种派发（emit/waterfall/parallel/serial）、可逆副作用、作用域、依赖驱动激活 | `vendor/cordis` + `core/scope` | 2 天 |
| [03 工具执行管线](chapters/03-tool-execution-pipeline.md) | 作用域化注册表、schema 校验、pre/execute/post waterfall、超时、规范化 | `packages/core/tools` | 2 天 |
| [04 Agent Loop + LLM 流式](chapters/04-agent-loop-and-llm-streaming.md) | turn/step 状态机、inbox、StreamChunk 协议、DeepSeek SSE 适配器、模型请求重试/退避（§4.10：retry-policy + llm-retry + agent/request-error） | `core/agent-loop` + `llm/llm` + `llm/llm-deepseek` + `llm/llm-retry` | 3 天 |
| [05 持久化 + 崩溃恢复 + 组合](chapters/05-persistence-recovery-composition.md) | JSONL/SQLite 双后端、flush 栅栏、fail-closed、interrupted 修复、boot + patch | `session/session-persistence` + `boot` | 2 天 |
| [06 进阶扩展口](chapters/06-advanced-seams.md) | 沙箱 / 凭据 / 子 agent —— "换 Provider 不改 Consumer"；§6.9 进阶实现（真沙箱后端 / 凭据四层 / 远程三通道） | `capability-seams` 各页 | 2-3 天 |
| [07 外部入口](chapters/07-external-entry-points.md) | 两个产品表面（web/headless）+ 三个协议入口（ACP/SDK/hooks）、headless 深读与复现、JSON-RPC 信封子集（§7.6）、ACP 最小子集（§7.7）与 hooks 桥（§7.8）复现 | `apps/cli` + `bundle/{headless,web-app}` + `{acp,sdk,hooks}` | 2-3 天 |
| [08 组合层深读](chapters/08-composition-layer.md) | vendor/cordis loader、host/agent plane、isolate realm、配置树三层归属、preset 四模式；mini：preset roster + 挂载视图 | `vendor/{cordis,loader}` + `packages/preset` + `apps/cli/config/agent-presets` | 3-4 天 |
| [09 Agent 干预面](chapters/09-agent-intervention.md) | steer/inject/cancel/whenIdle/runMaintenance、pre-step/request 瀑布、quiescence 语义、审批能力 seam（ask/never + 审计对）；mini：loop 干预面五方法 + approval.py | `core/agent` + `core/agent-loop` + `interaction/user-approval` | 2-3 天 |
| [10 轨迹投影引擎](chapters/10-trajectory-projection.md) | 折叠定义 → TrajectorySnapshot，Python 复现；headless summarize 的演进 | `packages/client/ui-trajectory` | 2-3 天 |
| [11 运行时自我修改](chapters/11-runtime-self-modification.md) | extensions 七工具、进程内存动态插件生命周期（define/run/stop） | `packages/extensions/*` | 2 天 |
| [12 异步化与并行工具](chapters/12-async-parallel-tools.md) | asyncio 事件总线、并行调度器（屏障/滚动池/模型序提交/取消排干）、`is_concurrency_safe` 分类 | `core/agent-loop` 并行编排 + `core/context` 并发模型 | 2-3 天 |

## 快速开始

```bash
# 在仓库根目录执行（代码包 miniharness/ 与测试 tests/ 都在根下）

# 1. 跑全部测试（Python 3.10+，只需标准库）
python -m unittest discover -s tests -t .

# 2. 体验一个真实回合（无需 API key，用内置假模型）
python -m miniharness.demo
```

> 每章开头都有"先动手再读解释"的最小代码；读完一章就运行一次该章测试，感受硬性规定被测试钉住的感觉。

## 手册与报告的关系

- **报告**（HTML 体系）回答"系统长什么样、为什么这样设计"：分层架构、ctx 服务地图、技术核心、关键流程、**产品面九大议题**（模式/preset、外部入口、Trajectory、干预面、审批、自我修改、resume、plan/goal、压缩与后台）——全部配 Mermaid 图，按主题分页。
- **本手册**回答"系统怎么从零长出来"：一章一个主题，代码逐步构建，测试即硬性规定。
- 报告 `index.html` §B 的四维对照表（上游包 ↔ mini 模块 ↔ 手册章节 ↔ 报告页面）是解读完整性的检查清单。
- 报告第二部分 §7.3 的 5 个复现项目清单，就是本手册 01~05 章的骨架索引。

## 验收总目标

完成后你应当能：

1. 用一句话讲清"模型可见 ⟺ 已记录"为什么是事件溯源的根本约束；
2. 手绘 turn/step 时序，解释 reject 分支为何留下持久化记录；
3. 解释 waterfall 的 `next()` 短路语义，并说出四种派发模式各自适用场景；
4. 解释能力扩展口三角色为什么缺一不可；
5. 跑通一个"文本 + 一个工具 + 会话持久化 + 崩溃恢复"的端到端 demo（第 05 章验收）。