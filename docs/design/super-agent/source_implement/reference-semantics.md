# 语义与模块参照

<p class="lead">生命周期七篇讲「按时间发生什么」；本篇按「类型、Engine 五段、持久化、Executor、Sources、写者边界」组织，供对照源码与写测试时检索。锚定 commit <code>6337fc8</code>。方法全集与 14+6 工具细节仍以 [proto-007](../proto-007-architecture.md) 英文块为准，此处给出走读常用表。</p>

<a id="sec-model"></a>

## 域模型与谓词

锚点：`core/model.ts`、`stage.ts`、`reasons.ts`。

| 实体 | 职责 | 写者 |
|------|------|------|
| `Trigger` | 不可变外部事件；`threads[0]` 为 Eval 主 Thread | Engine（Intake 存） |
| `Thread` | 订阅、pending、evaluating、correlated | Engine |
| `Evaluation` | 每 Thread 至多一个进行中的 Eval | Engine（Evals） |
| `Decision` | Eval 或 direct Eval 的结果 | Engine（Evals） |
| `Ticket` | Mandate、Assignments、Inputs、follows | Engine（Tickets） |
| `TriggerDisposition` | filtered/fanout/handbacks/acked | Engine（Intake 等） |

Source 拥有 `Trigger.meta`、`Thread.meta`；core 不解释 meta 语义。`Mandate` 在 accept 冻结；`revalidate(mandate)` 配合 Policy 变更后的 `wake`。

| `via` | 准入依据（`MandateBasis`） |
|-------|---------------------------|
| `addressed` | 允许的发送者 @ agent |
| `followup` | 已有 Ticket 的 Thread 上到达消息 |
| `admission` | 准入表单元格；`observation` 可标记 inferred candidate |
| `listed` | 列表内发送者（与 proto-002 一致） |
| `bot` | 所列 bot 产生 inferred candidate |

| 函数 | 文件 | 用途 |
|------|------|------|
| `followupOwners` | `model.ts` | 人的 follow-up 是否仍由某 Ticket worker 决定 |
| `waitsForPerson` | `model.ts` | 最新 report 为 blocked/failed，且此后无人的 Input 到达 |
| `reached` | `model.ts` | Trigger 是否到达 Ticket（含 context 线程） |
| `settled` | `model.ts` | Decision 是否已了结该 Trigger 请求 |
| `liveTicket` / `runningAssignment` | `model.ts` | 开放票 / 运行中 Assignment |
| `ticketStage` / `canRetry` | `stage.ts` | 面板阶段；人可否 retry 停滞票 |

`Reason`（`reasons.ts`）：`code` + `values` + 可选 `triggerId`；面板 `reason.<code>`。`UserError`（`errors.ts`）供 HTTP/工具共用。Python：`superagent.core.model`、`stage.py`（`tests/superagent/test_stage.py` 与旁路 `stage.spec.ts` 同表）。

### Ticket 阶段（`ticketStage`）

与 [proto-003 执行](../proto-003-execution.md) 一致：**自上而下首条匹配**（`core/stage.ts`）。

| 返回 `{stage, action?}` | 条件（摘要） | 面板含义 |
|-------------------------|--------------|----------|
| `ended` | `ticket.ended` 已设 | 已结束 |
| `person` / `delivery` | `ticket.delivery` 投递失败未清除 | Input 未送达 |
| `person` / `answer` | `ticket.attention` 打开 | 等人回答 |
| `person` / `review` | 最新 Assignment report 为 `ready` | 等人审阅 |
| `person` / `blocked` 或 `failed` | report 为 blocked/failed 且 `waitsForPerson` | 阻塞 / 失败 |
| `working` / `queued` | 无 Assignment，或最新为 `pending` | 排队 launch |
| `idle` | report `waiting`，或 Assignment 已 `ended` | 等 Trigger 续跑 |
| `working` | 其余（含 `progress`、运行中 Assignment） | 执行中 |

人消息到达后，blocked/failed 且曾 `waitsForPerson` 的票通常回到 `working`。`canRetry(ticket)` 为真当：票未结束，且（`waitsForPerson` 或仍有 `delivery` 失败）——见 `test_stage.CanRetryTests` 用例表。

<a id="sec-engine"></a>

## Engine 五段

锚点：`core/engine.ts`、`intake.ts`、`evals.ts`、`tickets.ts`、`assignments.ts`、`replies.ts`。

公开方法包在 `repo.atomic()` 内（`evaluateDue` 对多 Thread/Ticket **多次** atomic）。端口在事务内 enqueue，适配器 commit 后执行。五段依赖单向：Intake → Evals → Tickets → Assignments → Replies。

### 五段职责（命令簇）

| 段 | 写入/协调 | 典型入口 |
|----|-----------|----------|
| Intake | Trigger 存取、disposition、fan-out Input、Eval 排队 | `receive` |
| Evals | Evaluation、Decision、accept 原子建票 | `start`、`decide`、`due`、`policyChanged` |
| Tickets | Mandate、订阅、follows、end/wake/attach | `create`、`bindThread`、`threadClosed`、`addInput` |
| Assignments | Assignment 状态、report、attention | `assignmentStarted`、`report`、`inputsDelivered` |
| Replies | Outbox 请求合法性 | `reply` |

### Engine 方法（按 Pick 角色）

命令式名如 `stopTicket`；Executor **回调**用过去式如 `assignmentStarted`。完整英文说明见 proto-007 Engine 表。

| 角色 | 方法 | 效果摘要 |
|------|------|----------|
| Sources | `receive(trigger)` | 存 Trigger；filtered/duplicate；fan-out；无 owner 则 Eval 排队 |
| Sources | `bindThread(ticketId, thread, own)` | Ticket follow Thread（如 PR） |
| Sources | `threadClosed(thread, outcome, at)` | 更新 follows；`followsDone` 且 settled 时 end |
| MainSession | `candidate` / `candidateDelivered` / `decide` | Eval 卡片与 accept/ignore/stop |
| MainSession | `attach` | 路由 Trigger 到 Ticket，结束持有 Eval |
| MainSession | `note` / `requestStatus` / `reply` | 事实 Input、要 status、Outbox |
| People | `stopTicket` / `retryTicket` / `reevaluate` / `unsubscribe` / `dismiss` | 人控票与 Eval |
| Executor | `assignmentStarted`、`inputsDelivered`、`inputsRead`、`turnEnded` | 投递与读指针 |
| Executor | `deliveryFailed`、`report`、`markUnrelated`、`followForContext`、`handback` | 失败、结果、线程订阅 |
| Executor | `assignmentInterrupted` | 重启恢复 |
| Passes | `evaluateDue()` | start Eval、expire/requeue、wake paused Ticket |
| Settings | `policyChanged(from, to, effects)` | 通知 Ticket + reassess Eval |

逐步叙事（attach、handback、People、域事件）：[侧路径](side-paths.md)。

### `reply` purpose（`core/replies.ts`）

| purpose | 主要校验 |
|---------|----------|
| `acknowledgement` | human + channel；Trigger `reaches` Ticket；可 `takeUp` |
| `answer` | `ticket.answers` 含 trigger 或已有 outbox |
| `result` | 票 ready/completed/merged/dropped，或 live 且最新 report ready |
| `attention` | question 匹配 `ticket.attention` |

Outbox 消息 id 常由 `ids.ts` 派生（如 `key('ack', triggerId)`）。测试：`core-engine.spec.ts`、`stage.spec.ts`。

<a id="sec-store"></a>

## Store、Effects、Passes

锚点：`app/store.ts`、`effects.ts`、`passes.ts`、`outbox.ts`；键族 `kv.ts`。

### SQLite（schema 11）

| 表 / 行族 | 内容 |
|-----------|------|
| `trigger` / `disposition` | Trigger 体与去向 |
| `thread` / `ticket` / `decision` | 域聚合 JSON |
| `outbox` | Receipt：`pending` / `sent` / `dropped` 等 |
| `effect` | 副作用队列（`kind` + `key` 唯一） |
| `kv` | Settings/Prompt/Source 等 typed 键 |

生成列工作集（如 `ticket.open`、`thread.active`、`outbox.pending`）供 Pass 扫描 active 行。WAL；`atomic` 内写域；`afterCommit` 在成功 commit 后跑。读路径 **`store-read.ts`** 与 **`app/views/*`** 不得绕过 Engine 写 Ticket。

| effect kind | handler | limit | ordered |
|-------------|---------|-------|---------|
| assessment | MasterAssessor | 4 | 否 |
| launch | DshExecutor | worker 槽 | 否 |
| delivery | DshExecutor | 4 | 否 |
| relay | Sessions | 1 | 是 |
| notice | Sessions | 1 | 是 |
| reply | Outbox.deliver | 1 | 否 |

同 `kind+key` 再次 enqueue：**替换 payload**，`generation++`，`attempts=0`。handler 返回 `Later` 不占失败计数（Source 未装、Outbox behind 等）。**Passes**：约 2s 定时 + `receive` 合并 `request()`；`pumpOnce` = `effects.kick()` → `engine.evaluateDue()`；`configured()`  false 时不 pump。

**Outbox**：`send` 同事务写 receipt + enqueue `reply`；`deliver` 检查同 ticket+conversation 前序 pending，再 `sources.send`。

Python：`superagent.app.store`、`effects`、`passes`、`outbox`。测试：`store.spec.ts`、`effects.spec.ts`、`passes.spec.ts`、`outbox.spec.ts`。

<a id="sec-executor"></a>

## DshExecutor

锚点：`executor/dsh.ts`、`background.ts`。

- 每 Ticket **一个** worker Session（`worker-<ticketId>`）与工作目录；Engine 只 enqueue `launch` / `delivery`。
- **WorkerSlots**：`maxWorkers` 限制并发；槽满 → effect `Later`（约 5s）；人 wake 可多占一槽并优先于 launch。
- **Launch**：prepare 目录/命令 → `sessions.ensure` → assignment prompt → `assignmentStarted` → enqueue delivery。`PrepareFailed` **不**盲目自动重试 launch。
- **Delivery**：`inputMessage` 按 Ticket 状态渲染；`inputsDelivered` / `inputsRead` / `turnEnded`；turn 结束默认可能 report `waiting`。
- **`stop` worker**：经 `Store.afterCommit`，避免域回滚后误停 Session。
- **resume**：`assignmentInterrupted` 对比 latest report 与 post-report Input；未完成 launch 按 Ticket **最老优先**重排队。
- **background.ts**：turn 结束后 shell/subagent 仍跑 → `BACKGROUND_LIMIT_MS`（30 分钟量级，见 proto-003）后 stop 并 report waiting。

测试：`queue.spec.ts`、`background.spec.ts`、`runtime.spec.ts`。Python：`executor/fake.py`、`scripted.py`（无 DSH Session）。

<a id="sec-assessor"></a>

## Assessor、Sessions、Settings

锚点：`app/assessment.ts`、`sessions.ts`、`settings.ts`、`notices.ts`、`prompts.ts`。

| 组件 | 行为摘要 |
|------|----------|
| MasterAssessor | `request` → enqueue `assessment` → 主 Session 卡片 → `candidateDelivered` 起算 deadline；`release(evalId)` 清理 |
| Assessor 工具 | `orchestrator_eval` / `_read` / `_decide` → bounded context + `Engine.decide` |
| Sessions | `ensure` 主/worker；`relay`/`notice` 有序 effect；prompt revision 绑定 |
| Settings | `save(block, revision)`；`pause`；保存后 `policyChanged` |
| Prompts | 版本与 compose；save 不经 Engine 写 Ticket |

主 Session preset：`dsh-superagent-router`（5s shell/job wait）；worker：`WORKER_PRESET`（见 `cordis.patch.yml` / `sessions.ts`）。域事件经 `notices.ts` 进主 Session；`pullEndNotices` 在 PR 合并/关闭后发稳定 id IM。

测试：`assessment.spec.ts`、`followups.spec.ts`、`settings.spec.ts`。

<a id="sec-sources"></a>

## Sources（GitHub / Feishu）

锚点：`sources/registry.ts`、`host.ts`、`poll.ts`、`app/source-context.ts`。

`SourceRegistry` + `SourceHost.context(kind)` → **`SourceContext`** 窄接口：

| 能力 | 说明 |
|------|------|
| `receive` | → `Passes.receive` |
| `atomic` | 与 Store 同事务读写的辅助 |
| `threads.describe/patchMeta` | Thread 元数据（Source 写） |
| `tickets.bindThread` / `threadClosed` | **仅**经 Engine 改 Ticket |
| `notify` | Outbox 路径 |
| `state` | Source KV |

### GitHub 文件族

| 文件 | 职责 |
|------|------|
| `github-trigger.ts` | Issue/PR/comment → Trigger |
| `github-read.ts` | API 读、分页 |
| `github-pull.ts` | `claimPull`、`observePull`、close、**bind 规则** |
| `github-channel.ts` | comment/send |
| `github.ts` | 插件装配、poll |

**PR 绑定（`github-pull.ts`）**：Ticket origin 可为 PR Trigger；agent 分支末段匹配 ticket `execution.name`；**一 PR 至多一个 open Ticket owner**；`threadClosed` → merged/closed outcome。

### Feishu 文件族

| 文件 | 职责 |
|------|------|
| `feishu-trigger.ts` | IM → Trigger；mention/direct |
| `feishu-read.ts` / `feishu-image.ts` | 读消息与图片 |
| `feishu-channel.ts` | 发送（Outbox 侧字数校验 `infra/text.ts`） |
| `feishu-loops.ts` | 事件循环 |
| `feishu.ts` | 实例与 health |

`poll.ts` 更新读指针；Trigger 仍统一进 Passes。测试：`sources.spec.ts`、`registry.spec.ts`、`github-read.spec.ts`、`feishu-read.spec.ts`。Python：`sources/registry`、`fake_source`（无真 API）。

<a id="sec-writers"></a>

## 单写者与依赖

锚点：旁路 `docs/007-architecture.md` Dependency rules。

```
core（无 I/O）→ app + executor → client 面板
sources → app（不 import app 实现，仅 host 面）
```

| 状态 | 写者 |
|------|------|
| Trigger / Thread / Decision / Ticket / Assignment 域 | **Engine** |
| Source Thread meta、读指针 | 该 **Source**（经 SourceContext） |
| Settings 与 history | **Settings** |
| Prompt 版本 | **Prompts** |
| effect 行 | **Effects**（enqueue 在 Engine 事务内） |
| Outbox receipt | **Outbox**（`send` / deliver settle） |
| health 项 | 声明该 entry 的 Runtime 部件 |

Source **不得**直接改 Ticket 行；仅 `bindThread`、`threadClosed` 命令。Runtime 用 `Pick<Engine,…>`、`ExecutorHost` 等窄接口破环。GitHub/Feishu 与 glue（Outbox 字数、links、cards、`pullEndNotices`）仍有交叉依赖，换 Source 需改这些 seam 或抽象。

<a id="sec-glue"></a>

## Glue 与视图

锚点：`app/tools/main.ts`、`executor.ts`、`http.ts`、`api.ts`、`app/views/*`。HTTP 与面板 **写**路径必须进 Engine 或 Settings/Prompts。详见 [侧链](sidechain.md)。

### 主 Session 应用工具（14 个，007 表）

| 用途 | 工具名 |
|------|--------|
| 读与路由 | `orchestrator_read`, `orchestrator_route` |
| 管 Ticket | `orchestrator_ticket_retry`, `orchestrator_ticket_request_status`, `orchestrator_ticket_note` |
| 重排队 Eval | `orchestrator_trigger_requeue` |
| 评估 proactive | `orchestrator_eval`, `orchestrator_eval_read`, `orchestrator_eval_decide` |
| 通信 / 退订 | `orchestrator_reply`, `orchestrator_thread_unsubscribe` |
| 配置 | `orchestrator_settings_save`, `orchestrator_policy_sync`, `orchestrator_prompts_save` |

`orchestrator_route` 可批 1–20 条 decision，每条单独 Engine 命令。另有 DSH 通用工具（bash、read、skill、job_* 等），路由 preset 限制长 wait（`sessions.ts` `noWait` guard）。

### Worker 应用工具（6 个）

| 工具 | 用途 |
|------|------|
| `orchestrator_read` | 读本 Ticket 资源 |
| `orchestrator_ack` | 确认请求 |
| `orchestrator_report` | progress/ready/blocked…；可绑 PR |
| `orchestrator_thread_unrelated` | 丢弃无关 attach + handback |
| `orchestrator_thread_follow` | context 订阅 |
| `orchestrator_trigger_handback` | 单次 handback + reason |

Ticket 结束后 worker 工具拒绝。GitHub 读/claim 经 SourceContext（`readPull`、`claimPull`）。

### 视图读路径（`app/views/`）

| 模块 | 投影 |
|------|------|
| `overview.ts` | Eval 健康、open tickets 摘要 |
| `tickets.ts` / `threads.ts` | 执行面板、Thread 列表 |
| `records.ts` | 时间线 |
| `settings.ts` / `sources.ts` | 设置与 Source 配置 |
| `text.ts` | 长文本 |

测试：`tools.spec.ts`、`http.spec.ts`、`views.spec.ts`、`timeline.spec.ts`、`panel-*.spec.tsx`。

### `orchestrator_read` 资源（摘要）

`app/resources.ts` 的 `RESOURCES`：`tickets`、`triggers`、`threads`、`decisions` 分页列表；`thread/<key>`、`trigger/<id>`、`ticket/<id>` 及 ticket 子路径（assignments、inputs、report、transcript 等）。各角色可见集合由 `readResource` 限制（见 `tools.spec.ts`）。
