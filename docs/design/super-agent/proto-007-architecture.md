# Prototype · 007 架构

<p class="lead">对照 `dsh-superagent-prototype/docs/007-architecture.md`（618 行）。</p>

!!! info "延伸材料"
    实现骨架与论文术语对照见 [设计抽象 §7](design-abstract.md#sec-7-prototype)。

## 007 Architecture

!!! note "原文（English）"

    # 007 Architecture

    This is the developer guide to the implementation. It maps the concepts in [001 Domain model](proto-001-domain.md) through [006 Deployment](proto-006-deployment.md) to classes, interfaces and files. Start with the runtime and Engine, then use the module and test tables as a code index.

!!! quote "译文（中文）"

    # 007 架构

    实现开发者指南：将 [001 领域模型](proto-001-domain.md) 至 [006 部署](proto-006-deployment.md) 的概念映射到类、接口与文件。从 Runtime 与 Engine 入手，再以模块表与测试表作代码索引。

## Core classes and glue

!!! note "原文（English）"

    ## Core classes and glue

    Core classes own domain concepts and external boundaries behind small interfaces. Glue translates tool arguments, HTTP requests, Source payloads and DeepSeek Harness events into calls to those classes, then turns results into cards, views and messages.

    ```mermaid
    flowchart TB
      plugins[Source plugins] -->|SourceContext| engine
      tools[Session tools] --> engine
      http[Management routes] --> engine
      engine[Engine] -->|Repositories| store[Store]
      engine -->|Policy| policy[Policy]
      engine -->|SourceScope| registry[SourceRegistry]
      engine -->|Assessor| assessor[MasterAssessor]
      engine -->|Executor| executor[DshExecutor]
      assessor -->|decide| engine
      engine -->|Outbox| outbox[Outbox]
      executor -->|assignmentStarted, inputsDelivered, report| engine
      settings[Settings] -->|policyChanged| engine
      assessor & executor & sessions[Sessions] & outbox -->|enqueue| effects[Effects]
      effects --> store
    ```

    `Runtime` (`src/app/runtime.ts`) constructs the application, supplies dependencies, and owns startup and shutdown. `Engine` constructs its own internal parts. The Executor and Assessor call back into the Engine, so Runtime gives them getters rather than requiring construction in a circular order.

!!! quote "译文（中文）"

    ## 核心类与胶水

    核心类在窄接口背后持有领域概念与外部边界。胶水把工具参数、HTTP 请求、Source 载荷与 DeepSeek Harness 事件翻译成对这些类的调用，再把结果变成卡片、视图与消息。

    ```mermaid
    flowchart TB
      plugins[Source plugins] -->|SourceContext| engine
      tools[Session tools] --> engine
      http[Management routes] --> engine
      engine[Engine] -->|Repositories| store[Store]
      engine -->|Policy| policy[Policy]
      engine -->|SourceScope| registry[SourceRegistry]
      engine -->|Assessor| assessor[MasterAssessor]
      engine -->|Executor| executor[DshExecutor]
      assessor -->|decide| engine
      engine -->|Outbox| outbox[Outbox]
      executor -->|assignmentStarted, inputsDelivered, report| engine
      settings[Settings] -->|policyChanged| engine
      assessor & executor & sessions[Sessions] & outbox -->|enqueue| effects[Effects]
      effects --> store
    ```

    `Runtime`（`src/app/runtime.ts`）构建应用、供给依赖并负责启动与关闭。`Engine` 自行构造内部部件。Executor 与 Assessor 会回调 Engine，因此 Runtime 用 getter 提供它们，而不是按环状依赖顺序构造。

### Runtime

!!! note "原文（English）"

    ### Runtime

    `Runtime` is the composition root; it contains no domain rules. Its constructor locks the state directory, opens the Store and builds each component. If construction fails, it closes the Store and releases the lock.

    Startup registers management routes and runs Setup. After setup resolves, it installs Session and Executor hooks, ensures the main Session exists, starts Policy syncs, resumes interrupted Assignments, and starts passes and effects. `ready` marks completion of startup.

    Shutdown cancels turns, waits for passes, effects and agents, restores the process environment, closes the Store and releases the lock. Source plugins inject Runtime as the `orchestrator` service through the narrow `SourceHost` interface.

    | Part | Module | Does |
    | --- | --- | --- |
    | Setup | `src/app/setup.ts` | Resolves the repository and the accounts by probing their CLIs the way worker shells run them, writes and applies the workers' environment and command shims, and keeps the setup problems; Settings checks a changed account through it, and restarts through it, which runs setup again |
    | Passes | `src/app/passes.ts` | One pass at a time, every two seconds and whenever a command asks: the due effects, then `Engine.evaluateDue`; a Source's Trigger reaches the Engine through it |
    | Outbox | `src/app/outbox.ts` | The Engine's `Outbox` port, and the only writer of the receipts: `send` keeps a message's receipt and queues its `reply` effect in one transaction, and the effect's handler sends it through `SourceRegistry.send`; a message waits while its Source is off, and is dropped when the conversation is gone or not listened to |
    | Notices | `src/app/notices.ts` | Converts domain events and worker activity into main-Session messages. `eventNotice` handles domain notices; `reportRelay` forwards questions immediately, keeps ordinary progress on its Ticket, and relays other reports; `followupRelay` names the Tickets that own a human follow-up. Relays arriving during a turn are delivered together at its next step through the DeepSeek Harness inbox |

!!! quote "译文（中文）"

    ### Runtime

    `Runtime` 是组合根，不含领域规则。构造函数锁定 state 目录、打开 Store 并构建各组件。若构造失败，则关闭 Store 并释放锁。

    启动时注册管理路由并运行 Setup。Setup 完成后安装 Session 与 Executor hooks、确保主 Session 存在、启动 Policy 同步、恢复被中断的 Assignment，并启动 passes 与 effects。`ready` 标记启动完成。

    关闭时取消 turn、等待 passes/effects/agents、恢复进程环境、关闭 Store 并释放锁。Source 插件经窄接口 `SourceHost` 将 Runtime 作为 `orchestrator` 服务注入。

    | 部件 | 模块 | 作用 |
    | --- | --- | --- |
    | Setup | `src/app/setup.ts` | 以 worker shell 相同方式探测 CLI，解析仓库与账号，写入并应用 worker 环境与命令 shim，保留 setup 问题；Settings 经它校验变更的账号并经它重启（会再次运行 setup） |
    | Passes | `src/app/passes.ts` | 每次只跑一轮 pass：每两秒及有命令请求时处理到期 effects，再 `Engine.evaluateDue`；Source 的 Trigger 经它到达 Engine |
    | Outbox | `src/app/outbox.ts` | Engine 的 `Outbox` 端口，也是 receipt 的唯一写入者：`send` 在同一事务中保留消息 receipt 并入队其 `reply` effect，handler 经 `SourceRegistry.send` 发送；Source 关闭时消息等待，会话消失或未监听时丢弃 |
    | Notices | `src/app/notices.ts` | 将领域事件与 worker 活动转成主 Session 消息。`eventNotice` 处理领域通知；`reportRelay` 立即转发提问、普通 progress 留在 Ticket 上并中继其它 report；`followupRelay` 标明拥有人工 follow-up 的 Ticket。turn 期间到达的中继在下一步经 DeepSeek Harness inbox 一并投递 |

### Engine

!!! note "原文（English）"

    ### Engine

    `Engine` (`src/core/engine.ts`) is the only writer of Triggers, Threads, Decisions, Tickets and Assignments. It receives these ports from `src/core/ports.ts`: `Repositories`, a current-Policy getter, `SourceScope`, `Assessor`, `Executor` and `Outbox`. Optional dependencies are `prepareTicket`, `now` and `emit`.

    All Engine methods are synchronous. Each command changes its aggregates in one transaction. `evaluateDue` uses a separate transaction for each Eval or Ticket it changes. Ports enqueue external work inside the transaction; adapters perform it after commit. Invalid requests throw `UserError`.

    The Engine exposes one entry point and delegates to five internal parts. It owns each command's transaction. Commands spanning parts, such as `attach`, `evaluateDue` and `policyChanged`, call the parts in sequence.

    Each part extends `EnginePart` (`src/core/part.ts`), which provides options and shared reads. Dependencies between the parts flow in one direction:

    | Part | Responsibility | Depends on |
    | --- | --- | --- |
    | `Intake` | A Source's Trigger: stored once with its disposition, recorded on its Threads and their correlations, fanned out to the subscribed Tickets and, unless it is a person's message that reached one, queued for Eval on its primary Thread | Tickets |
    | `Evals` | The Evals of Threads: batching, the gate's strength, actor and basis with the Trigger that gave them (`primary`), the deadline from the candidate's delivery and its shift by a pause, the requeue of an expired Eval, candidates, replays and hand-backs, the requeue on a Policy change, and the Decision each ends with: accept, stop, ignore or unavailable | Tickets |
    | `Tickets` | The Ticket lifecycle: creation from an accept with its Mandate, Subscriptions and first Assignment, the Threads it follows and their closes (`followsDone`), attached Threads and Threads followed for context, Inputs and notes, resume and the messages a resume refuses, retry, restart, stop and end, the Subscriptions it drops, and the notice of a Policy change to the Tickets it bears on | Replies |
    | `Assignments` | What the Executor and its workers report: an Assignment started, Inputs reached it or failed to, a restart interrupted it, and a worker's Report with its question for a person | Tickets, Replies |
    | `Replies` | Channel writes through the `Outbox`: the acknowledgement, the answer, the result and the attention message, each by the rule that allows it | Nothing |

    Commands use imperative names such as `stopTicket`; facts reported by adapters use past-tense names such as `assignmentStarted`. The query `candidate` uses a noun.

    Callers receive only their role's `Pick` of the Engine: `EngineForSources`, `EngineForMainSession`, `EngineForPeople`, `EngineForSettings` or `EngineForExecutor`.

    | Role | Method | Effect |
    | --- | --- | --- |
    | Sources | `receive(trigger)` | Stores a Trigger once and answers `stored`, `duplicate`, or `filtered` when no Source receives it; fans it out to the Tickets subscribed to its Threads, where a person's message resumes an ended Ticket at once unless processing is paused, and queues it for Eval on its primary Thread, unless it is a person's message that reached a Ticket, which stays that Ticket's |
    | Sources | `bindThread(ticketId, thread, own)` | Makes an open Ticket follow one of the Source's Threads, such as a pull request's, `own` when the Ticket opened it; a closed Thread bound again is open again. The Threads' closes, not a `ready` Report, end the Ticket |
    | Sources | `threadClosed(thread, outcome, at)` | A Thread closed: each open Ticket that follows it records the close, and ends once it is done with the Threads it follows (`followsDone`): no Thread it opened is open, or, when it opened none, every Thread it follows closed. It ends `merged` or `closed` as the latest of those closes did, unless work resumed after the close has not reported `ready`, or its Executor took up, by acknowledging or answering it, a person's follow-up it decides after its latest Report other than `progress`, or the end of its last turn; a `dropped` Report then ends it as the close did |
    | Main Session | `candidate(triggerId)` | The Eval that holds a Trigger, as its assessor sees it |
    | Main Session | `candidateDelivered(evalId)` | The Eval's candidate reached the main Session: its deadline counts from now |
    | Main Session | `decide(triggerId, outcome)` | Records an Eval Decision, or a direct Decision for an explicit request. Accept creates the Ticket, Mandate, Assignment, Subscriptions and acknowledgement atomically. An inferred candidate ignored from its card records `ignoredFromCard` before the supplied reason. While `followupOwners` still owns a human follow-up, only stop is allowed |
    | Main Session | `attach(ticketId, triggerId, note?)` | Routes a Trigger to a Ticket it points at, whose Executor then decides it as a follow-up, and ends the Eval that holds it, whose other Triggers wait for a fresh Eval; an ended Ticket resumes under its Mandate at once unless processing is paused. A follow-up an Executor still decides is refused, as is one routed back to the Ticket that handed it back |
    | Main Session | `requestStatus(ticketId)` | Asks a waiting Executor Session for its status, and answers whether it asked |
    | Main Session | `note(ticketId, text)` | Gives a Ticket's Executor a fact from other work as a text Input, which grants nothing |
    | Main Session, Executor | `reply(request)` | Queues a message for the Channel its purpose names: `acknowledgement`, `answer`, `result` or `attention` |
    | People | `stopTicket(ticketId, reason?)` | A person's stop, which withdraws the Mandate |
    | People | `retryTicket(ticketId)` | Ends the stalled Assignment of a Ticket that waits for a person after a `failed` or `blocked` Report, or whose Input delivery failed (`canRetry`), and starts a new one |
    | People | `reevaluate(triggerId)` | Queues a stored Trigger for a fresh Eval |
    | People | `unsubscribe(thread)` | Drops every Subscription on a Thread, and the Tickets that followed it follow it no more; each keeps deciding the follow-ups it received there |
    | People | `dismiss(decisionId)` | Dismisses the problem an `unavailable` Decision shows, which the Decision records |
    | Executor | `assignmentStarted(ticketId, assignmentId)` | An Assignment runs; its waiting Inputs go out |
    | Executor | `inputsDelivered(ticketId, assignmentId, inputIds)` | Inputs reached the Assignment's Executor Session, where each waits for a step to take it |
    | Executor | `inputsRead(ticketId, inputIds)` | The Executor Session took the messages that carry these Inputs into its history (a `user/message` Session event), from which its model has them; a step that claimed them and was cancelled or failed before does not count. Each counts as read once a Report or the end of a turn follows, also after a restart, and its Ticket's end does not let go of it; an Input delivered before reads were recorded counts as read once a Report or the end of a turn followed its delivery |
    | Executor | `turnEnded(ticketId, assignmentId)` | A turn of the Executor Session ended, also after a Report, once nothing runs for it in the background or as it hands off to a person. What a step took before, it read (`inputsRead`) |
    | Executor | `deliveryFailed(ticketId, assignmentId, error)` | Delivering Inputs failed; the Ticket shows it until a delivery succeeds, a new Assignment starts or the Ticket ends |
    | Executor | `report(ticketId, report)` | Records a Report and its question for a person; `dropped` ends the Ticket, and `ready` completes one that follows no Thread, and ends one done with the Threads it follows as they ended |
    | Executor | `markUnrelated(ticketId, thread, reason)` | Drops only that Ticket's Subscription to an attached Thread it found unrelated, and hands back, with the reason, the follow-ups there that the Ticket decides |
    | Executor | `followForContext(ticketId, thread)` | Subscribes an open Ticket to a related Thread for context: its messages reach the Ticket but lend it no follow-up strength and do not resume it |
    | Executor | `handback(ticketId, triggerId, reason)` | Records that the Ticket hands back a Trigger that reached it, with the reason its candidate carries, and queues it for a fresh Eval once no other Ticket decides it, if this Ticket did; it answers whether it queued it. A Ticket hands back each Trigger once and keeps its Subscription |
    | Executor | `assignmentInterrupted(ticketId, assignmentId)` | An Assignment was running when the host stopped: one interrupted mid-work, or one that received an Input after its latest Report, starts again with a recovery note and the Inputs delivered to it that its Executor Session did not take, as a retry does; one that had reported since keeps its Session. The DshExecutor reports each at start (`resume`), oldest Ticket first, and queues again a launch that waits |
    | Passes | `evaluateDue()` | Unless processing is paused: starts the Evals of the Threads whose queued Triggers wait, one per Thread with the Triggers queued meanwhile as one batch; queues the Triggers of an Eval past its deadline again, twice in a row, and records it `unavailable` the third time; and resumes the ended Tickets a person's message reached during a pause, if their Mandates still hold. An Eval whose candidate has not reached the main Session does not expire. While processing is paused, no Eval expires, and once it resumes each deadline moves later by the pause's length |
    | Settings | `policyChanged(from, to, effects)` | Tells each live Ticket the change bears on how (`policyEffects` in `src/policy.ts`: a rule it was accepted under changed or no longer applies, its requester may no longer start work, or proactive work is no longer accepted), and restarts the Evals in progress under the new version |

    The Engine keeps batching, authorization, fan-out, resume and attention lifecycle rules private. Several shared predicates make these rules consistent:

    - `waitsForPerson` (`src/core/model.ts`): the latest Assignment reported `blocked` or `failed`, and no human message has arrived since. Automatic Inputs and failed launches remain held while this is true.
    - `followupOwners`: the Tickets still responsible for a human follow-up received on their own Threads. A Ticket can be live or ended without a human stop and waiting to resume. Handing back the message ends that ownership.
    - `settled`: a Decision resolved the request; stop, unavailable, routed and refused-stop Decisions do not count.
    - `ticketStage` (`src/core/stage.ts`): implements the stage table in [003 Execution](proto-003-execution.md).

    When a Ticket ends, unread follow-ups return to Eval unless another Ticket still owns them or another disposition settled them. Acknowledged, answered and handed-back messages are excluded. For older records without acknowledgement dispositions, the outbox receipt supplies that fact. A final Report also covers messages waiting when the Assignment began, including the message that resumed it. The message that stopped a Ticket is not automatically re-evaluated.

    After a change, the Engine emits an event from `DomainEvents` in `src/core/ports.ts`:

    `trigger.received`, `eval.started`, `decision.recorded`, `ticket.created`, `ticket.resumed`, `ticket.ended`, `assignment.started`, `assignment.ended`, `thread.subscribed`, `thread.unsubscribed`, `trigger.requeued`, or `policy.changed`.

    Runtime turns `ticket.ended`, `trigger.requeued` and `thread.unsubscribed` into notices. A human `trigger.received` delivered to Tickets becomes a follow-up relay. `pullEndNotices` sends one chat notice, using a stable id, when a Ticket ends merged or closed. Other events currently have no consumer.

!!! quote "译文（中文）"

    ### Engine

    `Engine`（`src/core/engine.ts`）是 Trigger、Thread、Decision、Ticket 与 Assignment 的唯一写入者。它从 `src/core/ports.ts` 接收端口：`Repositories`、当前 Policy getter、`SourceScope`、`Assessor`、`Executor` 与 `Outbox`。可选依赖为 `prepareTicket`、`now` 与 `emit`。

    所有 Engine 方法均为同步。每条命令在一个事务内修改其聚合。`evaluateDue` 对其改变的每个 Eval 或 Ticket 使用独立事务。端口在事务内入队外部工作；适配器在提交后执行。非法请求抛出 `UserError`。

    Engine 暴露单一入口并委托给五个内部部件。它拥有每条命令的事务。跨部件命令（如 `attach`、`evaluateDue`、`policyChanged`）按序调用各部件。

    各部件继承 `EnginePart`（`src/core/part.ts`），提供选项与共享读取。部件间依赖单向流动：

    | 部件 | 职责 | 依赖 |
    | --- | --- | --- |
    | `Intake` | Source 的 Trigger：连同 disposition 只存一次，记在其 Thread 与 correlation 上，扇出到订阅 Ticket；除非已是到达某 Ticket 的人的消息，否则在其主 Thread 上排队 Eval | Tickets |
    | `Evals` | Thread 上的 Eval：批处理、gate 强度、actor 与 basis（含给出它们的 `primary` Trigger）、自候选送达起的 deadline 及 pause 偏移、过期 Eval 重排队、candidate/replay/hand-back、Policy 变更重排队，以及最终 Decision：accept、stop、ignore 或 unavailable | Tickets |
    | `Tickets` | Ticket 生命周期：accept 创建 Ticket/Mandate/Subscription/首个 Assignment、所 follow 的 Thread 及其关闭（`followsDone`）、attach 与 context follow、Input 与 note、resume 及 resume 拒绝的消息、retry/restart/stop/end、丢弃的 Subscription、Policy 变更通知所影响的 Ticket | Replies |
    | `Assignments` | Executor 及其 worker 上报的事实：Assignment 开始、Input 送达或失败、重启中断、worker Report 及对人的提问 | Tickets, Replies |
    | `Replies` | 经 `Outbox` 的 Channel 写入：acknowledgement、answer、result、attention，各按允许规则 | 无 |

    命令用祈使名如 `stopTicket`；适配器上报的事实用过去式如 `assignmentStarted`。查询 `candidate` 用名词。

    调用方只收到其角色对 Engine 的 `Pick`：`EngineForSources`、`EngineForMainSession`、`EngineForPeople`、`EngineForSettings` 或 `EngineForExecutor`。

    | 角色 | 方法 | 效果 |
    | --- | --- | --- |
    | Sources | `receive(trigger)` | Trigger 只存一次，无 Source 接收时答 `stored`/`duplicate`/`filtered`；扇出到订阅其 Thread 的 Ticket（人消息除非 processing 暂停会立即 resume 已结束 Ticket），并在主 Thread 排队 Eval，除非人消息已到达某 Ticket 则留在该 Ticket |
    | Sources | `bindThread(ticketId, thread, own)` | 让 open Ticket follow Source 的 Thread（如 PR），Ticket 打开时为 `own`；已关闭 Thread 再绑定则重新 open。Thread 关闭（非 `ready` Report）结束 Ticket |
    | Sources | `threadClosed(thread, outcome, at)` | Thread 关闭：follow 它的 open Ticket 记录关闭，在 `followsDone` 后结束：其打开的 Thread 均已关闭，或未打开任何 Thread 时 follow 的全部关闭。以最近关闭的 `merged`/`closed` 结束，除非关闭后 resume 的工作未报 `ready`、Executor 在最新非 progress Report 之后通过 ack/answer 接手 follow-up、或最后一 turn 结束；`dropped` Report 则按关闭方式结束 |
    | Main Session | `candidate(triggerId)` | 持有 Trigger 的 Eval，assessor 所见 |
    | Main Session | `candidateDelivered(evalId)` | Eval 候选到达主 Session：deadline 从此时计 |
    | Main Session | `decide(triggerId, outcome)` | 记录 Eval Decision 或显式请求的直接 Decision。Accept 原子创建 Ticket/Mandate/Assignment/Subscription/acknowledgement。卡片上忽略 inferred candidate 时先记 `ignoredFromCard` 再记所给 reason。`followupOwners` 仍拥有人工 follow-up 时只允许 stop |
    | Main Session | `attach(ticketId, triggerId, note?)` | 将 Trigger 路由到所指 Ticket，Executor 作 follow-up 决定，并结束持有它的 Eval（其它 Trigger 等 fresh Eval）；已结束 Ticket 除非 processing 暂停立即在 Mandate 下 resume。Executor 仍在决定的 follow-up 拒绝，路由回 hand-back 的 Ticket 也拒绝 |
    | Main Session | `requestStatus(ticketId)` | 向等待中的 Executor Session 要 status，并回答是否已询问 |
    | Main Session | `note(ticketId, text)` | 以文本 Input 给 Executor 其它工作的事实，不授予权限 |
    | Main Session, Executor | `reply(request)` | 为 purpose 所名 Channel 排队消息：`acknowledgement`、`answer`、`result` 或 `attention` |
    | People | `stopTicket(ticketId, reason?)` | 人的 stop，撤回 Mandate |
    | People | `retryTicket(ticketId)` | 结束在 `failed`/`blocked` Report 后等人或 Input 投递失败（`canRetry`）的停滞 Assignment 并开新 Assignment |
    | People | `reevaluate(triggerId)` | 将已存 Trigger 排队 fresh Eval |
    | People | `unsubscribe(thread)` | 丢弃 Thread 上全部 Subscription，曾 follow 的 Ticket 不再 follow；各 Ticket 仍决定在那里收到的 follow-up |
    | People | `dismiss(decisionId)` | 关闭 `unavailable` Decision 所示问题，由 Decision 记录 |
    | Executor | `assignmentStarted(ticketId, assignmentId)` | Assignment 运行；等待的 Input 发出 |
    | Executor | `inputsDelivered(ticketId, assignmentId, inputIds)` | Input 到达 Assignment 的 Executor Session，各等一步取用 |
    | Executor | `inputsRead(ticketId, inputIds)` | Executor Session 将承载这些 Input 的消息纳入历史（`user/message` 事件），模型由此可见；此前 claim 后取消/失败的步不算。Report 或 turn 结束后各算读一次，重启后亦然，Ticket 结束不释放；在记录 read 前送达的 Input 在送达后随 Report/turn 结束算读 |
    | Executor | `turnEnded(ticketId, assignmentId)` | Executor Session 的一 turn 结束（Report 后亦然），无后台运行或交给人时。步之前取用的即 read（`inputsRead`） |
    | Executor | `deliveryFailed(ticketId, assignmentId, error)` | Input 投递失败；Ticket 展示直至投递成功、新 Assignment 开始或 Ticket 结束 |
    | Executor | `report(ticketId, report)` | 记录 Report 及对人的提问；`dropped` 结束 Ticket；`ready` 完成不 follow Thread 的 Ticket，或在 follow 的 Thread 均已结束时结束 Ticket |
    | Executor | `markUnrelated(ticketId, thread, reason)` | 仅丢弃该 Ticket 对无关 attach Thread 的 Subscription，并 hand-back 该处 Ticket 决定的 follow-up 及 reason |
    | Executor | `followForContext(ticketId, thread)` | 为 context 订阅相关 Thread：消息到达 Ticket 但不增 follow-up 强度、不 resume |
    | Executor | `handback(ticketId, triggerId, reason)` | 记录 Ticket hand-back 到达的 Trigger 及 candidate 的 reason；若无其它 Ticket 仍在决定且本 Ticket 曾决定则排队 fresh Eval；回答是否已排队。每 Trigger hand-back 一次并保留 Subscription |
    | Executor | `assignmentInterrupted(ticketId, assignmentId)` | host 停止时 Assignment 在跑：工作中断或最新 Report 后收到 Input 的，带 recovery note 与 Executor Session 未取用的已投递 Input 重启（同 retry）；已 Report 的保留 Session。DshExecutor 启动时经 `assignmentInterrupted` 上报（`resume`，最旧 Ticket 优先）并重排队等待的 launch |
    | Passes | `evaluateDue()` | 除非 processing 暂停：启动 queued Trigger 等待的 Thread 的 Eval，每 Thread 一个，期间排队的 Trigger 作一批；deadline 过的 Eval 的 Trigger 再排队两次，第三次记 `unavailable`；暂停期间人消息到达的已结束 Ticket 在 Mandate 仍有效时 resume。候选未达主 Session 的 Eval 不过期。暂停期间 Eval 不过期，恢复后各 deadline 延后 pause 时长 |
    | Settings | `policyChanged(from, to, effects)` | 告知各 live Ticket 变更如何影响（`src/policy.ts` 的 `policyEffects`：accept 所依规则变或不再适用、请求者可能不能再开工、或不再 accept 主动工作），并在新版本下重启进行中的 Eval |

    Engine 将批处理、授权、扇出、resume 与 attention 生命周期规则保持私有。若干共享谓词使规则一致：

    - `waitsForPerson`（`src/core/model.ts`）：最新 Assignment 报 `blocked` 或 `failed`，且此后无人消息。为真时自动 Input 与失败 launch 仍被 hold。
    - `followupOwners`：仍对自己 Thread 上收到的人工 follow-up 负责的 Ticket。Ticket 可 live 或无人 stop 的 ended 并等待 resume。hand-back 消息结束该归属。
    - `settled`：Decision 已解决请求；stop、unavailable、routed、refused-stop 不算。
    - `ticketStage`（`src/core/stage.ts`）：实现 [003 执行](proto-003-execution.md) 的阶段表。

    Ticket 结束时，未读 follow-up 回到 Eval，除非其它 Ticket 仍拥有或另一 disposition 已 settled。已 ack、已 answer、已 hand-back 排除。旧记录无 ack disposition 时 outbox receipt 提供该事实。最终 Report 也覆盖 Assignment 开始时等待的消息（含 resume 消息）。stop Ticket 的消息不自动 re-evaluate。

    变更后 Engine 从 `src/core/ports.ts` 的 `DomainEvents` 发出事件：

    `trigger.received`、`eval.started`、`decision.recorded`、`ticket.created`、`ticket.resumed`、`ticket.ended`、`assignment.started`、`assignment.ended`、`thread.subscribed`、`thread.unsubscribed`、`trigger.requeued` 或 `policy.changed`。

    Runtime 将 `ticket.ended`、`trigger.requeued`、`thread.unsubscribed` 转为 notice。送达 Ticket 的人 `trigger.received` 成为 follow-up relay。Ticket merged/closed 结束时 `pullEndNotices` 用稳定 id 发一条 chat notice。其它事件目前无消费者。

### Model

!!! note "原文（English）"

    ### Model

    `src/core/model.ts` holds the domain types, and `src/core/stage.ts` the Ticket stages. A Decision records why as `reason`, a list of `Reason`s (`src/core/reasons.ts`): each a `code` with its `values` and, for one Trigger of a batch, its `triggerId`.

    The core's codes, such as `timeout`, `policyChanged`, `routed` and `startedTicket`, are `REASONS`, with the English that agents read through `reasonText`; a gate or a Source scope gives its own, such as `not_a_member`; and `said` carries words an assessor or a person wrote. The panel words each code by `reason.<code>`, and a Trigger a Source filtered records the Source's code the same way.

    A Ticket keeps each fact once. It is open while it has no `ended`, which records when, by whom, why and with what outcome it ended; what it last said is its latest Assignment Report (`latestReport`); and its Executor Session is named after it (`workerSessionId` in `src/executor/dsh.ts`).

    Its fields are typed: `origin`, the admissible Trigger that gave its Eval's batch its strength and the batch itself (`triggerId`, `triggerIds`); `execution`, what the Executor chose (`name`, `directory` and `assignment`); `delivery`, the last failed Input delivery (`error`, `at`), until a delivery succeeds, a new Assignment starts or the Ticket ends; and `follows`, the Threads a Source made it follow, each `{thread, own, closed?}`: `own` when the Ticket opened it, and `closed` with `at` and `outcome` once it closed. Tickets alone writes `follows`; the GitHub Source reads its pull requests with `pullsOf` and `pullOf` (`src/sources/github-pull.ts`).

    An Input that carries a Trigger records whether a person wrote it, which `waitsForPerson` reads. A Thread also lists in `context` the subscribed Tickets that follow it only for context, and counts in `expired` the Evals of its queued Triggers that expired in a row; an Evaluation records its `primary` Trigger and when its candidate was `delivered`.

    A Trigger's disposition records the Tickets it reached, by fan-out or attachment, a person's requeue, each Ticket that handed it back, with the reason, and each Ticket whose Executor acknowledged or answered it.

    A Mandate's `basis` (`MandateBasis`) records how the gate admitted its Trigger. Revalidation reads the receiving `source`, `scope` and `row`, the listed `sender`, matched `rules`, and `via`:

    | `via` | Admission basis |
    | --- | --- |
    | `addressed` | An allowed sender addressed the agent |
    | `followup` | A message arrived on a Thread with Tickets |
    | `admission` | An admission-table cell; `observation` marks an inferred candidate |
    | `bot` | A listed bot produced an inferred candidate |

    Trigger and Thread `meta` belong to the Source. The core does not interpret them.

!!! quote "译文（中文）"

    ### Model

    `src/core/model.ts` 持有领域类型，`src/core/stage.ts` 持有 Ticket 阶段。Decision 以 `reason` 记录原因，即 `Reason` 列表（`src/core/reasons.ts`）：每项为带 `values` 的 `code`，批中某一 Trigger 还带 `triggerId`。

    核心 code 如 `timeout`、`policyChanged`、`routed`、`startedTicket` 属于 `REASONS`，agent 经 `reasonText` 读英文；gate 或 Source scope 自有 code 如 `not_a_member`；`said` 承载 assessor 或人写的文字。面板用 `reason.<code>` 措辞，Source 过滤的 Trigger 同样记录 Source 的 code。

    Ticket 每条事实只存一次。无 `ended` 时为 open，`ended` 记录何时、谁、为何、以何 outcome 结束；最后所说为最新 Assignment Report（`latestReport`）；Executor Session 以其命名（`src/executor/dsh.ts` 的 `workerSessionId`）。

    字段有类型：`origin`——使 Eval 批获得 strength 的可 admissible Trigger 及批本身（`triggerId`、`triggerIds`）；`execution`——Executor 所选（`name`、`directory`、`assignment`）；`delivery`——末次失败的 Input 投递（`error`、`at`），直至投递成功、新 Assignment 开始或 Ticket 结束；`follows`——Source 令其 follow 的 Thread，各 `{thread, own, closed?}`：Ticket 打开时为 `own`，关闭后带 `at` 与 `outcome` 的 `closed`。仅 Tickets 写 `follows`；GitHub Source 用 `pullsOf`、`pullOf` 读 PR（`src/sources/github-pull.ts`）。

    携带 Trigger 的 Input 记录是否人写，`waitsForPerson` 读取。Thread 在 `context` 列出仅作 context follow 的订阅 Ticket，在 `expired` 计数 queued Trigger 的 Eval 连续过期次数；Evaluation 记录 `primary` Trigger 与 candidate `delivered` 时刻。

    Trigger 的 disposition 记录 fan-out 或 attachment 到达的 Ticket、人的 requeue、hand-back 的 Ticket 及 reason、Executor ack 或 answer 的 Ticket。

    Mandate 的 `basis`（`MandateBasis`）记录 gate 如何 admit Trigger。Revalidation 读接收 `source`、`scope`、`row`、所列 `sender`、匹配 `rules` 与 `via`：

    | `via` | 准入依据 |
    | --- | --- |
    | `addressed` | 允许的发送者 @ agent |
    | `followup` | 已有 Ticket 的 Thread 上到达消息 |
    | `admission` | 准入表单元格；`observation` 标记 inferred candidate |
    | `bot` | 所列 bot 产生 inferred candidate |

    Trigger 与 Thread 的 `meta` 归 Source，核心不解释。

### Policy

!!! note "原文（English）"

    ### Policy

    `src/policy.ts` defines settings schemas, authorization and assessment rules. `settingsPolicy(settings, scope)` returns a fresh, stateless `Policy` on each use:

    - `version`, `paused` and `evalTimeoutMs` describe its current state.
    - `gate(trigger, thread, tickets)` returns admission strength and basis.
    - `revalidate(mandate)` checks whether an existing Mandate still holds.
    - `admitsInferred(rules)` permits proactive acceptance only in auto mode with enabled matching rules.

    `assessmentOutcome(assessment, settings, strength)` converts the model's assessment to an Engine outcome. It requires a judgement for every enabled rule, settled direction, a matching rule and evidence of intent. Outside auto mode, an inferred accept becomes a “would accept” record.

    `policyEffects(tickets, old, now, scope)` identifies which open Tickets a change affects and how. `Engine.policyChanged` notifies only those Tickets. `policyVersion` hashes rules, accept mode and the Sources' Policy parts, including bot lists.

!!! quote "译文（中文）"

    ### Policy

    `src/policy.ts` 定义 settings schema、授权与评估规则。`settingsPolicy(settings, scope)` 每次返回新的无状态 `Policy`：

    - `version`、`paused`、`evalTimeoutMs` 描述当前状态。
    - `gate(trigger, thread, tickets)` 返回准入 strength 与 basis。
    - `revalidate(mandate)` 检查现有 Mandate 是否仍有效。
    - `admitsInferred(rules)` 仅在 auto 模式且启用匹配规则时允许主动 accept。

    `assessmentOutcome(assessment, settings, strength)` 将模型评估转为 Engine outcome。要求每条启用规则有 judgement、方向 settled、有匹配规则与 intent 证据。非 auto 模式下 inferred accept 记为 “would accept”。

    `policyEffects(tickets, old, now, scope)` 识别变更影响的 open Ticket 及方式。`Engine.policyChanged` 只通知这些 Ticket。`policyVersion` 对 rules、accept 模式与 Source Policy 部分（含 bot 列表）哈希。

### Store

!!! note "原文（English）"

    ### Store

    `Store` (`src/app/store.ts`) owns one SQLite database. Runtime acquires the directory lock before opening it, preventing a second process from migrating or writing the same state.

    Its interfaces are grouped by purpose:

    - **Domain repositories:** `atomic`, plus `triggers`, `threads`, `tickets` and `decisions`, each with `get`, `save` and list queries. Indexed working sets are `threads.active()`, `tickets.open()`, `tickets.active()` and `read.subscribedThreads`.
    - **Read queries:** `read` serves views, tools and Source polls through methods such as `threadTriggers`, `triggerStats`, `search` and `droppedReplies`. These methods never write.
    - **Source metadata:** `threads.describe` and `threads.patchMeta` update Source-owned Thread metadata.
    - **Settings:** `settings`, `saveSettings`, `blockRevision` and `settingsHistory`. Settings and prompts use numbered `Versions` (`src/app/versions.ts`).
    - **Outbox receipts:** `replyStatus` and `receipts`, written only by Outbox. Receipt operations are `get`, `waiting`, `behind`, `add`, `settle` and `withdraw`. `behind` checks whether an earlier message for the same Ticket and conversation is waiting with fewer than three failures. A withdrawn message settles with `channelGone` or `channelIgnored`. Read queries expose failed and dropped replies through `failingReplies`, `droppedReplies` and `droppedSince`.
    - **Typed key-value records:** `get`, `set`, `delete` and `keys`, using entries from `KV`.

    `afterCommit` runs an action only after the transaction commits, never after rollback. Runtime reads records in their stored shape; the schema 11 migration converts older supported records at startup.

!!! quote "译文（中文）"

    ### Store

    `Store`（`src/app/store.ts`）拥有一个 SQLite 库。Runtime 打开前先获目录锁，防止第二进程迁移或写同一 state。

    接口按用途分组：

    - **领域仓库：** `atomic`，以及 `triggers`、`threads`、`tickets`、`decisions`，各有 `get`、`save` 与列表查询。索引工作集为 `threads.active()`、`tickets.open()`、`tickets.active()`、`read.subscribedThreads`。
    - **读查询：** `read` 经 `threadTriggers`、`triggerStats`、`search`、`droppedReplies` 等为视图、工具与 Source 轮询服务，从不写入。
    - **Source 元数据：** `threads.describe`、`threads.patchMeta` 更新 Source 拥有的 Thread 元数据。
    - **Settings：** `settings`、`saveSettings`、`blockRevision`、`settingsHistory`。Settings 与 prompts 用编号 `Versions`（`src/app/versions.ts`）。
    - **Outbox receipt：** `replyStatus`、`receipts`，仅 Outbox 写入。Receipt 操作为 `get`、`waiting`、`behind`、`add`、`settle`、`withdraw`。`behind` 检查同 Ticket 与会话上更早消息是否等待且失败少于三次。withdraw 的消息以 `channelGone` 或 `channelIgnored` settle。读查询经 `failingReplies`、`droppedReplies`、`droppedSince` 暴露失败与丢弃回复。
    - **类型化 KV：** `get`、`set`、`delete`、`keys`，使用 `KV` 条目。

    `afterCommit` 仅在事务提交后运行动作，回滚后不运行。Runtime 按存储形态读记录；schema 11 迁移在启动时将旧支持记录转换。

### Effects

!!! note "原文（English）"

    ### Effects

    `Effects` (`src/app/effects.ts`) is the durable side-effect queue. A command calls `enqueue(kind, key, payload)` inside its transaction; execution begins only after commit.

    Each kind/key pair has one row. Enqueuing the same waiting key replaces its payload and makes it due immediately without changing queue order. `handle(kind, run, options)` registers one handler per kind. It processes due rows oldest first, up to the concurrency limit.

    Failures retry with exponential backoff. Throwing `Later` defers work without counting a failure, for example when no worker slot is available. `start` begins processing after setup, `drain` stops it during shutdown while preserving pending rows, and `pending`/`idle` expose queue state.

    | Kind | Handler | Does | At once | Retry |
    | --- | --- | --- | --- | --- |
    | `assessment` | MasterAssessor | Steers an Eval's candidate card into the main Session's turn, then tells the Engine it was delivered, which starts the Eval's deadline | 4, none while paused | 2 s doubling to 60 s |
    | `launch` | DshExecutor | Launches a pending Assignment: prepare command, Executor Session, assignment message | The free worker slots, none while paused | A passing failure of the model provider or the network runs again after 5 s doubling to 300 s, three times; any other failure, and a failed prepare command, fails its Ticket. One held by a pause or by full worker slots tries again after 5 s |
    | `delivery` | DshExecutor | Hands a Ticket's waiting Inputs to its running Assignment, in order | 4, none while paused | 2 s doubling to 60 s; one that waits for a worker slot tries again after 2 s |
    | `relay` | Sessions | Steers a worker's report or question, or a person's follow-up, into the main Session's turn | 1, in queue order | 2 s doubling to 60 s |
    | `notice` | Sessions | Injects a notice into the main Session without waking it | 1, in queue order | 2 s doubling to 60 s |
    | `reply` | Outbox | Sends a message for a Channel through its Source, after the earlier messages of its Ticket to its conversation that have not failed three times; withdrawing the message cancels it | 1, none while paused | 5 s doubling to 300 s; one whose Source is off, or behind an earlier message, tries again after 5 s |

    Each kind has a health entry under its name. It reports an error once one of its effects failed three times in a row, except `delivery`, whose failures show on their Ticket instead. Relays keep their order, and so do notices; a failed one holds back the later ones of its kind.

!!! quote "译文（中文）"

    ### Effects

    `Effects`（`src/app/effects.ts`）是持久副作用队列。命令在事务内调用 `enqueue(kind, key, payload)`；执行仅在提交后开始。

    每种 kind/key 一行。对已等待的 key 再入队会替换 payload 并立即 due，不改变队列顺序。`handle(kind, run, options)` 每种 kind 注册一个 handler，按最旧优先处理 due 行，直至并发上限。

    失败指数退避重试。抛 `Later` 推迟工作且不计失败，例如无 worker slot。`start` 在 setup 后开始处理；`drain` 在关闭时停止处理但保留 pending 行；`pending`/`idle` 暴露队列状态。

    | Kind | Handler | 作用 | 并发 | 重试 |
    | --- | --- | --- | --- | --- |
    | `assessment` | MasterAssessor | 将 Eval 候选卡片 steer 入主 Session turn，再通知 Engine 已送达，从而开始 Eval deadline | 4，暂停时 0 | 2 s 倍增至 60 s |
    | `launch` | DshExecutor | 启动 pending Assignment：prepare 命令、Executor Session、assignment 消息 | 空闲 worker slot 数，暂停时 0 | 模型/网络瞬时失败 5 s 倍增至 300 s 共三次；其它失败及 prepare 失败则 Ticket 失败。因 pause 或 slot 满而 hold 的 5 s 后再试 |
    | `delivery` | DshExecutor | 按序将 Ticket 等待 Input 交给运行中 Assignment | 4，暂停时 0 | 2 s 倍增至 60 s；等 slot 的 2 s 后再试 |
    | `relay` | Sessions | 将 worker report/提问或人的 follow-up steer 入主 Session turn | 1，队列顺序 | 2 s 倍增至 60 s |
    | `notice` | Sessions | 向主 Session 注入 notice 且不唤醒 | 1，队列顺序 | 2 s 倍增至 60 s |
    | `reply` | Outbox | 经 Source 为 Channel 发消息，须同 Ticket 会话上更早消息未失败三次；withdraw 取消消息 | 1，暂停时 0 | 5 s 倍增至 300 s；Source 关或排在更早消息后的 5 s 后再试 |

    每种 kind 有同名 health 条目。其 effect 连续失败三次报 error，除 `delivery` 失败显示在 Ticket 上。relay 与 notice 保序；失败会阻塞同 kind 后续项。

### Sources

!!! note "原文（English）"

    ### Sources

    Sources extend the host through `SourceKind`, defined in `src/sources/kind.ts`. A kind declares its identity, title, description, configuration `schema` and `fields`, derived `facts`, and `health` entries. Optional behavior includes `scope`, a `channel` (`allowed`, `send`, `link`), `threadContext`, `find`, `policy`, `environment` and `refreshLabels`.

    `SourceRegistry` (`registry.ts`) owns registered kinds and stored instances, and implements the Engine's `SourceScope`. Registration claims health entries on `HealthBoard`; duplicate claims and reserved `sync:` names are refused. A SourceContext writes only through its registration's claimed entries, so reports after unregistration do nothing.

    Registry configuration methods include `kind`, `instances`, `instance`, `validate`, `validatePolicy`, `policyPart`, `channelAllowed`, `declarations`, `environments`, `find` and `scope`. `find` asks each kind that supports search.

    Generic code accesses Source behavior through the registry. `send(message, commands)` calls the kind's Channel and returns `sent`, `gone` or `waiting` if the kind is not installed. `link` resolves its Channel link. `threadContext` pairs a kind's reader with its configuration, or returns nothing when the kind has no reader. Invalid stored configuration disables the instance.

    Plugins receive `SourceHost`: the registry and `context(kind)`. The kind itself uses a narrower `SourceContext`, built by `sourceContextOf` in `src/app/source-context.ts`.

    That context supplies deployment and commands, setup readiness (`ready`, `configured`, `problem`), configuration, `receive`, `atomic`, and typed `state`. It also exposes Thread reads and metadata writes, Ticket reads and binding/closure commands, Trigger reads, `notify` through Outbox, and `report` for declared health entries.

!!! quote "译文（中文）"

    ### Sources

    Source 经 `src/sources/kind.ts` 定义的 `SourceKind` 扩展 host。kind 声明 identity、title、description、配置 `schema` 与 `fields`、派生 `facts`、`health` 条目。可选行为含 `scope`、`channel`（`allowed`、`send`、`link`）、`threadContext`、`find`、`policy`、`environment`、`refreshLabels`。

    `SourceRegistry`（`registry.ts`）拥有已注册 kind 与存盘实例，实现 Engine 的 `SourceScope`。注册在 `HealthBoard` 上 claim health 条目；重复 claim 与保留名 `sync:` 拒绝。SourceContext 仅经其注册 claim 的条目写入，注销后 report 无效。

    Registry 配置方法含 `kind`、`instances`、`instance`、`validate`、`validatePolicy`、`policyPart`、`channelAllowed`、`declarations`、`environments`、`find`、`scope`。`find` 询问支持搜索的各 kind。

    通用代码经 registry 访问 Source 行为。`send(message, commands)` 调用 kind 的 Channel，未安装 kind 时返回 `sent`、`gone` 或 `waiting`。`link` 解析 Channel link。`threadContext` 将 kind reader 与配置配对，无 reader 时返回空。无效存盘配置禁用实例。

    插件接收 `SourceHost`：registry 与 `context(kind)`。kind 自身用更窄的 `SourceContext`，由 `src/app/source-context.ts` 的 `sourceContextOf` 构建。

    该 context 提供 deployment 与 commands、setup 就绪（`ready`、`configured`、`problem`）、配置、`receive`、`atomic`、类型化 `state`。并暴露 Thread 读与元数据写、Ticket 读与 bind/close 命令、Trigger 读、经 Outbox 的 `notify`、以及所声明 health 条目的 `report`。

### DshExecutor

!!! note "原文（English）"

    ### DshExecutor

    `DshExecutor` (`src/executor/dsh.ts`) provides one Session and directory per Ticket. `start` and `deliver` enqueue `launch` and `delivery` effects. `stop` cancels the turn through `Store.afterCommit`, so a rolled-back end does not stop the worker.

    `prepare(ticket, outcome)` supplies the Engine's `prepareTicket` hook: short name, directory and `ticketExecution` assignment text. `workerSessionId(ticket.id)` derives the Session id.

    `WorkerSlots` reserves capacity before a launch or a delivery that wakes an idle worker. A human message can use one slot beyond `maxWorkers`, and wakes have priority over launches. `activeCount` supplies panel load.

    Transient provider and network failures get three retries. Launches use effect backoff; failed turns receive continuation messages after 15, 30 and 60 seconds. `inputMessage` renders each Input from current Ticket state, and `mandateInstructions` adds the matched-rule limits for inferred work.

    `install` subscribes to Session events; `dispose` unsubscribes. A turn end normally reports waiting, except after readiness or while human action is required. Errors report failed. At startup, `resume` reports interrupted Assignments through `assignmentInterrupted` and requeues pending launches that do not wait for a person, oldest Ticket first.

!!! quote "译文（中文）"

    ### DshExecutor

    `DshExecutor`（`src/executor/dsh.ts`）为每 Ticket 提供一个 Session 与目录。`start`、`deliver` 入队 `launch`、`delivery` effect。`stop` 经 `Store.afterCommit` 取消 turn，回滚的 end 不会停 worker。

    `prepare(ticket, outcome)` 供给 Engine 的 `prepareTicket` hook：短名、目录与 `ticketExecution` assignment 文本。`workerSessionId(ticket.id)` 派生 Session id。

    `WorkerSlots` 在 launch 或唤醒 idle worker 的 delivery 前预留容量。人消息可用超出 `maxWorkers` 的一个 slot，唤醒优先于 launch。`activeCount` 供面板负载展示。

    模型/网络瞬时失败重试三次。Launch 用 effect 退避；失败 turn 在 15、30、60 秒后收 continuation 消息。`inputMessage` 从当前 Ticket 状态渲染各 Input，`mandateInstructions` 为 inferred 工作加匹配规则限制。

    `install` 订阅 Session 事件；`dispose` 取消订阅。turn 结束通常报 waiting，ready 后或需人操作时除外。错误报 failed。启动时 `resume` 经 `assignmentInterrupted` 上报中断 Assignment，并重排队不等人 pending launch，最旧 Ticket 优先。

### MasterAssessor

!!! note "原文（English）"

    ### MasterAssessor

    `MasterAssessor` (`src/app/assessment.ts`) lets the main Session assess proactive candidates. `request` queues an assessment effect; `release` drops cached state when its Eval ends.

    Its `context`, `read` and `decide` methods serve `orchestrator_eval`, `orchestrator_eval_read` and `orchestrator_eval_decide`. They provide bounded Thread context through `SourceRegistry.threadContext`, repository context, and decision submission. A kind without a context reader supplies only the Trigger and batch.

    `assessmentOutcome` converts the model's answer for `Engine.decide`. Evals checks inferred acceptance against the current Policy again.

!!! quote "译文（中文）"

    ### MasterAssessor

    `MasterAssessor`（`src/app/assessment.ts`）让主 Session 评估 proactive 候选。`request` 入队 assessment effect；Eval 结束时 `release` 丢弃缓存状态。

    `context`、`read`、`decide` 服务 `orchestrator_eval`、`orchestrator_eval_read`、`orchestrator_eval_decide`。经 `SourceRegistry.threadContext` 提供有界 Thread 上下文、仓库上下文与 decision 提交。无 context reader 的 kind 只提供 Trigger 与 batch。

    `assessmentOutcome` 将模型答案转为 `Engine.decide` 输入。Evals 再次对照当前 Policy 检查 inferred accept。

### Settings

!!! note "原文（English）"

    ### Settings

    `Settings` (`src/app/settings.ts`) reads and writes revisioned settings blocks. `current` and `view` read them; `save(block, value, revision, origin)` validates a save; `pause(paused, origin)` changes the separate `paused` setting.

    Every save declares its origin: `panel`, `tool` or `sync`. Only `panel` can change the execution environment or fields listed in `SETUP_FIELDS` (`src/deployment.ts`).

    A Policy-version change calculates `policyEffects`, records which Tickets were notified, and calls `Engine.policyChanged`. `syncable` and `sync(id)` refresh a Source's Policy part. `refreshLabels(id)` refreshes display names.

!!! quote "译文（中文）"

    ### Settings

    `Settings`（`src/app/settings.ts`）读写带 revision 的 settings 块。`current`、`view` 读取；`save(block, value, revision, origin)` 校验保存；`pause(paused, origin)` 改独立的 `paused` 设置。

    每次 save 声明 origin：`panel`、`tool` 或 `sync`。仅 `panel` 可改执行环境或 `SETUP_FIELDS`（`src/deployment.ts`）所列字段。

    Policy 版本变更计算 `policyEffects`、记录已通知 Ticket 并调用 `Engine.policyChanged`。`syncable` 与 `sync(id)` 刷新 Source Policy 部分。`refreshLabels(id)` 刷新显示名。

### Prompts

!!! note "原文（English）"

    ### Prompts

    `Prompts` (`src/app/prompts.ts`) stores configurations as `Versions`. `current`, `version` and `history` read them; `validate`, `preview` and `save(value, baseRevision, source)` check and save them. `compose` and `skills` build Session text.

    `builtInParts` lists override keys, roles, default text and required template placeholders. Each override stores team `text` and a `base`: the `builtInBase` key of its original default. Saves preserve that default under `KV.prompts.builtIn`. `defaultChanged` then reports changed defaults, their old text when available, and removed parts.

    Validation inherits an omitted base from the saved override, drops removed parts already present in saved data, and rejects blank text or other unknown keys. This allows the save tool to round-trip a configuration it read. `environmentSection` and `policySection` substitute template values once, using own keys only. Configurations without overrides use defaults.

!!! quote "译文（中文）"

    ### Prompts

    `Prompts`（`src/app/prompts.ts`）以 `Versions` 存配置。`current`、`version`、`history` 读取；`validate`、`preview`、`save(value, baseRevision, source)` 校验并保存。`compose`、`skills` 构建 Session 文本。

    `builtInParts` 列出 override key、role、默认文本与必需模板占位。各 override 存团队 `text` 与 `base`（原默认的 `builtInBase` key）。保存时在 `KV.prompts.builtIn` 保留该默认。`defaultChanged` 报告变更的默认、可用时的旧文本与移除的 part。

    校验从已存 override 继承省略的 base，丢弃 saved 数据中已存在的移除 part，拒绝空白文本或其它未知 key，使 save 工具可 round-trip 所读配置。`environmentSection`、`policySection` 仅用自有 key 替换模板值一次。无 override 的配置用默认。

### Sessions

!!! note "原文（English）"

    ### Sessions

    `Sessions` (`src/app/sessions.ts`) manages DeepSeek Harness Sessions for both roles. `ensure` creates a Session with its role's model. `install` follows agent events and registers prompts, skills and tools for managed Sessions.

    `prompt` delivers once per request id. `relay` and `notify` enqueue main-Session messages handled by the `relay` and `notice` effects. `src/app/notices.ts` defines their content; `delivery` checks whether a message arrived.

    Each model request binds the Session to the current prompt revision. The revisions it used are stored durably: `promptVersion` identifies the active one and `mayRead` restricts which versions a worker can inspect. Registrations are tracked in one map per agent.

    `status` and `promptVersion` serve prompt pages. Saves go through `Prompts.save` and notify the main Session. `modelRetry` exposes model-budget retry state.

!!! quote "译文（中文）"

    ### Sessions

    `Sessions`（`src/app/sessions.ts`）管理两角色的 DeepSeek Harness Session。`ensure` 用角色模型创建 Session。`install` 跟随 agent 事件并为托管 Session 注册 prompts、skills、tools。

    `prompt` 每个 request id 只投递一次。`relay`、`notify` 入队由 `relay`、`notice` effect 处理的主 Session 消息。内容定义于 `src/app/notices.ts`；`delivery` 检查消息是否到达。

    每次模型请求将 Session 绑定当前 prompt revision；所用 revision 持久存储：`promptVersion` 标识活跃 revision，`mayRead` 限制 worker 可查看的版本。每 agent 一张 map 跟踪注册。

    `status`、`promptVersion` 服务 prompt 页。保存经 `Prompts.save` 并通知主 Session。`modelRetry` 暴露模型预算重试状态。

### Glue

!!! note "原文（English）"

    ### Glue

    | Glue | Translates |
    | --- | --- |
    | `src/app/runtime.ts` | The composition root: builds the parts with the interfaces they use, registers the routes and the Source host, and starts and stops dsh-superagent |
    | `src/app/notices.ts` | Domain events and worker reports into notices and relays for the main Session |
    | `src/app/tools/main.ts`, `src/app/tools/executor.ts` | Tool arguments of the main Session and the Executor Sessions into calls of the core classes, and their results into cards |
    | `src/app/http.ts`, `src/app/views/`, `src/app/api.ts` | HTTP requests into calls, and Store reads into the panel's views; `api.ts` types every route for the panel |
    | `src/app/resources.ts`, `src/app/links.ts`, `src/app/cards.ts` | Resource names into reads, source messages into links, records into compact cards for models |
    | `src/sources/github*.ts`, `src/sources/feishu*.ts`, `src/sources/poll.ts` | GitHub and Feishu payloads into Triggers and Thread facts, and messages into CLI calls |
    | `src/deployment.ts`, `src/setup.ts`, `src/infra/` | Setup probes and problems, CLI transport, the agent's process environment |

!!! quote "译文（中文）"

    ### Glue

    | 胶水 | 翻译 |
    | --- | --- |
    | `src/app/runtime.ts` | 组合根：用各部件所需接口组装、注册路由与 Source host、启停 dsh-superagent |
    | `src/app/notices.ts` | 领域事件与 worker report → 主 Session 的 notice 与 relay |
    | `src/app/tools/main.ts`, `src/app/tools/executor.ts` | 主 Session 与 Executor Session 工具参数 → 核心类调用，结果 → 卡片 |
    | `src/app/http.ts`, `src/app/views/`, `src/app/api.ts` | HTTP 请求 → 调用，Store 读 → 面板视图；`api.ts` 为面板类型化每条路由 |
    | `src/app/resources.ts`, `src/app/links.ts`, `src/app/cards.ts` | 资源名 → 读，Source 消息 → 链接，记录 → 模型用紧凑卡片 |
    | `src/sources/github*.ts`, `src/sources/feishu*.ts`, `src/sources/poll.ts` | GitHub/飞书载荷 → Trigger 与 Thread 事实，消息 → CLI 调用 |
    | `src/deployment.ts`, `src/setup.ts`, `src/infra/` | Setup 探测与问题、CLI 传输、agent 进程环境 |

## Dependency rules

!!! note "原文（English）"

    ## Dependency rules

    - **Explicit, narrow dependencies.** Each class receives an interface that names what it uses, mostly as `Pick` types of other classes: `ExecutorHost`, `AssessorHost`, `SessionsHost`, `SettingsHost`, `SetupHost`, `PassesHost` and `OutboxHost` for the core classes, and `HttpHost`, `MainToolsHost`, `ExecutorToolsHost`, `ResourceHost`, `LinkHost` and `SourceContextDeps` for the glue, with the Engine as the `Pick` of the role that calls it. The Runtime passes itself only behind such an interface. A Source plugin injects the `orchestrator` service, which its type names as a `SourceHost`: the registry and `context(kind)`; its kind's code sees only its `SourceContext`.
    - **What may import what.** `src/core` imports nothing outside itself apart from Node's hashing, and does no I/O. `src/sources` imports nothing from `src/app`. The panel imports only types from the host side, and the plain values of `src/admission.ts`, `src/limits.ts`, `src/setup.ts`, `src/navigation.ts`, `src/sources/host.ts`, `src/core/errors.ts`, `src/core/reasons.ts` and the filter lists of `src/app/api.ts`.
    - **One writer per state.** Each kind of state has one writer, and the others read it or ask that writer.

      | State | Writer |
      | --- | --- |
      | Triggers, Threads, Decisions, Tickets, Assignments | Engine |
      | A Source's read positions, and the meta it keeps on its Threads (`label`, `url`, `facts` and its own keys) | That Source, through `SourceContext` |
      | Settings and their history | Settings |
      | Prompt versions | Prompts |
      | Effect rows | Effects |
      | Outbox receipts | The Outbox: the Engine's messages and Sources' `notify` go through its `send`, and its `reply` effects mark them |
      | A health entry | The part that claimed it on the `HealthBoard` (`src/infra/health.ts`), which refuses a second claim: `runtime` the Runtime, which Passes report through, `evals` the Runtime after each pass, `models` Sessions, each effect kind Effects, `worker` the DshExecutor, a Source kind's entries the SourceRegistry for that kind's Source, `sync:<id>` Settings |

      A Source changes a Ticket only through `SourceContext.tickets.bindThread` and `threadClosed`, which call the Engine.
    - **Requests and facts.** A method that asks for something is named by an imperative verb and its object, such as `stopTicket` or `bindThread`, and may be refused; a method that records what an adapter did is named in the past tense, such as `assignmentStarted` or `deliveryFailed`. Each host receives the Engine as the role type it needs.
    - **Typed fields, and Source data under the Source.** The code reads typed fields of Tickets and Mandates. A Source keeps nothing on a Ticket: which of its Threads a Ticket follows is the Ticket's `follows`, and what it keeps about a Trigger or a Thread is in their `meta`.
    - **Fixed refusals.** A refusal a person or an agent can cause is a `UserError(code, values)` from `USER_ERRORS` in `src/core/errors.ts`. The management API answers a `missing` code with 404 and the others with 400, and the panel words each code in its language. Setup problems are `SetupError` codes (`src/setup.ts`) and fixed health texts `HealthCode`s (`src/sources/host.ts`), worded the same way.

    ### Source-specific dependencies

    Some generic modules still refer directly to GitHub or Feishu:

    - Outbox applies the Feishu chat word limit.
    - `src/app/links.ts` resolves Feishu message links and `repliedTo`. It and Runtime read settings with `feishuSettings`.
    - `src/app/resources.ts` reads the `feishu/` and `github/` originals; `src/app/cards.ts` uses `pullOf` for Ticket cards.
    - Runtime sends `pullEndNotices` for merged or closed Tickets.
    - Executor tools bind reported pull requests with `readPull` and `claimPull`, using the GitHub context exposed by Runtime.
    - MasterAssessor reads repository files with `readRepositoryPath`.
    - Sessions and Prompts register built-in skills only for GitHub and Feishu. Migration converts their schema-10 records.
    - Setup probes only GitHub and Feishu account fields (`SETUP_FIELDS`). Only those Sources receive account setup problems through `SourceContext.problem`.

!!! quote "译文（中文）"

    ## 依赖规则

    - **显式、窄依赖。** 各类接收命名所用能力的接口，多为其它类的 `Pick`：核心类用 `ExecutorHost`、`AssessorHost`、`SessionsHost`、`SettingsHost`、`SetupHost`、`PassesHost`、`OutboxHost`；胶水用 `HttpHost`、`MainToolsHost`、`ExecutorToolsHost`、`ResourceHost`、`LinkHost`、`SourceContextDeps`；Engine 为调用方角色的 `Pick`。Runtime 仅经此类接口传递自身。Source 插件注入 `orchestrator`，类型为 `SourceHost`（registry 与 `context(kind)`）；kind 代码只见其 `SourceContext`。
    - **谁可 import 谁。** `src/core` 除 Node 哈希外不 import 自身外任何模块，不做 I/O。`src/sources` 不 import `src/app`。面板只 import host 侧类型，以及 `src/admission.ts`、`src/limits.ts`、`src/setup.ts`、`src/navigation.ts`、`src/sources/host.ts`、`src/core/errors.ts`、`src/core/reasons.ts` 与 `src/app/api.ts` 过滤列表的 plain 值。
    - **每种状态单一写入者。** 其它部分只读或向写入者请求。

      | 状态 | 写入者 |
      | --- | --- |
      | Trigger、Thread、Decision、Ticket、Assignment | Engine |
      | Source 读位置及其在 Thread 上的 meta（`label`、`url`、`facts` 等） | 该 Source，经 `SourceContext` |
      | Settings 及历史 | Settings |
      | Prompt 版本 | Prompts |
      | Effect 行 | Effects |
      | Outbox receipt | Outbox：Engine 消息与 Source `notify` 经其 `send`，`reply` effect 标记 |
      | health 条目 | 在 `HealthBoard`（`src/infra/health.ts`）上 claim 的部件，拒绝二次 claim：`runtime` Runtime（Passes 经其上报）、`evals` 每 pass 后的 Runtime、`models` Sessions、各 effect kind Effects、`worker` DshExecutor、Source kind 条目 SourceRegistry、`sync:<id>` Settings |

      Source 仅经 `SourceContext.tickets.bindThread` 与 `threadClosed`（调用 Engine）改 Ticket。
    - **请求与事实。** 请求用祈使动词+宾语如 `stopTicket`、`bindThread`，可被拒绝；记录适配器行为用过去式如 `assignmentStarted`、`deliveryFailed`。各 host 收到所需角色类型的 Engine。
    - **类型化字段，Source 数据归 Source。** 代码读 Ticket、Mandate 的类型化字段。Source 不在 Ticket 上存东西：Ticket follow 哪些 Thread 在 Ticket 的 `follows`；Trigger/Thread 相关信息在其 `meta`。
    - **固定拒绝。** 人或 agent 可导致的拒绝为 `src/core/errors.ts` 中 `USER_ERRORS` 的 `UserError(code, values)`。管理 API 对 `missing` 回 404，其它 400；面板按语言措辞各 code。Setup 问题为 `SetupError`（`src/setup.ts`），固定 health 文案为 `HealthCode`（`src/sources/host.ts`），措辞方式相同。

    ### Source 特有依赖

    部分通用模块仍直接引用 GitHub 或飞书：

    - Outbox 应用飞书 chat 字数限制。
    - `src/app/links.ts` 解析飞书消息链接与 `repliedTo`；它与 Runtime 用 `feishuSettings` 读设置。
    - `src/app/resources.ts` 读 `feishu/`、`github/` 原文；`src/app/cards.ts` 用 `pullOf` 做 Ticket 卡片。
    - Runtime 对 merged/closed 结束的 Ticket 发 `pullEndNotices`。
    - Executor 工具用 Runtime 暴露的 GitHub context 经 `readPull`、`claimPull` 绑定上报的 PR。
    - MasterAssessor 用 `readRepositoryPath` 读仓库文件。
    - Sessions、Prompts 仅为 GitHub、飞书注册内置 skill。迁移转换 schema-10 记录。
    - Setup 只探测 GitHub、飞书账号字段（`SETUP_FIELDS`）。仅这些 Source 经 `SourceContext.problem` 收账号 setup 问题。

## Modules

!!! note "原文（English）"

    ## Modules

    | Path | Responsibility |
    | --- | --- |
    | `src/index.ts` | The main plugin entry: reads the machine settings, starts the Runtime, and gives it the restart that a save of a Source's account uses |
    | `src/deployment.ts` | The plugin `Config` and `Machine`; `resolveDeployment` and `resolveFeishuBot` read the repository and the accounts from the Source settings, each CLI run as Executor shells run it; `SETUP_FIELDS` lists the Source fields setup reads. The code names no person, account or organization |
    | `src/setup.ts` | Setup problems: `SetupError`, with a code, the place that fixes it and the English text the log, the main Session and the panel share |
    | `src/policy.ts` | The Policy: the settings schemas by block, the Policy version, the gate and Mandate revalidation |
    | `src/admission.ts` | The admission table's values, each cell's options and defaults, shared with the panel |
    | `src/limits.ts` | The limits of the values a person sets, which the settings schemas check and the panel's fields offer |
    | `src/kv.ts` | `KV`: every family of `kv` records with its key, its value's type and its owner, and the `Key` and `Family` types the Store and `SourceContext.state` take |
    | `src/navigation.ts` | Opens a DeepSeek Harness Session from the panel once the host's catalog lists it |
    | `src/core/model.ts` | The domain types, among them the typed Ticket and `MandateBasis`, with `threadKey`, `ticketOutcome`, `reached`, `followupOwners`, `settled`, `waitsForPerson`, `liveTicket`, `runningAssignment`, `latestAssignment` and `latestReport` |
    | `src/core/ports.ts` | The ports: `Repositories`, `Policy`, `SourceScope`, `Assessor`, `Executor`, `Outbox`, and the domain events |
    | `src/core/engine.ts` | The Engine, the core's entry point, and the role interfaces `EngineForSources`, `EngineForMainSession`, `EngineForPeople`, `EngineForSettings` and `EngineForExecutor` |
    | `src/core/part.ts` | `EnginePart`, the base of the Engine's parts, and `EngineOptions` |
    | `src/core/intake.ts` | `Intake`: Triggers from Sources, to their Tickets and, unless a person's message reached one, to Eval |
    | `src/core/evals.ts` | `Evals`: the Evals of Threads and their Decisions |
    | `src/core/tickets.ts` | `Tickets`: the Ticket lifecycle, the Threads a Ticket follows, Subscriptions and Inputs |
    | `src/core/assignments.ts` | `Assignments`: the Executor's facts and its workers' Reports |
    | `src/core/replies.ts` | `Replies`: the Engine's Channel writes |
    | `src/core/stage.ts` | `ticketStage` and `canRetry`: the Ticket stages of [003 Execution](proto-003-execution.md) |
    | `src/core/errors.ts` | `UserError` and `USER_ERRORS` |
    | `src/core/reasons.ts` | `Reason`, `REASONS` and `reasonText`: why the core recorded a Decision or set a Trigger aside, by code |
    | `src/core/ids.ts` | `key`: ids derived from their inputs, equal for equal inputs |
    | `src/core/memory.ts` | `MemoryRepositories`: the Store's repositories in memory, in the Store's order and with its nested rollback, for domain tests |
    | `src/core/index.ts` | The core's exports |
    | `src/app/runtime.ts` | The Runtime, the composition root, with its start and stop |
    | `src/app/setup.ts` | Setup, and `setupChanged`, whether a save changes a field setup reads |
    | `src/app/passes.ts` | Passes, the pass scheduler |
    | `src/app/outbox.ts` | The Outbox: the Engine's `Outbox` port, the receipts and the `reply` effects that send them |
    | `src/app/notices.ts` | The main Session's notices and relays: `eventNotice`, `reportRelay`, `resultRelay` and `followupRelay` |
    | `src/app/store.ts` | The Store, and `Receipt`, a message of the outbox |
    | `src/app/store-read.ts` | `storeReads`: the Store's read queries for the views, the tools and the Sources' polls, which never write |
    | `src/app/migration.ts` | The schema 11 migration: one step from schema 10 that writes every stored record in its current shape |
    | `src/app/effects.ts` | Effects |
    | `src/app/versions.ts` | `Versions`: a record kept as numbered snapshots with a pointer to the current one, for the settings and the prompts |
    | `src/app/cache.ts` | `TextCache`: texts kept in memory within a size, the least recently used dropped first |
    | `src/app/settings.ts` | Settings |
    | `src/app/settings-history.ts` | `policyHistory`: each saved settings revision with what it changed from the one before |
    | `src/app/prompts.ts` | Prompts, `builtInParts`, the built-in parts a team may override, and `promptParts`, the order of a role's prompt sections |
    | `src/app/context.ts` | The built-in prompt text, read from `prompts/`, and the transcript entries Sessions read |
    | `src/app/skill-files.ts` | Packaged text: `promptFile`, which joins a file's one-sentence lines into paragraphs, and the bundled skills |
    | `src/app/sessions.ts` | Sessions |
    | `src/app/delivery.ts` | `AgentDelivery`: delivers a message to a DeepSeek Harness agent once per request id, also across a restored Session |
    | `src/app/retry-health.ts` | `RetryHealth`: which Sessions wait on a model budget retry, for the `models` health entry |
    | `src/app/role-models.ts` | Resolves the Eval and Executor models, and seeds a new Session with its role's model |
    | `src/app/assessment.ts` | The MasterAssessor, the assessment schema and the window `orchestrator_eval` answers |
    | `src/app/execution-environment.ts` | The execution environment: its variables, a Ticket's values, the directory root and the lines of the prompt section, which `src/app/prompts.ts` fills into its template |
    | `src/app/source-context.ts` | `sourceContextOf`: each Source kind's `SourceContext` |
    | `src/app/links.ts` | `sourceLink`, the page of a Trigger's message, and `candidateCard` |
    | `src/app/cards.ts` | Compact cards of Triggers, Tickets, Threads and Decisions for models, the closed set of ticket card states `TICKET_STATES`, and `textPage` |
    | `src/app/resources.ts` | `RESOURCES` and `readResource`: what `orchestrator_read` reads, for each role |
    | `src/app/tools/tool.ts` | The `Tool` type, `readTool`, the `orchestrator_read` of both roles, and `AcknowledgementSchema` |
    | `src/app/tools/main.ts` | The main Session's tools |
    | `src/app/tools/executor.ts` | An Executor Session's tools |
    | `src/app/http.ts` | The `/orchestrator/api` routes: the same-origin check, request limits and one status per kind of failure |
    | `src/app/api.ts` | `Api`, every route with its parameters, query, body and response, and the filters of the lists; it imports only types, so the panel bundles it |
    | `src/app/views/text.ts` | The two forms of a view's text: `preview`, plain text on one line, and `markdown` without HTML; `plain` and `readable` |
    | `src/app/views/records.ts` | The rows the views share: Triggers, Tickets, Threads, Decisions and Channel links, and `ownersOf`, the owners of follow-ups as one request reads them |
    | `src/app/views/tickets.ts` | The Ticket lists by stage, a Ticket's page, the Mandate basis facts and the Executor's load |
    | `src/app/views/threads.ts` | The Thread lists, a Thread's timeline, the search and the Decision list |
    | `src/app/views/sources.ts` | The Source status: each Source instance's state, its last day's counts, and what it filtered and dropped in that day |
    | `src/app/views/settings.ts` | The settings page's view: each block with its revision, each Source's settings with its kind's form, and the execution environment |
    | `src/app/views/overview.ts` | The overview: Tickets that need a person, problems, the last day's flow and the Executor's load |
    | `src/executor/dsh.ts` | The DshExecutor, `WorkerSlots`, `ticketExecution`, `inputMessage` and `mandateInstructions` |
    | `src/executor/background.ts` | A worker's background work in DeepSeek Harness, the shell jobs its Session owns (`ctx.jobs`) and its subagents (`ctx.subagents`): `runningJobs`, `untilQuiet`, `killJobs`, `collectJobs`, `stopSubagents`, `stopBackground`, `until`, and `cancelled`, the workers whose turn a stop of their subagents cancelled |
    | `src/infra/commands.ts` | `Commands`, the CLI transport: `run`; `api` and `pages`, limited to the configured repository and validated; `lark` as the bot; `git`. `CliError` carries the code a command's output reports, such as a GitHub HTTP status, and a command that prints nothing fails with how it ended, never with its arguments; `validItems` skips and counts the malformed items of a list |
    | `src/infra/environment.ts` | The agent's process environment: command shims, the git configuration and credential helper, `runAsWorker`, and the checks of the environment and of a Ticket's directory |
    | `src/infra/paths.ts` | The package's root, where packaged files are |
    | `src/infra/text.ts` | Word counts and the chat message limit, `fill` and `sameName` |
    | `src/infra/health.ts` | The `HealthBoard`, every part's health entry, each claimed by its one writer |
    | `src/sources/kind.ts` | The `SourceKind` protocol |
    | `src/sources/registry.ts` | `SourceRegistry` |
    | `src/sources/host.ts` | `SourceHost`, `SourceContext`, `Health`, `health` and the fixed health texts |
    | `src/sources/poll.ts` | What GitHub and Feishu polling share: `POLL_HORIZON_MS`, `polled`, `cursorAt`, and `pollThreads`, which keeps a failure on its Thread as `pollError` |
    | `src/sources/github.ts` | The GitHub kind and plugin entry, exported as `./github-watch`: a poll round of notifications, the pull requests of open Tickets with their CI, and subscribed Threads |
    | `src/sources/github-trigger.ts` | GitHub comments, reviews, Issue bodies and CI changes as Triggers, and GitHub addresses |
    | `src/sources/github-pull.ts` | The pull request rules: which pull requests a Ticket follows (`claimPull`, `observePull`), the closes it reports to the Engine, and `pullEndNotices`, the chat notice of a Ticket that ended merged or closed |
    | `src/sources/github-channel.ts` | The GitHub Channel: comments and review thread replies, `sent` or `gone` |
    | `src/sources/github-read.ts` | CI snapshots, repository reads, the `github/` resources, and `githubThreadContext`: an Issue's or pull request's conversation by page |
    | `src/sources/feishu.ts` | The Feishu kind and plugin entry, exported as `./feishu-watch` |
    | `src/sources/feishu-trigger.ts` | Feishu messages as Triggers, by one rule for live events, history pages and topic pages |
    | `src/sources/feishu-loops.ts` | The Feishu loops: the event consumer, history recovery, chat names and polling of subscribed topics |
    | `src/sources/feishu-channel.ts` | The Feishu Channel: topic replies, `sent` or `gone` |
    | `src/sources/feishu-read.ts` | The `feishu/` reads of a Trigger's message, its root, parent and topic, and `feishuThreadContext`, which gives them to assessments with their images |
    | `src/sources/admission-context.ts` | The bounded window an assessment starts from: the first pages of a Trigger's Thread, read through its kind, and the messages around the Trigger |
    | `src/sources/feishu-image.ts` | Reads a Feishu message's image as an attachment for the assessing Session |
    | `src/client/index.tsx` | The client plugin entry: the sidebar entry, the main panel and the page in DeepSeek Harness's Plugin Manager |
    | `src/client/app.tsx` | The panel: header, search, glossary, tabs and the page of the current route |
    | `src/client/provider.tsx` | What every page reads from its host: copy, calls, navigation, notices, the state poll, and `restart`, which reads the state quickly while dsh-superagent restarts |
    | `src/client/api.ts` | Calls and polls of the management API, typed by `src/app/api.ts`, and the Decision filters of the Eval status |
    | `src/client/route.ts` | The tabs, the settings groups, the typed routes that `parseRoute` reads, and the route memory with the settings group opened last |
    | `src/client/overview.tsx` | The overview |
    | `src/client/source.tsx`, `trigger.tsx`, `eval.tsx`, `execution.tsx` | One file per stage: its status, and its settings where it has some |
    | `src/client/thread.tsx`, `ticket.tsx` | The record pages: a Thread with its timeline, and a Ticket with its authorization, Inputs and Assignments |
    | `src/client/settings.tsx` | The Settings tab: the group navigation and the group on screen |
    | `src/client/settings-kit.tsx` | The building blocks of settings blocks: the settings the stage groups share, `useBlockDraft`, the fields, among them a Source kind's, and the history of saves |
    | `src/client/prompts.tsx` | The Prompts settings: the page owns the view it read and its editor the draft, and the Edit, Full prompt and History tabs render from them; a built-in part's editor shows a changed default and keeps or drops the override |
    | `src/client/prompt-config.ts` | The prompt configuration by section, by the team sections, skill names, built-in parts (`part/<key>`, with `withOverride`) and part headings the prompt views give, and the line diff of the full prompt, as pure functions |
    | `src/client/deployment.tsx` | The Deployment settings, also shown in DeepSeek Harness's Plugin Manager |
    | `src/client/draft.tsx` | Settings drafts: `draftReducer`, their states and their merge with a newer saved revision, `saveBarText`, the save bar's line, and the save bar |
    | `src/client/controls.tsx` | Local copies of DeepSeek Harness's controls, which follow its markup, keyboard behaviour and design tokens, since the bundle does not import `@deepseek-ai/dsh-client-ui-primitives`; they read no context |
    | `src/client/widgets.tsx` | The small components the pages share, which read the panel's context: sections, page heads, load states, times, links, confirmed actions, the setup check and Markdown |
    | `src/client/rows.tsx` | The rows the pages share: a Ticket, a Thread and a Decision, a Thread's chip, and the folding of runs of automated messages |
    | `src/client/markdown.ts` | The Markdown subset of reports and questions, and the bare addresses in messages, as blocks and inline parts |
    | `src/client/text.ts` | Copy by key and the wording of domain values, reasons, senders and times |
    | `src/client/marks.ts` | The marks of the states the views classify: a Ticket's or a Thread's stage, a Source's state and a Decision's outcome, each as a dot, an icon, a tone and a label |
    | `src/client/locales.ts` | All copy in both languages, and the glossary of [001 Domain model](proto-001-domain.md) |
    | `src/client/client.css`, `css.d.ts` | The panel's stylesheet, loaded as text |

    `src/sources/github.ts` and `src/sources/feishu.ts` are also the entries of the two Source plugins, built by `tsdown.config.ts` and exported in `package.json` as `./github-watch` and `./feishu-watch`. The bundle's `cordis.patch.yml` inserts the three plugins, with Feishu disabled.

!!! quote "译文（中文）"

    ## 模块

    | Path | 职责 |
    | --- | --- |
    | `src/index.ts` | 主插件入口：读机器设置、启动 Runtime，并提供 Source 账号保存所用的 restart |
    | `src/deployment.ts` | 插件 `Config` 与 `Machine`；`resolveDeployment`、`resolveFeishuBot` 从 Source 设置读仓库与账号，CLI 调用方式同 Executor shell；`SETUP_FIELDS` 列出 setup 读取的 Source 字段。代码不命名具体的人、账号或组织 |
    | `src/setup.ts` | Setup 问题：`SetupError`，含 code、修复位置，以及日志、主 Session 与面板共用的英文文案 |
    | `src/policy.ts` | Policy：按块的 settings schema、Policy 版本、gate 与 Mandate revalidation |
    | `src/admission.ts` | 准入表的值、每格选项与默认，与面板共享 |
    | `src/limits.ts` | 人所设值的限制，settings schema 校验、面板字段提供 |
    | `src/kv.ts` | `KV`：每族 `kv` 记录的 key、值类型、owner，以及 Store 与 `SourceContext.state` 使用的 `Key`、`Family` 类型 |
    | `src/navigation.ts` | host catalog 列出后从面板打开 DeepSeek Harness Session |
    | `src/core/model.ts` | 领域类型，含类型化 Ticket、`MandateBasis`，以及 `threadKey`、`ticketOutcome`、`reached`、`followupOwners`、`settled`、`waitsForPerson`、`liveTicket`、`runningAssignment`、`latestAssignment`、`latestReport` |
    | `src/core/ports.ts` | 端口：`Repositories`、`Policy`、`SourceScope`、`Assessor`、`Executor`、`Outbox` 与领域事件 |
    | `src/core/engine.ts` | Engine、核心入口，以及 `EngineForSources`、`EngineForMainSession`、`EngineForPeople`、`EngineForSettings`、`EngineForExecutor` |
    | `src/core/part.ts` | `EnginePart`（Engine 部件基类）与 `EngineOptions` |
    | `src/core/intake.ts` | `Intake`：Source 的 Trigger 到 Ticket，除非人消息已到达某 Ticket，否则到 Eval |
    | `src/core/evals.ts` | `Evals`：Thread 上的 Eval 及其 Decision |
    | `src/core/tickets.ts` | `Tickets`：Ticket 生命周期、follow 的 Thread、Subscription 与 Input |
    | `src/core/assignments.ts` | `Assignments`：Executor 事实与 worker Report |
    | `src/core/replies.ts` | `Replies`：Engine 的 Channel 写入 |
    | `src/core/stage.ts` | `ticketStage` 与 `canRetry`：[003 执行](proto-003-execution.md) 的 Ticket 阶段 |
    | `src/core/errors.ts` | `UserError` 与 `USER_ERRORS` |
    | `src/core/reasons.ts` | `Reason`、`REASONS`、`reasonText`：核心记录 Decision 或搁置 Trigger 的原因（按 code） |
    | `src/core/ids.ts` | `key`：由输入派生的 id，相同输入则相同 |
    | `src/core/memory.ts` | `MemoryRepositories`：内存中 Store 仓库，顺序与嵌套回滚同 Store，供领域测试 |
    | `src/core/index.ts` | core 导出 |
    | `src/app/runtime.ts` | Runtime 组合根及 start/stop |
    | `src/app/setup.ts` | Setup 与 `setupChanged`（保存是否改变 setup 读取字段） |
    | `src/app/passes.ts` | Passes，pass 调度 |
    | `src/app/outbox.ts` | Outbox：Engine `Outbox` 端口、receipt 与发送它们的 `reply` effect |
    | `src/app/notices.ts` | 主 Session notice 与 relay：`eventNotice`、`reportRelay`、`resultRelay`、`followupRelay` |
    | `src/app/store.ts` | Store 与 outbox 消息 `Receipt` |
    | `src/app/store-read.ts` | `storeReads`：视图、工具、Source 轮询用的 Store 读查询，从不写入 |
    | `src/app/migration.ts` | schema 11 迁移：自 schema 10 一步，将所有存盘记录写成当前形态 |
    | `src/app/effects.ts` | Effects |
    | `src/app/versions.ts` | `Versions`：编号快照与当前指针，用于 settings 与 prompts |
    | `src/app/cache.ts` | `TextCache`：有界内存文本，LRU 淘汰 |
    | `src/app/settings.ts` | Settings |
    | `src/app/settings-history.ts` | `policyHistory`：每次 settings revision 相对前一版的变更 |
    | `src/app/prompts.ts` | Prompts、`builtInParts`（团队可覆盖的内置部分）、`promptParts`（角色 prompt 段顺序） |
    | `src/app/context.ts` | 自 `prompts/` 读的内置 prompt 文本，以及 Sessions 读的 transcript 条目 |
    | `src/app/skill-files.ts` | 打包文本：`promptFile`（单行句合并为段）与捆绑 skills |
    | `src/app/sessions.ts` | Sessions |
    | `src/app/delivery.ts` | `AgentDelivery`：每个 request id 向 DeepSeek Harness agent 投递一次，恢复 Session 后亦然 |
    | `src/app/retry-health.ts` | `RetryHealth`：哪些 Session 在等模型预算重试，供 `models` health |
    | `src/app/role-models.ts` | 解析 Eval/Executor 模型，并用角色模型 seed 新 Session |
    | `src/app/assessment.ts` | MasterAssessor、assessment schema 与 `orchestrator_eval` 窗口答案 |
    | `src/app/execution-environment.ts` | 执行环境：变量、Ticket 值、目录根、prompt 段行（由 `src/app/prompts.ts` 填入模板） |
    | `src/app/source-context.ts` | `sourceContextOf`：各 Source kind 的 `SourceContext` |
    | `src/app/links.ts` | `sourceLink`、Trigger 消息页、`candidateCard` |
    | `src/app/cards.ts` | Trigger/Ticket/Thread/Decision 紧凑卡片、`TICKET_STATES`、`textPage` |
    | `src/app/resources.ts` | `RESOURCES` 与 `readResource`：各角色 `orchestrator_read` 所读 |
    | `src/app/tools/tool.ts` | `Tool` 类型、`readTool`、两角色 `orchestrator_read`、`AcknowledgementSchema` |
    | `src/app/tools/main.ts` | 主 Session 工具 |
    | `src/app/tools/executor.ts` | Executor Session 工具 |
    | `src/app/http.ts` | `/orchestrator/api` 路由：同源检查、请求限制、各类失败的状态 |
    | `src/app/api.ts` | `Api`：每条路由的参数、query、body、response 与列表过滤；仅 import 类型供面板打包 |

    | `src/app/views/text.ts` | 视图文本两形态：`preview`（单行纯文本）与无 HTML 的 `markdown`；`plain` 与 `readable` |
    | `src/app/views/records.ts` | 视图共用行：Trigger、Ticket、Thread、Decision、Channel 链接；`ownersOf` 一次请求读 follow-up 归属 |
    | `src/app/views/tickets.ts` | 按阶段的 Ticket 列表、Ticket 页、Mandate basis 事实、Executor 负载 |
    | `src/app/views/threads.ts` | Thread 列表、时间线、搜索、Decision 列表 |
    | `src/app/views/sources.ts` | Source 状态：实例状态、近一日计数、过滤与丢弃 |
    | `src/app/views/settings.ts` | 设置页视图：各块 revision、各 Source 设置与 kind 表单、执行环境 |
    | `src/app/views/overview.ts` | 概览：需人的 Ticket、问题、近一日流量、Executor 负载 |
    | `src/executor/dsh.ts` | DshExecutor、`WorkerSlots`、`ticketExecution`、`inputMessage`、`mandateInstructions` |
    | `src/executor/background.ts` | worker 在 DSH 中的后台：Session 的 shell job（`ctx.jobs`）与 subagent（`ctx.subagents`）：`runningJobs`、`untilQuiet`、`killJobs`、`collectJobs`、`stopSubagents`、`stopBackground`、`until`、`cancelled` |
    | `src/infra/commands.ts` | `Commands` CLI 传输：`run`；限配置仓库的 `api`/`pages`；bot 用 `lark`；`git`。`CliError` 带命令输出 code；无输出命令按结束方式失败，不带参数；`validItems` 跳过并计数畸形列表项 |
    | `src/infra/environment.ts` | agent 进程环境：命令 shim、git 配置与凭据 helper、`runAsWorker`、环境与 Ticket 目录检查 |
    | `src/infra/paths.ts` | 包根路径（打包文件位置） |
    | `src/infra/text.ts` | 字数与 chat 消息限制、`fill`、`sameName` |
    | `src/infra/health.ts` | `HealthBoard`，各部 health 条目，各由唯一写入者 claim |
    | `src/sources/kind.ts` | `SourceKind` 协议 |
    | `src/sources/registry.ts` | `SourceRegistry` |
    | `src/sources/host.ts` | `SourceHost`、`SourceContext`、`Health`、`health` 与固定 health 文案 |
    | `src/sources/poll.ts` | GitHub/飞书轮询共用：`POLL_HORIZON_MS`、`polled`、`cursorAt`、`pollThreads`（失败记 Thread 上 `pollError`） |
    | `src/sources/github.ts` | GitHub kind 与插件入口 `./github-watch`：通知轮询、open Ticket 的 PR 与 CI、订阅 Thread |
    | `src/sources/github-trigger.ts` | GitHub 评论、review、Issue 正文、CI 变更作 Trigger，及 GitHub 地址 |
    | `src/sources/github-pull.ts` | PR 规则：Ticket follow 哪些 PR（`claimPull`、`observePull`）、向 Engine 报告的关闭、`pullEndNotices` |
    | `src/sources/github-channel.ts` | GitHub Channel：评论与 review thread 回复，`sent` 或 `gone` |
    | `src/sources/github-read.ts` | CI 快照、仓库读、`github/` 资源、`githubThreadContext`（Issue/PR 对话分页） |
    | `src/sources/feishu.ts` | 飞书 kind 与插件入口 `./feishu-watch` |
    | `src/sources/feishu-trigger.ts` | 飞书消息作 Trigger：实时、历史页、话题页统一规则 |
    | `src/sources/feishu-loops.ts` | 飞书循环：事件消费、历史恢复、chat 名、订阅话题轮询 |
    | `src/sources/feishu-channel.ts` | 飞书 Channel：话题回复，`sent` 或 `gone` |
    | `src/sources/feishu-read.ts` | Trigger 消息的 `feishu/` 读（根、父、话题）与 `feishuThreadContext`（含图片给 assessment） |
    | `src/sources/admission-context.ts` | assessment 起始有界窗口：Trigger Thread 首页（经 kind）与 Trigger 周围消息 |
    | `src/sources/feishu-image.ts` | 读飞书消息图片作 assessing Session 附件 |
    | `src/client/index.tsx` | 客户端插件入口：侧栏、主面板、Plugin Manager 页 |
    | `src/client/app.tsx` | 面板：头、搜索、术语表、标签与当前路由页 |
    | `src/client/provider.tsx` | 各页从 host 读：文案、调用、导航、notice、状态轮询；`restart` 在 dsh-superagent 重启时快速读状态 |
    | `src/client/api.ts` | 管理 API 调用与轮询，类型来自 `src/app/api.ts`；Eval 状态的 Decision 过滤 |
    | `src/client/route.ts` | 标签、设置组、`parseRoute` 的 typed 路由、记住上次打开的设置组 |
    | `src/client/overview.tsx` | 概览 |
    | `src/client/source.tsx`, `trigger.tsx`, `eval.tsx`, `execution.tsx` | 每阶段一文件：状态及若有则设置 |
    | `src/client/thread.tsx`, `ticket.tsx` | 记录页：Thread 时间线；Ticket 授权、Input、Assignment |
    | `src/client/settings.tsx` | Settings 标签：组导航与当前组 |
    | `src/client/settings-kit.tsx` | 设置块积木：阶段组共用设置、`useBlockDraft`、字段（含 Source kind）、保存历史 |
    | `src/client/prompts.tsx` | Prompts 设置：页持有所读 view、编辑器持 draft；Edit/Full prompt/History 标签；内置 part 编辑器显示默认变更并保留或丢弃 override |
    | `src/client/prompt-config.ts` | 按段的 prompt 配置、团队段、skill 名、内置 part（`part/<key>`、`withOverride`）、part 标题、全文行 diff（纯函数） |
    | `src/client/deployment.tsx` | Deployment 设置，亦在 Plugin Manager 显示 |
    | `src/client/draft.tsx` | 设置 draft：`draftReducer`、状态、与新 saved revision 合并、`saveBarText`、保存条 |
    | `src/client/controls.tsx` | DSH 控件的本地拷贝（ markup、键盘、design token），因 bundle 不 import `@deepseek-ai/dsh-client-ui-primitives`；不读 context |
    | `src/client/widgets.tsx` | 页面共用小组件（读面板 context）：section、页头、加载态、时间、链接、确认操作、setup 检查、Markdown |
    | `src/client/rows.tsx` | 共用行：Ticket、Thread、Decision、Thread chip、自动消息 run 折叠 |
    | `src/client/markdown.ts` | report/提问的 Markdown 子集与消息裸地址，块与 inline |
    | `src/client/text.ts` | 按 key 的文案与领域值、reason、发送者、时间的措辞 |
    | `src/client/marks.ts` | 视图分类状态的 mark：Ticket/Thread 阶段、Source 状态、Decision outcome（点、图标、tone、标签） |
    | `src/client/locales.ts` | 双语文案与 [001 领域模型](proto-001-domain.md) glossary |
    | `src/client/client.css`, `css.d.ts` | 面板样式表，以文本加载 |

    `src/sources/github.ts` 与 `src/sources/feishu.ts` 也是两个 Source 插件入口，由 `tsdown.config.ts` 构建，在 `package.json` 导出为 `./github-watch` 与 `./feishu-watch`。bundle 的 `cordis.patch.yml` 插入三个插件，飞书默认禁用。

## Tests

!!! note "原文（English）"

    ## Tests

    | Test | Covers |
    | --- | --- |
    | `tests/core-engine.spec.ts` | Domain rules on in-memory repositories: receiving, Eval, fan-out, resume and stop, interrupted Assignments, the pause, Policy changes, reports and outcomes, attaching, replies, failed deliveries and dismissed problems |
    | `tests/stage.spec.ts` | The Ticket stages, row by row |
    | `tests/policy.spec.ts` | Source scope, the gate, the admission table, Mandate revalidation, the Policy version, settings migrations and assessment outcomes |
    | `tests/registry.spec.ts` | `SourceRegistry`: instances, validation, Policy parts, Channels and Thread context |
    | `tests/sources.spec.ts` | The Feishu and GitHub Sources on a `SourceContext`: translation, polls and read positions, the pull request rules, health |
    | `tests/commands.spec.ts` | CLI calls as the deployment's accounts, the Channels' answers, and repository-scoped GitHub calls |
    | `tests/feishu-read.spec.ts`, `tests/github-read.spec.ts` | Feishu and GitHub reads |
    | `tests/admission-context.spec.ts`, `tests/feishu-image.spec.ts` | The Thread window of assessments, and Feishu image reads |
    | `tests/assessment.spec.ts` | What the MasterAssessor keeps per Eval, and when it lets go |
    | `tests/background.spec.ts` | Waiting for a worker's shell jobs and subagents: an end that woke it, and a host that stops |
    | `tests/bundle-patch.spec.ts` | The bundle's patch, read as DeepSeek Harness reads it, and the agent preset of Executor Sessions it defines |
    | `tests/effects.spec.ts` | The effect queue: coalescing, limits, retries with backoff, health and shutdown |
    | `tests/passes.spec.ts` | Passes: one at a time, the ones asked for during a pass run as one after it, and the runtime health a failed pass reports |
    | `tests/outbox.spec.ts` | The Outbox on a running Runtime: messages wait while processing is paused or their Source is not installed |
    | `tests/cache.spec.ts` | `TextCache` |
    | `tests/queue.spec.ts` | The Runtime's work through the effect queue, and what earlier versions stored |
    | `tests/followups.spec.ts` | A person's follow-up on a Ticket's Thread: its notice to the main Session, the tools that act on it, what a burst costs; a worker's hand-back and context follow, and the main Session's note |
    | `tests/health.spec.ts` | One health entry per part, each cleared by its own next success |
    | `tests/delivery.spec.ts` | `AgentDelivery` |
    | `tests/retry-health.spec.ts`, `tests/role-models.spec.ts` | Model budget retries, and the models of the two roles |
    | `tests/runtime.spec.ts` | The whole application on a simulated DeepSeek Harness host, through tools and HTTP |
    | `tests/tools.spec.ts` | The Sessions' tools: what `orchestrator_read` reads for each role, and the shapes and checks of the other main Session tools |
    | `tests/runtime-concurrency.spec.ts` | Work that overlaps: Triggers during a pass, Tickets that change during deliveries and launches |
    | `tests/http.spec.ts` | The same-origin and loopback checks, the status of each kind of failure, the body limit, path parameters and queries |
    | `tests/settings.spec.ts` | Settings saves that race a model or account check, a Policy sync that a save outran, and the refusals of the pause, a sync and a block |
    | `tests/views.spec.ts` | What the management views derive from the Store, such as the Thread a dropped message belongs to |
    | `tests/timeline.spec.ts` | A Thread's pages and a Trigger's neighbours by time and then id, with every received Trigger's time in one form |
    | `tests/migration.spec.ts` | The schema 11 migration: every stored record in its current shape, and the refusal of older schemas |
    | `tests/store.spec.ts` | Repositories and their working sets, in SQLite as in memory, the kv area, with every key of `KV` spelled only in `src/kv.ts` and the migration, settings history and queries |
    | `tests/prompts.spec.ts` | Prompt validation, assembly and versions |
    | `tests/cards.spec.ts` | The rules the built-in text must state, file by file |
    | `tests/model-surface.spec.ts` | Everything models read: tools, prompts, skills and inputs, checked against the tool schemas, the execution environment and the glossary, within a size budget per role |
    | `tests/deployment.spec.ts`, `tests/environment.spec.ts`, `tests/index.spec.ts` | The plugin configuration and setup, the agent's process environment, the plugin entry |
    | `tests/service-script.spec.ts` | `scripts/dsh-superagent` against fake `ps`, `tmux` and `dsh` |
    | `tests/client.spec.tsx`, `tests/navigation.spec.ts`, `tests/controls.spec.tsx` | The client plugin's registration and polling, Session navigation and route memory, the local controls, and the panel's imports, which have no cycle |
    | `tests/panel-overview.spec.tsx`, `panel-source.spec.tsx`, `panel-trigger.spec.tsx`, `panel-eval.spec.tsx`, `panel-execution.spec.tsx` | Each page of the panel, against a fake management API answered by route |
    | `tests/panel-settings.spec.tsx`, `panel-prompts.spec.tsx`, `panel-deployment.spec.tsx`, `panel-panel.spec.tsx` | The settings groups, drafts and history, the prompts, the Deployment page, and the panel's search, glossary and route memory |
    | `tests/draft.spec.ts`, `tests/prompt-config.spec.ts`, `tests/markdown.spec.ts`, `tests/marks.spec.ts` | Draft states, merges and the save bar's line; the prompt configuration by section and the line diff; the Markdown subset and its links; the mark of every state the views classify |
    | `tests/locales.spec.ts`, `tests/styles.spec.ts` | The copy in both languages, the stylesheet |
    | `tests/class-docs.spec.ts` | Every class in `src` has a JSDoc block before it |
    | `tests/docs.spec.ts` | Numbered documents link only to earlier ones, every local link resolves, no document has merge conflict markers, and this document's tables list every module and every test |

    `tests/fixtures/` holds what several tests share: `host.ts`, a simulated DeepSeek Harness host; `panel-server.ts` and `panel.tsx`, the fake management API and the panel around a page; `deployment.ts`, a resolved deployment; `git.ts`, the environment of every git the tests start, which names no repository; and `async.ts`.

!!! quote "译文（中文）"

    ## 测试

    | Test | 覆盖 |
    | --- | --- |
    | `tests/core-engine.spec.ts` | 内存仓库上的领域规则：receive、Eval、扇出、resume/stop、中断 Assignment、pause、Policy 变更、report/outcome、attach、reply、投递失败、dismiss 问题 |
    | `tests/stage.spec.ts` | Ticket 阶段，逐行 |
    | `tests/policy.spec.ts` | Source scope、gate、准入表、Mandate revalidation、Policy 版本、settings 迁移、assessment outcome |
    | `tests/registry.spec.ts` | `SourceRegistry`：实例、校验、Policy 部分、Channel、Thread context |
    | `tests/sources.spec.ts` | `SourceContext` 上的飞书/GitHub Source：翻译、轮询与读位置、PR 规则、health |
    | `tests/commands.spec.ts` | 以 deployment 账号的 CLI、Channel 应答、仓库范围 GitHub 调用 |
    | `tests/feishu-read.spec.ts`, `tests/github-read.spec.ts` | 飞书/GitHub 读 |
    | `tests/admission-context.spec.ts`, `tests/feishu-image.spec.ts` | assessment 的 Thread 窗口与飞书图片读 |
    | `tests/assessment.spec.ts` | MasterAssessor 每 Eval 保留什么、何时释放 |
    | `tests/background.spec.ts` | 等 worker shell job/subagent：唤醒结束的 turn、停止的 host |
    | `tests/bundle-patch.spec.ts` | bundle patch（按 DSH 读取方式）与 Executor Session agent preset |
    | `tests/effects.spec.ts` | effect 队列：合并、限制、退避重试、health、关闭 |
    | `tests/passes.spec.ts` | Passes：一次一个、pass 期间请求的在其后合并为一轮、失败 pass 的 runtime health |
    | `tests/outbox.spec.ts` | 运行中 Runtime 的 Outbox：processing 暂停或 Source 未安装时消息等待 |
    | `tests/cache.spec.ts` | `TextCache` |
    | `tests/queue.spec.ts` | Runtime 经 effect 队列的工作与旧版存盘 |
    | `tests/followups.spec.ts` | Ticket Thread 上人的 follow-up：主 Session notice、工具、burst 代价；worker hand-back/context follow 与主 Session note |
    | `tests/health.spec.ts` | 每部件一条 health，各自下次成功清除 |
    | `tests/delivery.spec.ts` | `AgentDelivery` |
    | `tests/retry-health.spec.ts`, `tests/role-models.spec.ts` | 模型预算重试与两角色模型 |
    | `tests/runtime.spec.ts` | 模拟 DSH host 上整应用，经工具与 HTTP |
    | `tests/tools.spec.ts` | Sessions 工具：各角色 `orchestrator_read` 与主 Session 其它工具形态/校验 |
    | `tests/runtime-concurrency.spec.ts` | 重叠工作：pass 期间 Trigger、delivery/launch 期间变化的 Ticket |
    | `tests/http.spec.ts` | 同源与 loopback、各类失败状态、body 限制、路径参数与 query |
    | `tests/settings.spec.ts` | 与模型/账号检查竞态的 settings save、save 赶上的 Policy sync、pause/sync/block 拒绝 |
    | `tests/views.spec.ts` | 管理视图从 Store 派生（如丢弃消息所属 Thread） |
    | `tests/timeline.spec.ts` | Thread 分页与 Trigger 按时间再 id 的邻居，received Trigger 时间统一格式 |
    | `tests/migration.spec.ts` | schema 11 迁移：全部记录当前形态、拒绝更旧 schema |
    | `tests/store.spec.ts` | 仓库与工作集（SQLite 同内存）、kv 区、`KV` 各 key 仅在 `src/kv.ts` 与迁移中拼写、settings 历史与查询 |
    | `tests/prompts.spec.ts` | Prompt 校验、组装与版本 |
    | `tests/cards.spec.ts` | 内置文本必须陈述的规则，逐文件 |
    | `tests/model-surface.spec.ts` | 模型所读一切：工具、prompt、skill、input，对照 schema、执行环境、glossary，每角色体积预算 |
    | `tests/deployment.spec.ts`, `tests/environment.spec.ts`, `tests/index.spec.ts` | 插件配置与 setup、agent 进程环境、插件入口 |
    | `tests/service-script.spec.ts` | `scripts/dsh-superagent` 对假 `ps`/`tmux`/`dsh` |
    | `tests/client.spec.tsx`, `tests/navigation.spec.ts`, `tests/controls.spec.tsx` | 客户端插件注册与轮询、Session 导航与路由记忆、本地控件、面板 import 无环 |
    | `tests/panel-overview.spec.tsx`, `panel-source.spec.tsx`, `panel-trigger.spec.tsx`, `panel-eval.spec.tsx`, `panel-execution.spec.tsx` | 面板各页，对按路由应答的假管理 API |
    | `tests/panel-settings.spec.tsx`, `panel-prompts.spec.tsx`, `panel-deployment.spec.tsx`, `panel-panel.spec.tsx` | 设置组、draft/历史、prompts、Deployment 页、搜索/glossary/路由记忆 |
    | `tests/draft.spec.ts`, `tests/prompt-config.spec.ts`, `tests/markdown.spec.ts`, `tests/marks.spec.ts` | draft 状态/合并/保存条；按段 prompt 与行 diff；Markdown 子集与链接；各分类状态的 mark |
    | `tests/locales.spec.ts`, `tests/styles.spec.ts` | 双语文案与样式表 |
    | `tests/class-docs.spec.ts` | `src` 中每个 class 前有 JSDoc |
    | `tests/docs.spec.ts` | 编号文档只链更早文档、本地链接可解析、无冲突标记、本文模块表与测试表列全 |

    `tests/fixtures/` 存放多测共用：`host.ts` 模拟 DSH host；`panel-server.ts`/`panel.tsx` 假管理 API 与包页的面板；`deployment.ts` 已解析 deployment；`git.ts` 各测试启动 git 的环境（不命名仓库）；`async.ts`。

## Persistence

!!! note "原文（English）"

    ## Persistence

    One SQLite file, `<stateDir>/orchestrator.sqlite`, which only the Store opens.

    | Table | Holds | Written by |
    | --- | --- | --- |
    | `trigger` | Each Trigger, its time `at` in the one form `Date#toISOString` gives, set when the Engine receives it, so times compare as text | Engine |
    | `disposition` | Where each Trigger went: `filtered` with the Source's reason code, its Eval and Decision, the Tickets it reached, by fan-out or attachment, whether it was requeued, the Tickets that handed it back with their reasons, and those whose Executor acknowledged or answered it | Engine |
    | `trigger_thread` | Which Threads each Trigger is on, with its time and whether a person wrote it, so a Thread's Triggers are read without scanning every Trigger, in order of time and then id: a page of a Thread's timeline continues from a `<at>|<id>` cursor, and Triggers that share a time are not skipped | Store, with each Trigger |
    | `thread` | Threads: Subscriptions, the Tickets that follow it for context, queued Triggers, the Eval in progress, the count of its expiries and `meta`; the generated columns `active`, set while Triggers are queued or an Eval runs, and `subscribed`, its Source while a Ticket subscribes to it | Engine; Sources write `meta` |
    | `ticket` | Tickets with their Mandate, origin, execution, Assignments, Channels, Inputs and the Threads they follow; the generated columns `open`, set while it has not ended, and `active`, set while Inputs are undelivered or the latest Assignment is pending or running | Engine |
    | `decision` | Decisions, by Thread | Engine |
    | `effect` | The effect queue: per kind and key, the payload, its order, failed attempts with the last error, and when it is due | Effects; the schema 11 migration queues what earlier versions kept for the main Session |
    | `outbox` | The receipt of each message for a Channel, by id, with the Ticket it is for and its `status`, `pending`, `sent` or `dropped`, kept so an id is never queued twice; for a dropped message, when and why, as the generated column `dropped`, its time, and a reason code. A message that waits has a `reply` effect, which holds its attempts | The Outbox |
    | `kv` | Every other record, by key | The part that names it |

    A generated column is computed from its row's JSON and is empty outside its set, and each has an index over its set, so a pass reads only active work. A database that predates a column gets it when the Store opens it.

    `KV` (`src/kv.ts`) defines each key-value record's owner, key and type. All readers and writers use these entries; only migration code names older keys.

    | Records | Keys or contents |
    | --- | --- |
    | Schema | Current schema version |
    | Settings versions | `settings-revision`, `settings-version:<n>`, `settings-block-revision:<block>`; snapshots include origin, Policy version and affected Tickets |
    | Prompt versions | `prompt-revision`, `prompt-version:<n>` |
    | Main Session | `master-id` |
    | Session prompt history | `session-prompts:<session id>` |
    | Source state | Read positions such as `github-cursor:<number>`, PR CI fingerprints and seen Feishu-topic messages; keys begin with the kind or `thread-` plus a Thread key |
    | Display names | `source-labels:<source id>` |

    Dismissal is stored on its Decision. Input text is rendered from the Ticket when read, so neither needs a separate key-value record.

    Read caches use bounded in-memory `TextCache` instances and are lost on restart. A Session paging through an original Source record or prompt preview must reread offset 0 after a restart. Feishu links resolved after intake are cached the same way; link-based search finds them only after that link has been read during the current process lifetime.

    Nothing deletes old records yet, so these grow without bound: the `trigger`, `disposition` and `decision` rows; ended Tickets and their Threads; the outbox's receipts; and in `kv`, every saved settings revision and prompt version, the prompt revisions each Session worked under, and the Sources' records per Thread and per Issue or pull request.

    `SCHEMA_VERSION` only increases. A database of a newer version refuses to start; `src/app/migration.ts` upgrades a database of the pre-release schema 10 once, at start, and refuses an older one.

!!! quote "译文（中文）"

    ## 持久化

    单一 SQLite 文件 `<stateDir>/orchestrator.sqlite`，仅 Store 打开。

    | 表 | 存放 | 写入者 |
    | --- | --- | --- |
    | `trigger` | 各 Trigger，时间 `at` 为 `Date#toISOString` 统一形式，Engine 接收时设定，文本可比 | Engine |
    | `disposition` | 各 Trigger 去向：`filtered` 与 Source reason、Eval/Decision、fan-out/attachment 到达的 Ticket、是否 requeue、hand-back 的 Ticket 及 reason、Executor ack/answer 的 Ticket | Engine |
    | `trigger_thread` | Trigger 所在 Thread、时间及是否人写；Thread Trigger 按时间再 id 读，时间线页从 `<at>|<id>` 游标续读，同时间不跳过 | Store（随 Trigger） |
    | `thread` | Thread：Subscription、context follow 的 Ticket、queued Trigger、进行中 Eval、连续过期计数、`meta`；生成列 `active`（有 queued 或 Eval）、`subscribed`（有 Ticket 订阅时其 Source） | Engine；Source 写 `meta` |
    | `ticket` | Ticket 与 Mandate、origin、execution、Assignment、Channel、Input、follow 的 Thread；生成列 `open`（未结束）、`active`（Input 未投递或最新 Assignment pending/running） | Engine |
    | `decision` | Decision，按 Thread | Engine |
    | `effect` | effect 队列：kind/key、payload、顺序、失败次数与末次错误、due 时间 | Effects；schema 11 迁移入队旧版为主 Session 保留的项 |
    | `outbox` | 各 Channel 消息 receipt：id、Ticket、`status`（pending/sent/dropped），同 id 不重复入队；dropped 时有生成列 `dropped`（时间、reason）。等待消息有 `reply` effect 记尝试次数 | Outbox |
    | `kv` | 其余记录，按 key | 命名该 key 的部件 |

    生成列由行 JSON 计算，集合外为空；各列在其集合上有索引，pass 只读活跃工作。缺列的旧库在 Store 打开时补列。

    `KV`（`src/kv.ts`）定义每条 KV 的 owner、key、类型；读写均用这些条目；仅迁移代码命名旧 key。

    | 记录 | Key 或内容 |
    | --- | --- |
    | Schema | 当前 schema 版本 |
    | Settings 版本 | `settings-revision`、`settings-version:<n>`、`settings-block-revision:<block>`；快照含 origin、Policy 版本、受影响 Ticket |
    | Prompt 版本 | `prompt-revision`、`prompt-version:<n>` |
    | 主 Session | `master-id` |
    | Session prompt 历史 | `session-prompts:<session id>` |
    | Source 状态 | 读位置如 `github-cursor:<number>`、PR CI 指纹、已见飞书话题消息；key 以 kind 或 `thread-`+Thread key 开头 |
    | 显示名 | `source-labels:<source id>` |

    Dismiss 存在 Decision 上。Input 文本读时从 Ticket 渲染，无需单独 KV。

    读缓存用有界内存 `TextCache`，重启丢失。Session 分页读 Source 原文或 prompt preview 须在重启后从 offset 0 重读。intake 后解析的飞书链接同样缓存；基于链接的搜索仅在该链接于本进程 lifetime 内读过之后可见。

    尚无删除旧记录，故以下无界增长：`trigger`/`disposition`/`decision` 行；已结束 Ticket 及其 Thread；outbox receipt；`kv` 中每次 settings/prompt revision、各 Session 工作过的 prompt revision、Source 按 Thread/Issue/PR 的记录。

    `SCHEMA_VERSION` 只增。较新 schema 的库拒绝启动；`src/app/migration.ts` 启动时将 pre-release schema 10 库升级一次，更旧则拒绝。

## Model-facing interfaces

!!! note "原文（English）"

    ## Model-facing interfaces

    ### Main Session tools

    The main Session has 14 application tools:

    | Purpose | Tools |
    | --- | --- |
    | Read and route | `orchestrator_read`, `orchestrator_route` |
    | Manage Tickets | `orchestrator_ticket_retry`, `orchestrator_ticket_request_status`, `orchestrator_ticket_note` |
    | Re-evaluate | `orchestrator_trigger_requeue` |
    | Assess proactive work | `orchestrator_eval`, `orchestrator_eval_read`, `orchestrator_eval_decide` |
    | Communicate and unsubscribe | `orchestrator_reply`, `orchestrator_thread_unsubscribe` |
    | Configure | `orchestrator_settings_save`, `orchestrator_policy_sync`, `orchestrator_prompts_save` |

    `orchestrator_route` accepts 1–20 decisions in order. Each accept, attach, stop or ignore runs as a separate Engine command. It also permits ignoring an inferred candidate directly from its card. `orchestrator_ticket_note` sends a fact as context, not authority.

    Settings saves select `policy`, `eval`, `executor`, `pause`, or `source`; the last also names its Source id. A prompt save with `preview: true` composes text without saving.

    The main Session also has DeepSeek Harness's `skill`, `bash`, `read`, `read_image`, `job_output`, `job_list` and `job_kill`. Avoiding code reads is a prompt rule, not a tool restriction.

    ### Presets and wait limits

    New main Sessions use `dsh-superagent-router` (`ROUTER_PRESET`). Shell commands and job waits return after 5 seconds. If the Web UI selects another preset before the first turn, `Sessions` restores this preset, or `WORKER_PRESET` for a worker.

    The `noWait` guard in `src/app/sessions.ts` refuses main-Session shell commands that invoke `sleep` at a command position, shell `timeoutMs` above 30 seconds, and `job_output` waits above 5 seconds. Reports and job results arrive automatically, so waiting only delays incoming candidates.

    Existing main Sessions on `standard` also need explicit timeouts for foreground commands and job waits; otherwise that preset's defaults hold the step for 60 and 30 seconds respectively. The host determines the current preset through `presetForSession`, falls back to the Session projection or header, and assumes `standard` if it cannot read the value.

    ### Executor tools

    Each Executor has six application tools:

    | Tool | Purpose |
    | --- | --- |
    | `orchestrator_read` | Read resources available to its own Ticket |
    | `orchestrator_ack` | Acknowledge a request |
    | `orchestrator_report` | Report progress or an outcome, binding a pull request under the pull-request rules |
    | `orchestrator_thread_unrelated` | Drop an unrelated attached Thread, hand back its owned follow-ups and tell the main Session why |
    | `orchestrator_thread_follow` | Subscribe to a related Thread for context and tell the main Session |
    | `orchestrator_trigger_handback` | Hand back a message once, with a reason carried by its candidate |

    A hand-back returns `queued: false` if another Ticket still owns the message, or this Ticket never owned it. For example, a bot message went to Eval independently.

    The worker preset is defined in `cordis.patch.yml` and selected through `WORKER_PRESET`. `WORK_TOOLS` in `src/app/sessions.ts` retains available file, search, shell, job, web, skill, todo and subagent tools. Workers do not receive tools for asking or presenting to a person, changing the profile, scheduling work outside worker slots, starting or messaging other Sessions, or editing external documents. Tool calls are refused after the Ticket ends.

    ### Result shapes and assessment context

    Routing results use `{decision?, reason, ticket?}`. `orchestrator_route` returns `{results}`, preserving item order and adding each item's `trigger_id` and `kind`. Invalid or refused items return `refused` and a reason without stopping the batch.

    Settings and prompt saves return the saved configuration. Policy sync returns the Source, allowed-list size, and `added` and `removed` names.

    `orchestrator_eval` returns context rather than instructions. Assessment instructions live in `runtime/admission` (`prompts/admission.md`), and rules in `runtime/policy`. Its context includes:

    - The Trigger text, up to 4,000 characters, with image ids and other Triggers in the batch.
    - Up to eight `related_tickets`, open ones first, and `tickets_total`.
    - The Eval's `policy_version`.
    - A Thread window: the first five messages and five on either side of the Trigger. Messages use `{id, from, at, text}`, normally limited to 400 characters, or 1,200 for the two on either side. Truncated messages include `chars`; image references use `images`.
    - Other loaded messages as `{id, from, preview}` stubs of 50 characters. `orchestrator_eval_read` expands them by id.

    Ticket cards use the closed `TICKET_STATES` set from `src/app/cards.ts`: an open stage such as `person/review` or `working/queued`, or an ended outcome. Acknowledgements are limited to 100 words whether submitted through accept, assessment or `orchestrator_ack`.

    ### Read resources

    `RESOURCES` in `src/app/resources.ts` defines the resources and generates the tool's `resource` description.

    | Resources | Content |
    | --- | --- |
    | `tickets`, `triggers`, `threads`, `decisions` | Paged lists as `{items, next_offset}`; activity lists put newest first |
    | `decision/<id>` | One Decision |
    | `thread/<key>` | Label, Tickets, recent Decisions and a page of Triggers |
    | `trigger/<id>` | Message text, replied-to Trigger and neighboring messages, excluding CI runs |
    | `ticket/<id>` | Related ids, source-batch Triggers, Threads, and first pages of Assignments and Inputs |
    | Ticket subresources | `/assignments`, `/inputs`, `/assignment`, `/result`, `/report/<assignment id>`, `/transcript`, `/message/<n>`, `/input/<id>` |
    | `settings`, `settings/<block>` | Revisions and summaries, or a complete block ready to save |
    | `prompts`, `prompts/<revision>`, `prompts/current`, `prompts/defaults`, `prompt-preview/<id>` | Prompt configurations and composed text |
    | `models`, `models/<provider>` | Available models |
    | `feishu/<trigger id>/…`, `github/…` | Original Source records, described in the Source skills |

    Ticket Threads are marked `context` for context-only follows and `bound` for Source bindings. Assignment pages are newest first and include report previews. Continue lists with `next_offset`. `/input/<id>` accepts an Input's name or id. `settings` includes small blocks in full and summarizes Policy and Source blocks.

    Source resources include message neighborhoods, `github/pulls/<n>/thread/<comment id>` and `github/commits/<sha>/checks`.

    An original's messages read in one compact shape, `{id, from, at, text, reply_to?, path?, line?, url?}`, not as GitHub or Feishu records them.

    An Executor Session reads only its own Ticket, its Threads, the Triggers that reached it or that its request held, the prompt versions it worked under and the originals, and its pages leave out the ids of what it cannot read, Decisions and other Tickets.

    Every id a page shows reads through one of these, so a Session walks from one object to the next; hashes nothing reads by, such as an Eval's id or a Policy version, stay off the pages.

    A text page has 4,000 UTF-16 code units by default and 8,000 at most, a list 20 items by default and 50 at most.

    The names `event/`, `task/` and `observation/` of earlier versions still resolve.

    ### Messages delivered to Sessions

    These messages are steered into the main Session's turn:

    | Message | Content |
    | --- | --- |
    | Candidate | Batch Trigger cards, Thread, `related_tickets`, `tickets_total`, `delivered_to`, referenced Tickets from Issue/PR links, `handed_back` reasons and the next action |
    | `follow_up` | One message as its Executor sees it, `replies_to`, receiving Tickets, and the reminder that Executors decide it while the main Session may only stop work |
    | `worker_report` | Ticket card, originating Trigger, state and summary up to 600 characters; `chars` and a read pointer when truncated |
    | `worker_attention` | Ticket card carrying the question |

    Other notices are injected without waking the main Session: `ticket_ended`, `settings_saved`, prompt saves, Trigger requeues, dropped Subscriptions and context follows. A settings notice names the block, changed paths and `read: settings/<block>`. The main Session receives no redundant notice for a save, requeue, unsubscribe or attach it performed itself.

    Workers receive assignments and Inputs inline. The first message includes the title, up to 1,500 characters of assignment text and the source Trigger card. Inputs carry a Trigger card with up to 600 characters and `replies_to`, or plain Input text. Read pointers appear only for truncated content.

    ### Panel-only actions

    There is no tool for dismissing an unavailable Decision, rerunning setup, refreshing Source display names, or sending an Executor-owned human follow-up to Eval. The last action is deliberately refused by `orchestrator_trigger_requeue`. Other panel actions have equivalent tools.

    Most built-in instructions live in `prompts/*.md`, with one sentence per line joined by `promptFile`. Source skills live in `skills/dsh-superagent-*/SKILL.md`. Tool descriptions and fixed notices such as restart recovery notes remain in code. Assessment instructions live only in `prompts/admission.md`.

    `tests/model-surface.spec.ts` checks these surfaces for drift. [004 Prompts](proto-004-prompts.md) explains the team's editable parts.

!!! quote "译文（中文）"

    ## 面向模型的接口

    ### 主 Session 工具

    主 Session 有 14 个应用工具：

    | 用途 | 工具 |
    | --- | --- |
    | 读与路由 | `orchestrator_read`, `orchestrator_route` |
    | 管理 Ticket | `orchestrator_ticket_retry`, `orchestrator_ticket_request_status`, `orchestrator_ticket_note` |
    | 重新评估 | `orchestrator_trigger_requeue` |
    | 评估主动工作 | `orchestrator_eval`, `orchestrator_eval_read`, `orchestrator_eval_decide` |
    | 通信与退订 | `orchestrator_reply`, `orchestrator_thread_unsubscribe` |
    | 配置 | `orchestrator_settings_save`, `orchestrator_policy_sync`, `orchestrator_prompts_save` |

    `orchestrator_route` 按序接受 1–20 条 decision；各 accept/attach/stop/ignore 为独立 Engine 命令。亦允许在卡片上直接 ignore inferred candidate。`orchestrator_ticket_note` 发送事实作上下文，非授权。

    Settings 保存选 `policy`、`eval`、`executor`、`pause` 或 `source`（后者还带 Source id）。`preview: true` 的 prompt 保存只 compose 不写入。

    主 Session 另有 DSH 的 `skill`、`bash`、`read`、`read_image`、`job_output`、`job_list`、`job_kill`。避免读代码是 prompt 规则，非工具限制。

    ### Preset 与等待限制

    新主 Session 用 `dsh-superagent-router`（`ROUTER_PRESET`）。Shell 与 job 等待 5 秒返回。Web UI 在首 turn 前选其它 preset 时，`Sessions` 恢复此 preset（worker 用 `WORKER_PRESET`）。

    `src/app/sessions.ts` 的 `noWait` 拒绝主 Session shell 在命令位调用 `sleep`、`timeoutMs`>30s、以及 `job_output` 等待>5s。Report 与 job 结果自动到达，等待只会延迟候选。

    已在 `standard` 上的主 Session 亦需显式超时 foreground 命令与 job 等待；否则 preset 默认步长 60s/30s。host 经 `presetForSession` 定当前 preset，回退 Session 投影或 header，读不到则假定 `standard`。

    ### Executor 工具

    每个 Executor 六个应用工具：

    | 工具 | 用途 |
    | --- | --- |
    | `orchestrator_read` | 读本 Ticket 可用资源 |
    | `orchestrator_ack` | Ack 请求 |
    | `orchestrator_report` | 报 progress/outcome，按 PR 规则绑定 PR |
    | `orchestrator_thread_unrelated` | 丢弃无关 attach Thread、hand-back 自有 follow-up 并告知主 Session 原因 |
    | `orchestrator_thread_follow` | 为 context 订阅相关 Thread 并告知主 Session |
    | `orchestrator_trigger_handback` | 带 candidate reason 一次性 hand-back 消息 |

    若其它 Ticket 仍拥有消息或本 Ticket 从未拥有，hand-back 返回 `queued: false`（如 bot 消息独立进入 Eval）。

    worker preset 在 `cordis.patch.yml` 定义，经 `WORKER_PRESET` 选择。`src/app/sessions.ts` 的 `WORK_TOOLS` 保留文件/搜索/shell/job/web/skill/todo/subagent。Worker 无向人提问/展示、改 profile、slot 外调度、启停/消息其它 Session、编辑外部文档的工具。Ticket 结束后拒绝工具调用。

    ### 结果形态与 assessment 上下文

    路由结果用 `{decision?, reason, ticket?}`。`orchestrator_route` 返回 `{results}`，保序并为每项加 `trigger_id`、`kind`。无效/拒绝项返回 `refused` 与 reason，不中断批。

    Settings/prompt 保存返回已存配置。Policy sync 返回 Source、允许列表大小、`added`/`removed` 名。

    `orchestrator_eval` 返回上下文而非指令。Assessment 指令在 `runtime/admission`（`prompts/admission.md`），规则在 `runtime/policy`。上下文含：

    - Trigger 文本最多 4000 字，含图片 id 与批内其它 Trigger。
    - 最多八个 `related_tickets`（open 优先）与 `tickets_total`。
    - Eval 的 `policy_version`。
    - Thread 窗口：前五条与 Trigger 两侧各五条。消息 `{id, from, at, text}`，通常 400 字，紧邻两条 1200 字。截断含 `chars`；图片引用用 `images`。
    - 其它已加载消息为 50 字 `{id, from, preview}` stub，`orchestrator_eval_read` 按 id 展开。

    Ticket 卡片用 `src/app/cards.ts` 闭集 `TICKET_STATES`：open 阶段如 `person/review`、`working/queued`，或 ended outcome。Ack 无论 accept/assessment/`orchestrator_ack` 均限 100 词。

    ### 读资源

    `src/app/resources.ts` 的 `RESOURCES` 定义资源并生成工具的 `resource` 描述。

    | 资源 | 内容 |
    | --- | --- |
    | `tickets`, `triggers`, `threads`, `decisions` | 分页 `{items, next_offset}`；活动列表最新在前 |
    | `decision/<id>` | 单条 Decision |
    | `thread/<key>` | 标签、Ticket、近期 Decision、Trigger 页 |
    | `trigger/<id>` | 消息文本、回复 Trigger、邻域消息（不含 CI run） |
    | `ticket/<id>` | 相关 id、source-batch Trigger、Thread、Assignment/Input 首页 |
    | Ticket 子资源 | `/assignments`, `/inputs`, `/assignment`, `/result`, `/report/<assignment id>`, `/transcript`, `/message/<n>`, `/input/<id>` |
    | `settings`, `settings/<block>` | revision 与摘要，或可保存的完整块 |
    | `prompts`, `prompts/<revision>`, `prompts/current`, `prompts/defaults`, `prompt-preview/<id>` | Prompt 配置与 compose 文本 |
    | `models`, `models/<provider>` | 可用模型 |
    | `feishu/<trigger id>/…`, `github/…` | Source 原文（见 Source skills） |

    Ticket Thread 标 `context`（仅 context follow）或 `bound`（Source 绑定）。Assignment 页最新在前含 report preview。列表用 `next_offset` 续读。`/input/<id>` 接受 Input 名或 id。`settings` 小块全文、Policy/Source 块摘要。

    Source 资源含消息邻域、`github/pulls/<n>/thread/<comment id>`、`github/commits/<sha>/checks`。

    原文消息统一紧凑形态 `{id, from, at, text, reply_to?, path?, line?, url?}`，非 GitHub/飞书原生记录。

    Executor Session 只读本 Ticket、其 Thread、到达或 request 持有的 Trigger、工作过的 prompt 版本与原文；页上不出现不可读的 id、Decision 与其它 Ticket。

    页上每个 id 经上述资源可读，Session 可对象间 walk；Eval id、Policy 版本等不可读的 hash 不出现在页上。

    文本页默认 4000 UTF-16 码元、最多 8000；列表默认 20 条、最多 50。

    旧版 `event/`、`task/`、`observation/` 仍解析。

    ### 投递给 Session 的消息

    以下 steer 入主 Session turn：

    | 消息 | 内容 |
    | --- | --- |
    | Candidate | 批 Trigger 卡片、Thread、`related_tickets`、`tickets_total`、`delivered_to`、Issue/PR 链接引用 Ticket、`handed_back` reason 与下一步 |
    | `follow_up` | Executor 所见单条消息、`replies_to`、接收 Ticket，并提醒 Executor 决定、主 Session 只能 stop |
    | `worker_report` | Ticket 卡片、源 Trigger、状态与最多 600 字摘要；截断时 `chars` 与读指针 |
    | `worker_attention` | 带问题的 Ticket 卡片 |

    其它 notice 注入且不唤醒主 Session：`ticket_ended`、`settings_saved`、prompt 保存、Trigger requeue、丢弃 Subscription、context follow。settings notice 含块、变更路径与 `read: settings/<block>`。主 Session 自行 save/requeue/unsubscribe/attach 时不重复 notice。

    Worker 内联收 assignment 与 Input。首条含标题、最多 1500 字 assignment 文本与源 Trigger 卡片。Input 带最多 600 字 Trigger 卡片与 `replies_to`，或纯 Input 文本。读指针仅截断时出现。

    ### 仅面板操作

    无工具可 dismiss unavailable Decision、重跑 setup、刷新 Source 显示名、或将 Executor 拥有的人工 follow-up 送 Eval；最后一项被 `orchestrator_trigger_requeue` 故意拒绝。其它面板操作有等价工具。

    多数内置指令在 `prompts/*.md`，`promptFile` 将单行句合并。Source skills 在 `skills/dsh-superagent-*/SKILL.md`。工具描述与固定 notice（如重启 recovery）留在代码。Assessment 指令仅在 `prompts/admission.md`。

    `tests/model-surface.spec.ts` 防这些面向漂移。[004 Prompts](proto-004-prompts.md) 说明团队可编辑部分。

## Configuration reload

!!! note "原文（English）"

    ## Configuration reload

    Every plugin configuration key except `stateDir` is volatile. The panel's Deployment settings and DeepSeek Harness's Plugin Manager edit them through DeepSeek Harness's configuration form; the host writes `cordis.patch.yml` and updates the values in place, and dsh-superagent reads each limit when it uses it, so a change applies without a restart.

    A settings save that changes a Source field setup reads, such as an account's CLI, and any save while setup waits, restarts dsh-superagent. DeepSeek Harness's settings service does not serve the entry during that restart, so after its own restart dsh-superagent asks it to read the entry again, and an open form becomes editable again.

!!! quote "译文（中文）"

    ## 配置热读

    除 `stateDir` 外各插件配置键均为 volatile。面板 Deployment 与 DSH Plugin Manager 经 DSH 配置表单编辑；host 写 `cordis.patch.yml` 就地更新值，dsh-superagent 使用时读取各限制，故变更无需重启即可生效。

    改变 setup 所读 Source 字段（如账号 CLI）的 settings save，以及 setup 等待期间的任何 save，会重启 dsh-superagent。重启期间 DSH settings 服务不提供该条目；dsh-superagent 自重启后再请求读取，打开表单可再次编辑。

## Reviewing a change against real data

!!! note "原文（English）"

    ## Reviewing a change against real data

    Point `DSH_HOME` at a temporary directory and create a new profile there. Copy the database from a backup, disable every Source, pause processing and remove the main Session id. Give the GitHub and Feishu CLIs read-only stand-ins.

!!! quote "译文（中文）"

    ## 用真实数据审查变更

    将 `DSH_HOME` 指向临时目录并在其中建新 profile。从备份复制数据库，禁用所有 Source、pause processing 并移除主 Session id。为 GitHub 与飞书 CLI 提供只读替身。

## Open questions

!!! note "原文（English）"

    ## Open questions

    - Trust: which Trigger fields are trusted, such as the Source's actor and Thread membership, and which are not, such as the body and quotes.
    - Deployment isolation: the agent's profile on its own pinned DeepSeek Harness installation, so upgrading DeepSeek Harness elsewhere does not break it.

!!! quote "译文（中文）"

    ## 开放问题

    - 信任：哪些 Trigger 字段可信（如 Source actor、Thread 成员），哪些不可信（如正文与引用）。
    - 部署隔离：agent profile 固定在独立 pinned DSH 安装上，其它处升级 DSH 不破坏它。

