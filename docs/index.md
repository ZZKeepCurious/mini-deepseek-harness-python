# Mini DeepSeek Harness（Python）— 文档入口

> 用 Python（成熟开源库优先，无语义等价库时才用标准库手写）清晰复现 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的核心约定，目标是可直接上生产。
> 仓库：https://github.com/ZZKeepCurious/mini-deepseek-harness-python

仓库根 [README.zh.md](https://github.com/ZZKeepCurious/mini-deepseek-harness-python/blob/main/README.zh.md) 有架构分层与常用命令；本页串联报告、手册与架构/wire 文档。

## 这是什么

本手册用 Python（成熟开源库优先：SSE 传输 `httpx`、凭据跨进程写锁 `filelock`、文件监视 `watchdog`、YAML 配置 `pyyaml`）逐层实现一个 `MiniHarness`：一个以清晰复现为核心、面向生产就绪的 Agent 运行时，逐条复现 DeepSeek Harness（`dsh`）的**约定与硬性规定**。

复现的目标是掌握约定，不逐行移植 TypeScript：

| 术语 | 含义 |
|---|---|
| **durable** | 事件已 append 进 Session 日志；模型可见历史由此投影，不另存副本 |
| **turn / step** | turn 是一次对话回合括号；step 是 turn 内一次模型请求（可含工具链） |
| **waterfall** | 插件链式决策：调用 `next()` 才继续下游；可短路 |
| **wire** | `web/` 与 `webui/` 之间的 HTTP/WS 与 Remote 帧约定（见 interface-wire） |
| **体裁** | 章节表列：**实现**（跟做内核）、**解读**（对照成品与上游）、**对照**（测与源码对齐） |

| 保留（技术 core） | 跳过（非核心） |
|---|---|
| 事件溯源会话 + `deriveMessages` 投影 | 声明合并（`declare module`） |
| 插件可逆副作用 + waterfall 短路 | 双面打包 / 生成器门禁 |
| 能力扩展口三角色 | HMR 热重载 |
| 作用域化注册 + 工具管线 | typert 类型图 |
| turn/step 状态机 + LLM 流式协议 | Web 客户端 / UI 卡片 |

## 运行时总览

`boot` / `loader` 装组合 → Session 只追加事件 → 投影与 Agent loop 读写同一条日志 → `web/` 发布 wire、`webui/` 只消费约定。功能模块如何挂在同一运行时上（Fig S1～S6、章节验收表）见 **[system-map.md](system-map.md)**；走读或空白复现见 **[guides/learning-paths.md](guides/learning-paths.md)**。

L0–L3 与 core / web / webui 边界见 [architecture.md](architecture.md) 第 3 节（Fig S2 摘要见 system-map）。

## 架构与 wire

- **[架构说明与上游对应](architecture.md)** —— `miniharness/` 目录如何镜像上游包族、模块映射表、依赖方向、公共 API 与教学扩展清单。
- **[接口约定参考](interface-wire.md)** —— 前后端耦合面：两信封 RPC、`/api/remote.mux`、Remote 帧、`$events`/`$events/result`、审批与用户提问 waterfall、静态承载、会话导出。unary 端点以 `miniharness/web/api.py` 的 `WebApi.ROUTES` 为准；`webui/` 只依赖这份约定。

## 分析报告

- **[报告首页](report/index.md)** —— 六个主题子页 + 四维对照表（上游包 ↔ mini 模块 ↔ 手册章节 ↔ 报告页面）。
  - [01 项目全景与分层架构](report/01-overview.md)
  - [02 系统架构与内核](report/02-architecture.md)
  - [03 关键处理流程](report/03-flows.md)
  - [04 产品面全解读](report/04-product-surface.md)（模式设计、外部入口、Trajectory、干预面、审批、自我修改、resume、plan/goal、压缩与后台）
  - [05 路线图与 Python 复现](report/05-roadmap.md)
  - [06 附录与 HOWTO](report/06-appendix.md)

## 教程手册

> 每章：概念 → 最小可运行代码 → 解释 → 测试/硬性规定 → 可选练习。

### 章节顺序

依赖与 Capstone 见 [system-map Fig S6](system-map.md#fig-s6)。

### 章节表 {#chapter-table}

| 章 | 模块边界 | 体裁 | 主验收 | 内容摘要 | 上游（入口） |
|---|---|---|---|---|---|
| [00](chapters/00-setup.md) | 环境与纪律 | 准备 | 全量 unittest + demo | 包结构、变量、简化立场 | — |
| [01](chapters/01-event-sourced-session.md) | Session 日志与投影 | 实现 | `test_session.py` | append、derive_messages、repair | `packages/core/session` |
| [02](chapters/02-plugin-context-and-event-bus.md) | Context / 总线 | 实现 | `test_bus.py` | 四种派发、scope、可逆副作用 | `vendor/cordis` |
| [03](chapters/03-tool-execution-pipeline.md) | 工具管线 | 实现 | `test_tools.py` | pre/execute/post、schema | `packages/core/tools` |
| [04](chapters/04-agent-loop-and-llm-streaming.md) | Loop + LLM | 实现 | `test_loop.py` | turn/step、SSE、§4.9 重试 | `core/agent-loop` |
| [05](chapters/05-persistence-recovery-composition.md) | 持久化 + boot | 实现 | `test_persistence_boot.py` | JSONL、boot、patch | `session-persistence` |
| [06](chapters/06-advanced-seams.md) | 能力扩展口 | 实现+延伸 | `test_seams.py` | 沙箱 / 凭据 / 子 agent | capability-seams |
| [07](chapters/07-external-entry-points.md) | 入口 + web + 协议（多页） | 解读 | `test_web_*.py` | headless、gateway 形态 wire | `api/gateway` 等 |
| [08](chapters/08-composition-layer.md) | loader + preset | 解读 | `test_composition.py` | EntryTree、roster | `vendor/loader` |
| [09](chapters/09-agent-intervention.md) | 干预 + 审批 | 对照 | loop + `tests/test_web_approvals.py` 等 | steer、cancel、ask | `interaction/user-approval` |
| [10](chapters/10-trajectory-projection.md) | Trajectory 折叠 | 实现 | `tests/test_trajectory.py` | fold_trajectory | `ui-trajectory` |
| [11](chapters/11-runtime-self-modification.md) | 动态插件 | 边界 | `tests/test_dynamic.py` | define/run 生命周期 | `packages/extensions` |
| [12](chapters/12-async-parallel-tools.md) | 并行工具 | 对照 | `tests/test_loop.py`、tool_calls 相关 | async 派发、屏障 | `core/agent-loop` |
| [13](chapters/13-cordis-service-interceptor-logger.md) | Service / intercept | 实现 | `tests/test_bus.py` | LoggerService | `vendor/cordis` |
| [14](chapters/14-dsh-scope-carrier.md) | dsh_scope 载波 | 实现 | scope 相关单测 | scopeTarget 载波 | `core/scope` |
| [15](chapters/15-schemastery.md) | schemastery | 实现 | `tests/test_schema_full.py` | resolve / ValidationError | `vendor/schemastery` |

## 快速开始

```bash
# 在仓库根目录执行（代码包 miniharness/ 与测试 tests/ 都在根下）

# 1. 跑全部测试（Python 3.10+；`httpx`/`filelock`/`watchdog`/`pyyaml` 为依赖）
python -m unittest discover -s tests -t .

# 2. 体验一个真实回合（无需 API key，用内置假模型）
python -m miniharness.demo
```

> 每章开头都有"先动手再读解释"的最小代码；读完一章就运行一次该章测试，用断言验证硬性规定。

## 手册与报告的关系

- **[system-map.md](system-map.md)**：功能模块在整体架构中的位置（Fig S1～S6）与章节验收表。
- **[guides/learning-paths.md](guides/learning-paths.md)**：走读对照 / 空白复现、按目标选章。
- **报告**回答上游「是什么、为什么」；**章节**在共享内核上逐块走读或复现（实现章 vs 解读章见上表「体裁」列）。
- 上游包 ↔ mini 模块 ↔ **手册章号** ↔ 报告页：以 [architecture.md §2](architecture.md#sec-2) 映射表为行级单源；[report/index.md](report/index.md) 四维表作跳转索引。
- 复现清单：[report/05-roadmap.md](report/05-roadmap.md) §7.3 与 01～05 章；Mini 分阶段路线见同页 Mini P0～P5。

## 学完你能做到什么

完成后你应当能：

1. 用一句话讲清"模型可见 ⟺ 已记录"为什么是事件溯源的根本约束；
2. 手绘 turn/step 时序，解释 reject 分支为何留下持久化记录；
3. 解释 waterfall 的 `next()` 短路语义，并说出四种派发模式各自适用场景；
4. 解释能力扩展口三角色为什么缺一不可；
5. 跑通一个"文本 + 一个工具 + 会话持久化 + 崩溃恢复"的端到端 demo（第 05 章验收）。