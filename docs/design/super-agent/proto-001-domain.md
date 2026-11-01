# Prototype · 001 领域模型

<p class="lead">对照 `dsh-superagent-prototype/docs/001-domain.md`。</p>

!!! info "延伸材料"
    领域流与 Trigger 路径见 [设计抽象 §4](design-abstract.md#sec-4-events)。

## 001 Domain model

!!! note "原文（English）"

    # 001 Domain model

    This document names the concepts of dsh-superagent and the rules each one keeps. The code, the tools the agents call, the panel and the other documents use these terms and no others. Following [000 Philosophy](proto-000-philosophy.md), the model holds only concepts the work needs, and a business need becomes an attribute of one of them.

!!! quote "译文（中文）"

    # 001 领域模型

    本文档命名 dsh-superagent 的概念及各自遵守的规则。代码、agent 所调用的工具、面板与其他文档只使用这些术语，不用其它说法。遵循 [000 哲学](proto-000-philosophy.md)：模型只保留工作所需的概念，业务需求应成为其中某个概念的属性。

## Flow

!!! note "原文（English）"

    ## Flow

    Work moves from Trigger to Eval to execution. A Source produces Triggers. A Trigger fans out as context to the Tickets subscribed to its Threads. A person's message that reached a Ticket this way stays with that Ticket; every other Trigger enters Eval, which decides whether it asks for new work. When Eval accepts, it creates a Ticket with a Mandate. An Assignment hands the Ticket to an Executor, and results go back to the Source through a Channel.

    ```mermaid
    flowchart LR
      S[Source] -->|Trigger| T[Thread]
      T -->|batch| E[Eval]
      T -->|Subscription fan-out| K[Ticket]
      E -->|accept with Mandate / stop| K
      E -->|ignore / unavailable| X[recorded]
      P[Policy change] -->|notice| K
      K -->|Assignment| W[Executor]
      K -->|Channel| S
    ```

!!! quote "译文（中文）"

    ## 流程

    工作从 Trigger 经 Eval 到执行。Source 产生 Trigger。Trigger 作为上下文扇出到订阅了其 Thread 的 Ticket；经此路径到达 Ticket 的人的消息留在该 Ticket；其余 Trigger 进入 Eval，由 Eval 决定是否请求新工作。Eval accept 时创建带 Mandate 的 Ticket；Assignment 将 Ticket 交给 Executor；结果经 Channel 回到 Source。

    ```mermaid
    flowchart LR
      S[Source] -->|Trigger| T[Thread]
      T -->|batch| E[Eval]
      T -->|Subscription fan-out| K[Ticket]
      E -->|accept with Mandate / stop| K
      E -->|ignore / unavailable| X[recorded]
      P[Policy change] -->|notice| K
      K -->|Assignment| W[Executor]
      K -->|Channel| S
    ```

## Concepts

!!! note "原文（English）"

    ## Concepts

    Source, Thread, Eval, Mandate and Subscription form the core domain; the rest supports them.

    | Concept | Definition | Rules it keeps | Why it exists |
    | --- | --- | --- | --- |
    | Source | A stream of events. A Source kind is a reusable implementation; a Source instance is one configuration of a kind: whether it runs and what it listens to | Holds its read position and its own bot identity. Defines what counts as an event, including conditions that only hold over time. Drops the bot's own messages and Triggers outside what it listens to before they enter the domain. Translates each Trigger into the Threads it belongs to and the Channel addresses to reply on | A restart must not replay events |
    | Trigger | One immutable event with the Source's own metadata, such as its actor | Belongs to one or more Threads. Accepting a Trigger twice creates one Ticket | The unit of audit and of idempotence |
    | Thread | Triggers in time order; the Source defines which Triggers form one Thread | At most one Eval runs on it at a time. It may have several Tickets. It holds its Subscriptions, the running Eval and its Decision history | Gives Eval a consistent view and its context |
    | Correlation | The relation between Threads that one Trigger belongs to | Adds context; never routes | Lets Eval follow related evidence |
    | Channel | The write side of a Thread; the Source defines its addresses | A Thread may have several Channels; a Ticket holds a set of them | Acknowledgements, replies, pull requests and resolved reviews are all writes |
    | Policy | The versioned assessment rules and each Source's part: who may originate work through that Source, in that Source's own identities, how the list is synchronized, and which bots may make candidates | The Policy version covers the rules, the accept mode and the Source parts. A change that bears on a live Ticket, such as a change to a rule it was accepted under, is delivered to its Assignment; it does not revoke Mandates. Identities on different Sources are never mapped to each other | When the rules change, a running Eval must be discarded |
    | Eval | The judgement on a batch of Triggers, in two layers: gate and assessment | Input: the Trigger batch, a Thread snapshot, every Ticket on the Thread including ended ones, and the current Policy. Output: a Decision. Holds no state | Decides whether new work starts, goes on or stops |
    | Mandate | What an accept grants: the Policy version, the scope, the strength, the matched rules, the sender's identity on that Source and how the Source received the Trigger | Immutable; wider scope means a new Ticket. A Ticket's end does not end it: only a person's stop withdraws it, and a Policy that no longer holds it revokes it. Revalidated against the current Policy before an ended Ticket resumes | States why the work may be done, and how far it may go |
    | Ticket | A unit of authorized work | Only Eval creates one. Refers to its Mandate; holds its Channels and Inputs. Goes open → ended, and back to open when a person's message on one of its Threads resumes it, unless a person stopped it. Defines its own state instead of following the Executor's | Owns the execution and its reports |
    | Assignment | The binding of a Ticket to one Executor instance | A Ticket has several over time. Goes pending → running → ended independently of the Ticket. Only its final report may end the Ticket on the Executor's side | The Executor is outside the domain, but who executes must be inside it |
    | Subscription | A many-to-many relation between Tickets and Threads, held by the Thread | A matching Trigger fans out to every live subscribed Ticket, with no check of who sent it; a person's message that reaches a Ticket this way takes no Eval. A Thread an Executor follows for context reaches its Ticket the same way, but lends it no authority and does not resume it. An ended Ticket keeps its Subscriptions, including that of a pull request that closed or merged: a person's message resumes it under the same Mandate, with no Eval or Decision; messages from bots and CI do not reach it, since its Executor reads its Threads again when it resumes. A Ticket a person stopped receives nothing. The main Session attaches a Thread to an existing Ticket only when its message points at that Ticket explicitly. A person can drop every Subscription on a Thread; an Executor can drop only its own Ticket's Subscription to a Thread it finds unrelated, except the Thread its Ticket started on and a Thread a Source made it follow, such as its pull request's | Later discussion reaches the Executor without anyone mentioning the agent again |

    A person is not a domain concept. A Trigger carries its actor's identifier as the Source gives it. An allowed list is a Source's part of the Policy, in that Source's identities. That one person has accounts on several Sources is prompt text that the agents read when they need it.

    The Executor, with its Sessions and directories, is outside the domain and connects through Assignments.

    Aggregates: a Source holds its read position. A Thread holds its Subscriptions, its running Eval, which runs serially and merges Triggers that arrive meanwhile into the next batch, and its Decision history. A Ticket holds its Assignments, Channels and Inputs, and refers to Decisions by id.

!!! quote "译文（中文）"

    ## 概念

    Source、Thread、Eval、Mandate 与 Subscription 构成核心领域；其余概念为它们服务。

    | 概念 | 定义 | 所守规则 | 存在理由 |
    | --- | --- | --- | --- |
    | Source | 事件流。Source 种类是可复用的实现；Source 实例是某种类的一份配置：是否运行以及监听什么 | 持有其读取位置及其 bot 身份。定义何谓事件，包括仅在一段时间内成立的条件。在进入领域之前丢弃 bot 自身消息及监听范围外的 Trigger。将每个 Trigger 翻译成其所属的 Thread 以及回复用的 Channel 地址 | 重启时不应重放事件 |
    | Trigger | 一条不可变事件，带 Source 自有元数据（如 actor） | 属于一个或多个 Thread。对同一 Trigger 接受两次只会创建一个 Ticket | 审计与幂等的单位 |
    | Thread | 按时间排序的 Trigger；Source 定义哪些 Trigger 构成一条 Thread | 同一时刻其上最多运行一个 Eval。可有多张 Ticket。持有 Subscription、正在运行的 Eval 及其 Decision 历史 | 为 Eval 提供一致视图与上下文 |
    | Correlation | 同一条 Trigger 所属多条 Thread 之间的关系 | 只增添上下文；从不用于路由 | 让 Eval 跟踪相关证据 |
    | Channel | Thread 的写侧；Source 定义其地址 | 一条 Thread 可有多个 Channel；一张 Ticket 持有一组 Channel | 确认、回复、拉取请求与已 resolve 的 review 都是写操作 |
    | Policy | 版本化的评估规则，以及每个 Source 的部分：谁可通过该 Source、以该 Source 自有身份发起工作，列表如何同步，以及哪些 bot 可提出候选 | Policy 版本涵盖规则、accept 模式与各 Source 部分。影响 live Ticket 的变更（例如其所依据的规则变更）会送达其 Assignment；不会撤销 Mandate。不同 Source 上的身份永不彼此映射 | 规则变更时必须丢弃正在运行的 Eval |
    | Eval | 对一批 Trigger 的判断，分两层：gate 与 assessment | 输入：Trigger 批次、Thread 快照、该 Thread 上每张 Ticket（含已结束的）及当前 Policy。输出：Decision。不持有状态 | 决定新工作是开始、继续还是停止 |
    | Mandate | accept 授予的内容：Policy 版本、范围、强度、匹配的规则、发送方在该 Source 上的身份，以及 Source 如何接收该 Trigger | 不可变；更宽的范围意味着新 Ticket。Ticket 结束不会结束 Mandate：只有人的 stop 可撤回，Policy 不再成立则撤销。已结束 Ticket 恢复前须对当前 Policy 重新验证 | 说明工作为何可做、可做到何种程度 |
    | Ticket | 一个授权工作单元 | 仅 Eval 创建。引用其 Mandate；持有 Channel 与 Input。状态 open → ended；当其某 Thread 上的人的消息恢复它时可回到 open，除非被人 stop。自定状态，不随 Executor 的状态走 | 拥有执行及其报告 |
    | Assignment | 一张 Ticket 与一个 Executor 实例的绑定 | 一张 Ticket 随时间可有多个 Assignment。pending → running → ended，独立于 Ticket。仅其最终报告可在 Executor 侧结束 Ticket | Executor 在域外，但由谁执行必须在域内 |
    | Subscription | Ticket 与 Thread 之间的多对多关系，由 Thread 持有 | 匹配的 Trigger 扇出到每张 live 的 subscribed Ticket，不检查发送者；经此到达 Ticket 的人的消息不经过 Eval。Executor 为上下文 follow 的 Thread 以同样方式到达其 Ticket，但不授予 authority、不恢复 Ticket。已结束 Ticket 保留 Subscription，包括已关闭或 merge 的 PR：人的消息在同 Mandate 下恢复，无 Eval 或 Decision；bot 与 CI 的消息达不到它，因其 Executor 恢复时会再读 Thread。被人 stop 的 Ticket 什么也收不到。主 Session 仅当消息明确指向某 Ticket 时才把 Thread 挂到已有 Ticket。人可 drop 某 Thread 上全部 Subscription；Executor 只能 drop 自己 Ticket 对无关 Thread 的 Subscription，除 Ticket 起始 Thread 以及 Source 要求其 follow 的 Thread（如其 PR 的 Thread） | 后续讨论可达 Executor，无需再提及 agent |

    人不是领域概念。Trigger 按 Source 所给携带 actor 标识。允许名单是 Policy 中属于某 Source 的部分，使用该 Source 自有身份。同一个人在多个 Source 上有账号，是 agent 在需要时阅读的 prompt 文本。

    Executor 及其 Session 与目录在域外，经 Assignment 连接。

    聚合：Source 持有读取位置。Thread 持有 Subscription、正在运行的 Eval（串行运行，并将期间到达的 Trigger 合并进下一批）及其 Decision 历史。Ticket 持有 Assignment、Channel 与 Input，并以 id 引用 Decision。

## Rejected concepts

!!! note "原文（English）"

    ## Rejected concepts

    | Rejected | Reason |
    | --- | --- |
    | Principal or Membership as an entity, or mapping identities across Sources | A person is Trigger metadata; an allowed list is a Source's part of the Policy; a cross-Source mapping is prompt text |
    | Eval scheduling its own delayed Triggers | A condition that needs waiting is an ordinary Source, so Eval never depends back on Sources |
    | Waiting or revoked Eval states | Waiting belongs to Sources; an Eval that runs out of time queues its Triggers again, or ends `unavailable` |
    | At most one live Ticket per Thread | One Thread can carry several independent pieces of work; the assessment avoids duplicates from the Ticket list |
    | An "append to Ticket" Decision | Appending is Subscription fan-out and needs no Eval |
    | Admission checks on Subscriptions | A Subscription covers the whole Thread; the Mandate separates authorization from context |
    | Correlation that routes | Subscriptions route; Correlation only adds context |
    | Ticket state that follows the DSH Session | A Ticket can be handed off, resumed, or run by several Executors, so it defines its own state |
    | An Executor declaring that a person asked it to stop | Only Eval decides the strength of an end |
    | Cancelling running Tickets on a Policy change | A cancelled change leaves half-finished work; the change is recorded and delivered instead |
    | Locking all of an Executor Session's tools when it fails or blocks | It would refuse a person working directly in the Session, and the report after recovery; pausing automatic input is enough |
    | Inferring "waiting on a person" from a report's free text | The classification needs structured facts; the Executor asks through a structured field |

!!! quote "译文（中文）"

    ## 拒绝的概念

    | 拒绝项 | 理由 |
    | --- | --- |
    | 将 Principal 或 Membership 作为实体，或跨 Source 映射身份 | 人是 Trigger 元数据；允许名单是 Policy 中 Source 的部分；跨 Source 映射是 prompt 文本 |
    | Eval 自行调度延迟 Trigger | 需要等待的条件由普通 Source 承担，Eval 从不反向依赖 Source |
    | Eval 的 waiting 或 revoked 状态 | 等待属于 Source；Eval 超时时将其 Trigger 重新入队，或以 `unavailable` 结束 |
    | 每条 Thread 最多一张 live Ticket | 一条 Thread 可承载多项独立工作；assessment 靠 Ticket 列表避免重复 |
    | 「append to Ticket」类 Decision | 追加是 Subscription 扇出，不需要 Eval |
    | 对 Subscription 做准入检查 | Subscription 覆盖整条 Thread；Mandate 将授权与上下文分开 |
    | 用于路由的 Correlation | 路由靠 Subscription；Correlation 只添上下文 |
    | 跟随 DSH Session 的 Ticket 状态 | Ticket 可交接、恢复或由多个 Executor 运行，故自定状态 |
    | Executor 宣称某人要求它 stop | 只有 Eval 决定结束的强度 |
    | Policy 变更时取消运行中 Ticket | 取消变更会留下半成品；改为记录变更并送达 |
    | Executor Session 失败或 blocked 时锁死其全部工具 | 会拒绝人直接在 Session 中工作及恢复后的报告；暂停自动输入即可 |
    | 从报告自由文本推断「等待某人」 | 分类需要结构化事实；Executor 通过结构化字段提问 |

## What is reusable

!!! note "原文（English）"

    ## What is reusable

    The concepts above and their rules are reusable. The Source implementations, the Policy contents, the Executor and the Channel implementations belong to a deployment. Each Source keeps its own words internally, such as message, chat or notification, and translates them into Triggers.

    | Part | In this repository |
    | --- | --- |
    | Source | Feishu: live events, history recovery, polling of subscribed topics. GitHub: notifications, subscribed Issues and pull requests, CI changes, pull requests merged or closed, and, where switched on, every new Issue, pull request and comment in the repository and the default branch's CI |
    | Policy | Assessment rules, and each Source's allowed list, its sync, its bot list and its admission table |
    | Executor | A DSH Executor Session and a directory per Ticket, in an execution environment whose variables the Executor and the Sources register |
    | Channel | Feishu topic replies, GitHub comments and review thread replies |

!!! quote "译文（中文）"

    ## 何者可复用

    上述概念及其规则可复用。Source 实现、Policy 内容、Executor 与 Channel 实现属于一次部署。每个 Source 内部保留自有用词（如 message、chat、notification），并翻译成 Trigger。

    | 部分 | 本仓库中 |
    | --- | --- |
    | Source | 飞书：实时事件、历史恢复、已订阅主题的轮询。GitHub：通知、已订阅的 Issue 与 pull request、CI 变更、已 merge 或关闭的 pull request；在开启时还包括仓库内每条新 Issue、pull request 与评论，以及默认分支的 CI |
    | Policy | 评估规则，以及每个 Source 的允许名单、同步方式、bot 列表与准入表 |
    | Executor | 每个 Ticket 一个 DSH Executor Session 与一个目录，位于 Executor 与 Source 注册变量的执行环境中 |
    | Channel | 飞书话题回复、GitHub 评论与 review 线程回复 |

## Glossary

!!! note "原文（English）"

    ## Glossary

    A reference list of the terms this and the later documents use, with their Chinese names; the later documents explain the terms not introduced above. The panel shows one language at a time; its (?) button opens this glossary with both languages. The messages agents send to people on GitHub and Feishu use plain language, not these terms.

    | Term | 中文 | Meaning |
    | --- | --- | --- |
    | Agent | 智能体 | The automated team member dsh-superagent runs, acting on Sources through identities such as a GitHub account and a Feishu bot |
    | Main Session | 主 Session | The DSH Session that evaluates Triggers, accepts and dispatches Tickets, and replies; `orchestrator` in code and prompts |
    | Executor Session | 执行器 Session | The DSH Session an Executor runs for one Ticket; `worker` in code and prompts |
    | Source | 来源 | A stream of events: a kind that a plugin provides, plus one instance configuration |
    | Trigger | 触发 | One immutable event from a Source |
    | Thread | 线程 | Triggers in time order; the unit within which Evals run one at a time |
    | Correlation | 关联 | The relation between Threads that share a Trigger; it adds context only |
    | Channel | 渠道 | The write side of a Thread |
    | Policy | 策略 | The versioned rules for accepting work, and each Source's part of it, such as who may ask for work there |
    | Policy version | 策略版本 | The identifier of one combination of rules, Source parts and accept mode; every Mandate records it |
    | Eval | 评估 | The process that turns a batch of Triggers into a Decision |
    | Gate | 准入 | The deterministic first layer of Eval: who may originate work, and with what strength |
    | Assessment | 研判 | The model's judgement during Eval |
    | Decision | 决定 | The result of Eval |
    | Mandate | 授权 | What an accept grants: strength, scope and the Policy version it relied on |
    | Ticket | 工单 | A unit of authorized work |
    | Assignment | 派工 | The binding of a Ticket to one Executor instance |
    | Subscription | 订阅 | The relation between a Ticket and a Thread |
    | Fan-out | 扇出 | Delivering Triggers of a subscribed Thread to a Ticket as context |
    | Executor | 执行器 | The port that runs Assignments: one Executor Session and one directory per Ticket |
    | Execution environment | 执行环境 | Where Executor Sessions and the prepare command run: variables that the Executor and the Sources register, each with a key, a value and a description |
    | Report | 报告 | The state an Assignment reports to its Ticket |
    | Attention | 待人工决定 | A question on a Ticket that waits for a person's answer |
    | Disposition | 去向 | The record of a Trigger being filtered, evaluated or fanned out |
    | Input | 输入 | Context delivered to a Ticket: fanned-out Triggers, Policy changes, resumes and text |
    | Scope | 范围 | The work a Mandate allows |
    | Strength | 强度 | How strong the basis of a Mandate or an Eval is |

    | Group | Values |
    | --- | --- |
    | Decision | accept 接单 · stop 停止 · ignore 忽略 · unavailable 未完成 |
    | Strength | explicit 明确 · inferred 推断 |
    | Assignment | pending 待启动 · running 执行中 · ended 已结束 |
    | Report | progress 进展 · waiting 等待 · ready 就绪 · blocked 受阻 · failed 失败 · dropped 无需处理 |

!!! quote "译文（中文）"

    ## 术语表

    本文及后续文档所用术语的参照列表，附中文名；上文未介绍的术语由后续文档说明。面板一次只显示一种语言；其 (?) 按钮打开本术语表的中英对照。agent 在 GitHub 与飞书上发给人的消息用平实语言，不用这些术语。

    | Term | 中文 | 含义 |
    | --- | --- | --- |
    | Agent | 智能体 | dsh-superagent 运行的自动化团队成员，通过 GitHub 账号、飞书 bot 等身份在 Source 上行动 |
    | Main Session | 主 Session | 评估 Trigger、accept 与派发 Ticket 并回复的 DSH Session；代码与 prompt 中为 `orchestrator` |
    | Executor Session | 执行器 Session | Executor 为一张 Ticket 运行的 DSH Session；代码与 prompt 中为 `worker` |
    | Source | 来源 | 事件流：插件提供的种类，加一份实例配置 |
    | Trigger | 触发 | 来自 Source 的一条不可变事件 |
    | Thread | 线程 | 按时间排序的 Trigger；Eval 串行运行的单位 |
    | Correlation | 关联 | 共享同一条 Trigger 的 Thread 之间的关系；只添上下文 |
    | Channel | 渠道 | Thread 的写侧 |
    | Policy | 策略 | 接受工作的版本化规则，以及每个 Source 在其中承担的部分，例如谁可在该处请求工作 |
    | Policy version | 策略版本 | 一套规则、Source 部分与 accept 模式的标识；每个 Mandate 都记录它 |
    | Eval | 评估 | 将一批 Trigger 变为 Decision 的过程 |
    | Gate | 准入 | Eval 的确定性第一层：谁可发起工作，以及强度 |
    | Assessment | 研判 | Eval 期间模型的判断 |
    | Decision | 决定 | Eval 的结果 |
    | Mandate | 授权 | accept 授予的内容：强度、范围及其所依据的 Policy 版本 |
    | Ticket | 工单 | 一个授权工作单元 |
    | Assignment | 派工 | 一张 Ticket 与一个 Executor 实例的绑定 |
    | Subscription | 订阅 | Ticket 与 Thread 之间的关系 |
    | Fan-out | 扇出 | 将已订阅 Thread 的 Trigger 作为上下文送达 Ticket |
    | Executor | 执行器 | 运行 Assignment 的端口：每个 Ticket 一个 Executor Session 与一个目录 |
    | Execution environment | 执行环境 | Executor Session 与 prepare 命令运行之处：Executor 与 Source 注册的变量，各有键、值与描述 |
    | Report | 报告 | Assignment 向其 Ticket 报告的状态 |
    | Attention | 待人工决定 | Ticket 上等待人回答的问题 |
    | Disposition | 去向 | Trigger 被过滤、评估或扇出的记录 |
    | Input | 输入 | 送达 Ticket 的上下文：扇出的 Trigger、Policy 变更、恢复与文本 |
    | Scope | 范围 | Mandate 允许的工作 |
    | Strength | 强度 | Mandate 或 Eval 依据的强度 |

    | Group | Values |
    | --- | --- |
    | Decision | accept 接单 · stop 停止 · ignore 忽略 · unavailable 未完成 |
    | Strength | explicit 明确 · inferred 推断 |
    | Assignment | pending 待启动 · running 执行中 · ended 已结束 |
    | Report | progress 进展 · waiting 等待 · ready 就绪 · blocked 受阻 · failed 失败 · dropped 无需处理 |
