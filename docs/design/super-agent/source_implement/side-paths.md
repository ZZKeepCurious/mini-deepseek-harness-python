# 侧路径：路由、订阅与 hand-back

<p class="lead">生命周期 1–7 章描述「一条 Trigger 从进来到 Outbox 结果」的主链。主 Session 路由、worker hand-back、人控票与 Thread 订阅改的是 <strong>同一张 Trigger 下一步进 Eval 还是进 worker</strong>。读完第 7 章后读本篇，把域里其余高频写路径补齐。</p>

## 在走读中的位置

| 路径类型 | 典型入口 | 域方法（Engine Pick） |
|----------|----------|------------------------|
| 主轴 | Source `receive` | Sources → Intake |
| 本篇 · 主 Session 路由 | 工具 `orchestrator_route` / `attach` | MainSession |
| 本篇 · worker 交还 | 工具 `orchestrator_trigger_handback` | Executor |
| 本篇 · Thread 订阅 | `followForContext` / `markUnrelated` | Executor |
| 本篇 · 人与面板 | retry / stop / reevaluate / unsubscribe | People |
| Glue 翻译 | 见 [reference-semantics § Glue](reference-semantics.md#sec-glue) | HTTP / 工具 → 上表 |

以下步骤均发生在 `repo.atomic()` 内，除非注明为 commit 后的 effect（与 [container](container.md) 一致）。

---

## 1. `attach`：把 Trigger 指给已有 Ticket

**动机**：Eval 已 accept 某批 Trigger，或 Thread 上仍有 open Ticket 时，主 Session 不必再开新票，而是把**新到达或批内另一条** Trigger 交给该 Ticket 的 worker 处理。

1. 主 Session 调用 `attach(ticketId, triggerId, note?)`（工具侧常为 `orchestrator_route` 的一项 `attach`）。
2. **Tickets** 校验：Ticket 须 live（或符合 processing 未暂停时的 ended **resume** 规则）；Trigger 须已存且未被「不可路由」状态阻塞（例如已在该 Thread hand-back 排队 Eval 的 Trigger 再 attach 会 `UserError handedBackHere`）。
3. 更新 **disposition.fanout**：记录该 Trigger 被路由到哪些 Ticket；可能生成新的 **Input**（带可选 routing note），Executor 在下次 delivery 看到。
4. **结束**仍持有该 Trigger 的 **Evaluation**（批内其余 Trigger 可能触发 reassess 或新批——见 `test_outcomes_attach` 中「attach 结束 batch eval」）。
5. 若 Ticket 曾 **ended** 且 Mandate 仍 `revalidate` 通过，**人的** attach 可 **wake** 新 Assignment（bot attach 不复活 ended 票，见 `test_reviews_routing_reads`）。

**与 Intake 的分叉**：Intake 在 `receive` 时若 `followupOwners` 非空，Trigger **不进 Eval**，只 fan-out Input。attach 是主 Session **显式**把 Eval 或游离 Trigger 绑到指定 Ticket，并常 **终止**当前 Eval，避免同一 Trigger 上并行两个决策。

**worker 侧约束**：已被 attach 到 open Ticket、且 worker 尚未 hand-back 的 Trigger，主 Session **不能**对该 Trigger 再 `decide(accept)`（`UserError workerDecides`）——须 worker `handback` 交还 Eval 队列。

| 边界 | 结果 |
|------|------|
| attach 到 ended 且非人消息 | 不 resume |
| attach 带 note | Input 携带 note，不单独授予 Mandate |
| stopped / revoked Mandate | attach 拒绝 |
| 批 Eval 中 attach 其一 | 结束该 Eval，其余 Trigger 可能 reassess |

**测试**：旁路 `core-engine.spec.ts` attach 段；Python `test_outcomes_attach.py`、`test_followup_owners.py`（attach wake ended）、`test_resume_stop.py`、`test_reviews_routing_reads.py`。

---

## 2. `handback`：worker 把 Trigger 交还 Eval

**动机**：worker 认为某条 Input/Trigger 不应由本 Ticket 独占（新需求、客人消息、越权 thread），需要主 Session 重新 gate 并 accept/stop/ignore。

1. Executor 调用 `handback(ticketId, triggerId, reason)`。
2. Engine 校验 Trigger **reached** 该 Ticket，且该 Ticket **曾决定**这条消息（经 fan-out、attach 或 accept 批内归属）。否则返回 `false` 或 `UserError`（如 `notReached`）。
3. 在 Trigger **disposition.handbacks** 记录 `{ticketId, reason, at}`；**同一 Ticket 对同一 Trigger 只能 hand-back 一次**（再次调用 → `UserError handedBack`）。
4. 若尚无其它 Ticket 仍 `followupOwners` 占有该 Trigger，把 Trigger id 推回 **primary Thread 的 pending**，下次 Pass `Evals.start` 开新 Eval；hand-back **保留** Thread 订阅，便于后续 fan-out。
5. `pump()` 后 Assessor 收到新 batch；strength 随 gate（explicit / inferred 等）。

**与 attach 串联**（典型故事）：主 Session `attach(ticket, trigger)` → worker 收到 Input → worker 无法单独 accept → `handback(..., reason)` → Thread pending → 主 Session `decide(ignore|accept|...)`。见 Python `test_handback.test_attached_message_handback_sends_to_eval`。

| 边界 | 结果 |
|------|------|
| 人消息已直接进 Eval、非本 Ticket 决定 | `handback` → false |
| context-only Thread 上的消息 | 通常不能 hand-back（Ticket 未「决定」） |
| hand-back 后 | 同 Ticket 不可再 attach 同 Trigger（`handedBackHere`） |

**测试**：`test_handback.py`；旁路 core-engine handback 描述。

---

## 3. Thread 订阅：`followForContext` 与 `markUnrelated`

**followForContext(ticketId, thread)**：Ticket **订阅**相关 Thread 仅作 **context**——该 Thread 上新 Trigger 会 fan-out Input，但 **不**增强 follow-up 强度、**不**因 alone 触发 Eval 排队（仍看 `followupOwners`）。用于 worker 读周边 Issue/群聊，而不把每条消息都当新工单。

**markUnrelated(ticketId, thread, reason)**：撤销对该 attach Thread 的 **订阅**；对该 Thread 上由本 Ticket worker 决定的 follow-up 执行 **hand-back** 语义（带 reason），并通知主 Session（relay/notice 路径）。

**context 线程 Intake 标记**：fan-out 时 Input 可标 `context: true`（见 [第 1 章](lifecycle/01-trigger-receive.md)）。ended Ticket 不会因 context Thread  alone 而 resume，除非 **attach** 明确路由（`test_context_threads.py`）。

**测试**：`test_context_threads.py`、`test_subscription.py`；旁路 subscription / context 相关 spec。

---

## 4. People：`stopTicket`、`retryTicket`、`reevaluate`、`unsubscribe`、`dismiss`

这些方法走 **People** Pick（面板 HTTP 或主 Session 工具 `orchestrator_ticket_*`、`orchestrator_trigger_requeue`）。

### `stopTicket`

撤销 Mandate，结束 Ticket；未读 follow-up 可能重新进入 Eval 队列（除非其它 Ticket 仍 owner 或 disposition 已 settled）。stop 的消息 **不会**自动 re-evaluate。

### `retryTicket`

当 `canRetry(ticket)` 为真（blocked/failed 且等人，或 Input **delivery** 失败未清除）：结束停滞 Assignment，开 **新 Assignment**；阶段表从 `person/*` 回到 `working/queued` 等（见 [reference § 阶段](reference-semantics.md#sec-model)）。

### `reevaluate(triggerId)`

对已存 Trigger 强制 **fresh Eval**（`thread.pending`），不重复存 Trigger。若 Trigger 已关联 started Ticket 的 Decision，可能返回已有 Decision 而非新 Eval（见 `test_direct_decisions.test_reevaluate_replayed_trigger_returns_started_ticket_decision`）。与 hand-back 区别：reevaluate **不**写 handbacks 记录，由人主动重审。

### `unsubscribe(thread)`

丢弃该 Thread 上 **全部** Subscription；曾 follow 的 Ticket 不再 follow，但 Ticket 仍处理已到达的 follow-up 规则。

### `dismiss(decisionId)`

关闭 **unavailable** Decision 所示问题，由 Decision 记录结案。

**测试**：`test_resume_stop.py`、`test_direct_decisions.py`、`test_stage.py`（retry 与 delivery 阶段）；旁路 `core-engine.spec.ts` People 段、`policy.spec.ts`。

---

## 5. 域事件 → notice 与 relay

Engine 在事务末尾从 `core/ports.ts` **`DomainEvents`** 发出事实，例如：

`trigger.received`、`eval.started`、`decision.recorded`、`ticket.created`、`ticket.resumed`、`ticket.ended`、`assignment.started`、`assignment.ended`、`thread.subscribed`、`thread.unsubscribed`、`trigger.requeued`、`policy.changed`。

**Runtime 消费（commit 后，经 Sessions / Effects）**：

- `ticket.ended`、`trigger.requeued`、`thread.unsubscribed` → **notice** effect（有序，limit 1）→ 主 Session 卡片。
- 送达 Ticket 的 **人** `trigger.received` → **relay** effect → 主 Session 看到 worker 侧 follow-up 摘要。
- Ticket 因 PR **merged/closed** 结束 → GitHub glue **`pullEndNotices`** 用稳定 message id 发 IM（与 [第 7 章](lifecycle/07-thread-close-github.md) 衔接）。

其它事件目前可无消费者；走读不断言 mini 已实现 notice 文案。

**测试**：旁路 `followups.spec.ts`、`notices` 相关；Python 多通过 Harness 的 assessor/outbox 断言，非全量 notice 快照。

---

## 6. 与主轴章节的交叉索引

| 侧路径 | 常与之交叉的主轴章 |
|--------|---------------------|
| attach + handback | 1 receive、2 Eval、3 Assignment |
| retry / delivery 失败 | 3 Assignment、4 Outbox（delivery 与 reply 不同） |
| reevaluate / unavailable dismiss | 2 Eval、5 evaluateDue |
| follow / context | 1 receive、7 Thread 关闭 |
| policy.changed + wake | 5 evaluateDue、6 Policy |
| stop + pending requeue | 2 Eval、5 evaluateDue |

工具参数到 Engine 的完整映射见 [reference-semantics § Glue](reference-semantics.md#sec-glue)；英文逐字段说明见 [proto-007](../proto-007-architecture.md) Engine 与 Model-facing interfaces 节。
