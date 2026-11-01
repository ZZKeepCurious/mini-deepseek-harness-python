# 源码走读（source_implement）

<p class="lead">旁路 <code>dsh-superagent-prototype/src</code> 的中文行为解读；代码真源以本目录 + 锚定 commit 为准，产品抽象见 <a href="../design-abstract.md">设计抽象</a>。</p>

## 锚定

| 项 | 值 |
|----|-----|
| 旁路 commit | `6337fc8`（工作区 `status/upstream/superagent-baseline.md`） |
| 英文方法索引 | 旁路 `docs/007-architecture.md`、[proto-007](../proto-007-architecture.md) |
| Python 参照 | `superagent/`、[superagent-framework.md](../superagent-framework.md) |

## 目录结构

| 路径 | 用途 |
|------|------|
| [container.md](container.md) | 运行容器：DSH 边界、Runtime、Pass/事务 |
| [lifecycle/](lifecycle/01-trigger-receive.md) | **主轴**：第 1–7 章，按工单时间顺序 |
| [side-paths.md](side-paths.md) | **M2**：attach、handback、订阅、People、域事件 |
| [sidechain.md](sidechain.md) | 部署、Glue、管理 UI（非主线） |
| [reference-semantics.md](reference-semantics.md) | 谓词、Engine 五段、Store、写者（检索用） |
| 本节 [模块索引](#sec-module-index) | 92 个 `src/` 文件 → 测试锚点 |

## 阅读顺序（一条线）

1. [运行容器](container.md)
2. 生命周期章节（下表顺序）
3. [侧路径](side-paths.md)（路由、hand-back、人控票、notice/relay）
4. 按需：[侧链](sidechain.md)、[语义参照](reference-semantics.md)

| 章 | 文档 | 摘要 |
|----|------|------|
| 1 | [Trigger 摄入](lifecycle/01-trigger-receive.md) | Source → Passes → Intake → Eval 排队 |
| 2 | [Eval 与 accept](lifecycle/02-eval-decide-accept.md) | 批次 Eval、decide、原子建 Ticket |
| 3 | [Assignment 生命周期](lifecycle/03-assignment-lifecycle.md) | launch、delivery、report |
| 4 | [Outbox 回复](lifecycle/04-outbox-reply.md) | receipt、reply effect、Source.send |
| 5 | [evaluateDue](lifecycle/05-evaluate-due.md) | Pass pump、超时、wake |
| 6 | [Policy 变更](lifecycle/06-policy-changed.md) | settings → policyChanged |
| 7 | [Thread 关闭](lifecycle/07-thread-close-github.md) | PR merged/closed、followsDone |

Python 七场景：`tests/superagent/test_flows.py`。侧路径：`test_handback.py`、`test_outcomes_attach.py`、`test_followup_owners.py`、`test_direct_decisions.py`、`test_context_threads.py` 等。

## 模块索引 {#sec-module-index}

路径均相对 `dsh-superagent-prototype/src/`。测试列指向旁路 `tests/` 中最相关 spec。

| 路径 | 卷 | 职责摘要 | 测试锚点 |
|------|-----|----------|----------|
| `index.ts` | 部署 | Cordis 插件：Runtime、SourceHost | `index.spec.ts`, `deployment.spec.ts` |
| `deployment.ts` | 部署 | stateDir、Machine | `deployment.spec.ts` |
| `setup.ts` | 部署 | 全局 setup 问题类型 | `deployment.spec.ts` |
| `policy.ts` | 部署 | Policy、gate、assessment 转 outcome | `policy.spec.ts` |
| `admission.ts` | 部署 | 准入表与 scope | `admission-context.spec.ts` |
| `limits.ts` | 部署 | volatile 限制 | `deployment.spec.ts` |
| `navigation.ts` | 部署 | 侧栏导航 | `navigation.spec.ts` |
| `kv.ts` | Store | typed KV 键表 | `store.spec.ts` |
| `app/runtime.ts` | 容器 | 组合根 | `runtime.spec.ts` |
| `app/setup.ts` | 容器 | 账户、环境 shim | `runtime.spec.ts` |
| `app/context.ts` | 容器 | HTTP/工具共享上下文 | `runtime.spec.ts` |
| `infra/health.ts` | 容器 | HealthBoard | `health.spec.ts` |
| `infra/commands.ts` | 容器 | 外部 CLI | `commands.spec.ts` |
| `infra/environment.ts` | 容器 | 环境变量 | `environment.spec.ts` |
| `infra/paths.ts` | 容器 | 路径解析 | `deployment.spec.ts` |
| `infra/text.ts` | 容器 | IM 文本长度 | `outbox.spec.ts` |
| `core/engine.ts` | Engine | 域入口、五段协调 | `core-engine.spec.ts` |
| `core/intake.ts` | Engine | receive、fan-out | `core-engine.spec.ts` |
| `core/evals.ts` | Engine | Eval、decide | `core-engine.spec.ts` |
| `core/tickets.ts` | Engine | Ticket 生命周期 | `core-engine.spec.ts` |
| `core/assignments.ts` | Engine | 执行器回调 | `core-engine.spec.ts` |
| `core/replies.ts` | Engine | reply 规则 | `core-engine.spec.ts` |
| `core/model.ts` | Engine | 类型与谓词 | `stage.spec.ts` |
| `core/ports.ts` | Engine | 端口定义 | `core-engine.spec.ts` |
| `core/memory.ts` | Engine | 内存 Repositories | `store.spec.ts` |
| `core/part.ts` | Engine | EnginePart 基类 | `core-engine.spec.ts` |
| `core/stage.ts` | Engine | 阶段表 | `stage.spec.ts` |
| `core/errors.ts` | Engine | UserError | `tools.spec.ts` |
| `core/reasons.ts` | Engine | Reason | `policy.spec.ts` |
| `core/ids.ts` | Engine | 内容派生 id | `core-engine.spec.ts` |
| `core/index.ts` | Engine | 导出 | — |
| `app/store.ts` | 异步 | SQLite 域仓库 | `store.spec.ts` |
| `app/store-read.ts` | 异步 | 只读查询 | `views.spec.ts` |
| `app/migration.ts` | 异步 | schema 迁移 | `migration.spec.ts` |
| `app/versions.ts` | 异步 | settings/prompt 版本 | `settings.spec.ts` |
| `app/effects.ts` | 异步 | 提交后副作用 | `effects.spec.ts` |
| `app/passes.ts` | 异步 | Pass + evaluateDue | `passes.spec.ts` |
| `app/outbox.ts` | 异步 | receipt + reply | `outbox.spec.ts` |
| `executor/dsh.ts` | 执行 | DSH Session 执行器 | `queue.spec.ts` |
| `executor/background.ts` | 执行 | 后台 job | `background.spec.ts` |
| `app/assessment.ts` | 执行 | MasterAssessor | `assessment.spec.ts` |
| `app/sessions.ts` | 执行 | 主 Session relay | `followups.spec.ts` |
| `app/notices.ts` | 执行 | 域事件 → 主 Session | `followups.spec.ts` |
| `app/delivery.ts` | 执行 | AgentDelivery 投影 | `delivery.spec.ts` |
| `app/retry-health.ts` | 执行 | 模型重试健康 | `retry-health.spec.ts` |
| `sources/registry.ts` | Sources | Registry / Scope | `registry.spec.ts` |
| `sources/host.ts` | Sources | SourceHost | `sources.spec.ts` |
| `sources/kind.ts` | Sources | SourceKind | `registry.spec.ts` |
| `sources/poll.ts` | Sources | 轮询 | `sources.spec.ts` |
| `sources/admission-context.ts` | Sources | Thread 窗口 | `admission-context.spec.ts` |
| `sources/github.ts` | Sources | GitHub 插件 | `sources.spec.ts` |
| `sources/github-channel.ts` | Sources | GitHub 发送 | `commands.spec.ts` |
| `sources/github-pull.ts` | Sources | PR 规则 | `sources.spec.ts` |
| `sources/github-read.ts` | Sources | GitHub 读 | `github-read.spec.ts` |
| `sources/github-trigger.ts` | Sources | GitHub Trigger | `sources.spec.ts` |
| `sources/feishu.ts` | Sources | 飞书 Source | `sources.spec.ts` |
| `sources/feishu-channel.ts` | Sources | 飞书发送 | `sources.spec.ts` |
| `sources/feishu-loops.ts` | Sources | 飞书事件环 | `sources.spec.ts` |
| `sources/feishu-read.ts` | Sources | 飞书读 | `feishu-read.spec.ts` |
| `sources/feishu-trigger.ts` | Sources | 飞书 Trigger | `sources.spec.ts` |
| `sources/feishu-image.ts` | Sources | 飞书图片 | `feishu-image.spec.ts` |
| `app/source-context.ts` | Sources | SourceContext | `registry.spec.ts` |
| `app/http.ts` | Glue | 管理 API | `http.spec.ts` |
| `app/api.ts` | Glue | API 绑定 | `http.spec.ts` |
| `app/tools/main.ts` | Glue | 主 Session 工具 | `tools.spec.ts` |
| `app/tools/executor.ts` | Glue | Worker 工具 | `tools.spec.ts` |
| `app/tools/tool.ts` | Glue | 工具共享 | `tools.spec.ts` |
| `app/prompts.ts` | Glue | Prompt 版本 | `prompts.spec.ts` |
| `app/settings.ts` | Glue | Settings | `settings.spec.ts` |
| `app/settings-history.ts` | Glue | 设置历史 | `settings.spec.ts` |
| `app/cards.ts` | Glue | 候选卡片 | `cards.spec.ts` |
| `app/links.ts` | Glue | 管理面链接 | `views.spec.ts` |
| `app/resources.ts` | Glue | 资源 host | `runtime.spec.ts` |
| `app/execution-environment.ts` | Glue | Ticket 目录 | `environment.spec.ts` |
| `app/cache.ts` | Glue | TextCache | `cache.spec.ts` |
| `app/role-models.ts` | Glue | 模型配置 | `role-models.spec.ts` |
| `app/skill-files.ts` | Glue | Skill 注入 | `model-surface.spec.ts` |
| `client/api.ts` | UI | SPA API 客户端 | `client.spec.tsx` |
| `client/route.ts` | UI | 前端路由 | `navigation.spec.ts` |
| `client/locales.ts` | UI | 文案 | `locales.spec.ts` |
| `client/markdown.ts` | UI | Markdown | `markdown.spec.ts` |
| `client/marks.ts` | UI | 状态标记 | `marks.spec.ts` |
| `client/prompt-config.ts` | UI | Prompt UI | `prompt-config.spec.ts` |
| `client/text.ts` | UI | 文本工具 | `draft.spec.ts` |
| `client/css.d.ts` | UI | 样式类型 | `styles.spec.ts` |
| `app/views/overview.ts` | UI | 概览 | `panel-overview.spec.tsx` |
| `app/views/records.ts` | UI | 时间线 | `timeline.spec.ts` |
| `app/views/settings.ts` | UI | 设置 | `panel-settings.spec.tsx` |
| `app/views/sources.ts` | UI | Source 视图 | `panel-source.spec.tsx` |
| `app/views/text.ts` | UI | 文本视图 | `views.spec.ts` |
| `app/views/threads.ts` | UI | Thread 视图 | `views.spec.ts` |
| `app/views/tickets.ts` | UI | Ticket 视图 | `panel-execution.spec.tsx` |

## Python 对照（总览）

| TS 区域 | Python 模块 |
|---------|----------------|
| `core/*` | `superagent/superagent/core/` |
| `app/store|effects|passes|outbox|runtime` | `superagent/superagent/app/` |
| `sources/registry` + 假 Source | `superagent/superagent/sources/` |
| `executor/dsh`（假实现） | `superagent/superagent/executor/fake.py` |
| `policy.ts`（最小） | `superagent/superagent/infra/policy.py` |

Python 包不含 Cordis 壳、管理 `client/` 全量、GitHub/飞书真 API。范围见 [superagent-framework.md](../superagent-framework.md) 与 `superagent/README.md`。
