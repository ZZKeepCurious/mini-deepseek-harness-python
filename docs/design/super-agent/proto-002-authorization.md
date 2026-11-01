# Prototype · 002 授权

<p class="lead">对照 `dsh-superagent-prototype/docs/002-authorization.md`。</p>

!!! info "延伸材料"
    Fast/Slow 与准入表见 [设计抽象 §4](design-abstract.md#sec-4-events)。本节保留全文原文与译文。

!!! note "原文（English）"

    # 002 Authorization

    Authorization determines who may start work and what that work may include. This document builds on [001 Domain model](proto-001-domain.md).

    The host enforces allowed lists, rules and Mandates. Messages, comments and quoted documents are inputs to a decision; they cannot change those permissions.

    ## Two paths for a Trigger

    ### Eval: deciding whether to start work

    The Source records whether a message addresses the agent: `mention` for a mention of the bot or agent account, and `direct` for a direct message. The gate checks the sender and the Source's Policy:

    - An allowed person who addresses the agent makes an explicit request.
    - A person's follow-up on a Thread with Tickets follows the rules below.
    - Other human messages use the admission table for their receiving scope.
    - A bot on the Source's bot list can produce an inferred candidate, never an explicit request. Other bots are not admitted.

    ### Subscriptions: continuing existing work

    Every message on a subscribed Thread reaches its subscribed Tickets as context, including messages from bots and CI. This delivery bypasses the gate and grants no new authority. Each Ticket remains limited by its Mandate.

    A person's message on a Ticket's own Thread belongs to that Ticket's Executor to decide. The same applies when the main Session attaches a message to a Ticket. While an Executor owns the message, it takes no Eval and gets no Decision. Each Executor either handles the request within its Ticket's scope or hands it back for routing elsewhere.

    Ownership continues while the Ticket is live, even if a person removes its Subscription. It also covers an ended Ticket that was not stopped by a person and has the message waiting to resume it.

    The main Session receives a follow-up notice. While an Executor owns the message, the main Session may only stop work on the person's request; accept, attach, ignore and requeue are refused. A stop still passes through the gate with the follow-up strength described below.

    ### Handing back and releasing follow-ups

    An Executor hands a message back once when it asks for work beyond the Ticket's scope or belongs to another Ticket. The Ticket keeps its Subscription. If an attached Thread is unrelated, the Executor hands back the messages it owns there.

    A message goes to a fresh Eval only after no Ticket still owns it. Its candidate includes the Executors' reasons. The gate judges it as an ordinary message on that Thread, and the main Session can accept it, attach it elsewhere or ignore it. Handing back a message the Ticket did not own, such as a bot message or one a person already sent to Eval, queues nothing.

    When a Ticket ends, it releases follow-ups its Executor did not read or take up. These include messages never delivered to the Executor and messages delivered after its last Report or turn end, provided it neither acknowledged, answered nor handed them back. Each released message goes to Eval when no other Ticket owns it. A stop ends only the stopped Ticket's work.

    A stopped Ticket does not send its stop message to Eval again unless another Ticket handed it back. The main Session that stopped it may accept the rest of the message as new work.

    A person can send an owned follow-up to Eval from the panel. If the main Session then attaches it back to a live Ticket, the Executor receives it again. The panel also offers re-evaluation when no effective judgement covers the message.

    ### Context-only subscriptions

    An Executor can follow a related Thread, such as another Issue or pull request, for context. Its messages reach the Ticket but grant no authority or follow-up strength. Human messages there do not resume an ended Ticket unless the main Session attaches a message from that Thread. Notes from the main Session about other work are also context only.

    Human messages that reach no Ticket, including Threads whose Tickets were stopped by a person, take the Eval path. Bot messages take that path independently of their delivery to subscribed Tickets.

    ## Strength

    | Strength | Cases |
    | --- | --- |
    | explicit | A person on the list addresses the agent, or follows up on a Thread that already has a Ticket; a cell of the admission table set to explicit request |
    | inferred | A person not on the list follows up on a Thread with a Ticket, unless their cell of the admission table is explicit; a cell of the admission table set to inferred candidate; a message of a bot on the Source's bot list that addresses the agent or arrives where the Source receives every message |
    | not admitted | Any other bot's message, everything the admission table ignores, and every inferred case while the accept mode is `off` |

    For an unlisted person, an explicit admission cell takes precedence even on a Thread with Tickets and works in every accept mode. Otherwise their follow-up is inferred and is not admitted in `off` mode. Messages on Threads followed only for context use the admission table instead of follow-up strength.

    ## Admission

    Each Source's Policy has an admission table. Its rows are receiving scopes:

    - **Feishu:** each configured chat, other chats the bot receives mentions from, and direct messages. Other chats are received only when **其他群** is set to **仅 @** on the Feishu card.
    - **GitHub:** new Issues, new pull requests, comments and the default branch's CI.

    A row has up to three cells. Each cell decides whether a message becomes an explicit request, an inferred candidate or nothing:

    | Cell | Choices | Default |
    | --- | --- | --- |
    | On the list, not addressing the agent | inferred, ignore | inferred |
    | Not on the list, addressing the agent | explicit, inferred, ignore | ignore |
    | Not on the list, not addressing the agent | inferred, ignore | ignore |

    Cells for unaddressed messages are available only where the Source receives every message, such as a Feishu chat set to **全部消息** or a GitHub scope with its receive-all switch enabled. A CI result has no sender, so its row offers only the last cell. Bot messages use the bot list instead of these cells.

    Setting the unlisted-addressed cell to explicit lets anyone in that scope request work by mentioning the agent. This can be useful for a dedicated support chat. Inferred candidates still depend on the accept mode. Follow-ups normally bypass this table, except that an explicit cell still grants explicit strength.

    The admission table is part of the Policy version. Weakening a cell does not immediately stop open Tickets accepted under it. It can revoke their Mandates when they are revalidated before resuming.

    ### Bots and automated reviewers

    The host recognizes GitHub Apps, logins ending in `[bot]`, and Feishu bots as bots. A Source can list bots such as `dependabot[bot]` whose messages may become inferred candidates. They must either address the agent or post in a scope that receives every message. They cannot make explicit requests or stop Tickets.

    An automated reviewer using an ordinary account counts as a person to the host. Its comments reach subscribed Tickets and generate follow-up notices. Team prompt text must identify such accounts as automated reviewers.

    ## Routing candidates

    The main Session routes explicit candidates with `orchestrator_route`. One call can contain several accept, attach, stop or ignore decisions. Each item is processed independently: a refused item or a missing field does not prevent the others from running.

    An inferred candidate normally needs a full assessment. There is one shortcut: if its card clearly touches no enabled rule, such as plain chat or unrelated discussion, the main Session may ignore it directly. The Decision records `ignoredFromCard` to distinguish this from a rule assessment. The main Session reads the enabled rules in its Policy prompt section.

    Accepting an inferred candidate always requires an assessment. Direct accept refuses a Trigger that the gate did not admit as explicit.

    ## Accept mode and rules

    The accept mode is part of the Policy:

    | Mode | Effect |
    | --- | --- |
    | `off` | Nothing inferred becomes a candidate, such as an unaddressed message, a bot's message or the follow-up of a person not on the list |
    | `observe` (default) | Inferred candidates are assessed; a positive assessment is recorded as ignore marked "would accept", and nothing is accepted |
    | `auto` | A positive assessment of an inferred candidate accepts it |

    Explicit requests work in every mode. Start in `observe` and switch to `auto` after reviewing the recorded Decisions. Each assessment judges every enabled rule exactly once, whether it matches or not. A proactive accept requires at least one matching rule.

    Each rule describes a kind of proactive work. The Ticket's Mandate records the matched rules, and its Executor receives their names and instructions.

    For that Ticket, a rule's process and communication requirements override the defaults. A silence rule can suppress an acknowledgement; a review rule can name the required reviewer and approval before merge. Requirements that apply to every Ticket belong in team prompt sections instead.

    ## Policy version

    Rules, accept mode and each Source's Policy form the Policy version. Every Mandate records the version under which it was accepted.

    A version change discards running Evals and queues their Triggers again. It also sends a policy-changed Input to each affected live Ticket. The Input explains whether:

    - a matched rule changed, including its new text, or no longer applies;
    - the requester may no longer start work; or
    - proactive work is no longer accepted, for example because the mode changed from `auto`.

    Unaffected Tickets receive no notice. The change does not immediately revoke live Tickets' Mandates.

    Source configuration is separate from Policy. It controls what enters the system: which chats are read and whether a scope receives all messages or only mentions. A Mandate records its Source and receiving scope. Resuming needs the Source to still listen to the Thread, but does not require it to still receive every message there.

    Replies wait while their Source is off. They are dropped if the Source no longer listens to their destination.

    A condition on what the Executor may do with its own result, such as when it may merge, does not decide who may authorize work. It is part of the agent's instructions, not of the Policy.

    ## Resume and stop

    An ended Ticket keeps its Mandate. A person's message on one of its own Threads can resume the same work without a new Eval or Decision, regardless of who sent it. Bots and CI do not wake ended Tickets. The Executor decides whether the request fits the scope and ends the round if it does not.

    Only three operations change a Mandate: accept creates it, a person's stop withdraws it, and failed revalidation revokes it. A Ticket stopped by a person stays ended and receives nothing more. Work requested there needs a new accept.

    ### Revalidation before resume

    The Source must still listen to the original Thread. The remaining checks depend on the Mandate's basis:

    - An explicit follow-up Mandate passes.
    - An inferred Mandate needs `auto` mode and its matched rules still enabled.
    - A Mandate admitted by a table cell must still receive at least its original strength under today's table and sender membership. Removing a sender from the list can therefore revoke a Mandate accepted through the listed-sender cell.
    - A bot's Mandate requires that bot to remain on the Source's bot list. Inferred table and bot Mandates also need `auto` mode and enabled matched rules.
    - Other Mandates require the sender to remain on the Source's allowed list.

    If revalidation fails, the Ticket stays ended and takes none of its waiting messages. Each message is treated as having reached no Ticket; if no other Ticket owns it, it goes to Eval. The main Session is not told it reached a worker. This also applies after a processing pause. Attaching to the Ticket is refused, and further work requires a new accept.

    - **A person saying "stop" in a Thread is an ordinary Trigger,** and Eval decides stop. An Executor cannot declare that a person asked it to stop.
    - **Stop in the panel is a command.** It acts on the Ticket directly and is recorded as a manual stop.
    - **Waiting conditions belong to Sources.** A condition such as "no assignee for N hours" would be a Source that waits and checks again itself; Eval never waits.

    ## When Eval does not finish

    The time limit starts when the candidate reaches the main Session. Time spent waiting for delivery does not count, and an undelivered candidate never expires.

    An expired Eval queues its Triggers for another attempt. After two consecutive retries, the third expiry records `unavailable`. An assessment failure also records `unavailable`; a later Trigger can start a new Eval. A Policy change or a decision made against an outdated Policy records `unavailable` and immediately queues the batch under the current version.

    The `evals` health entry reports unresolved unavailable Decisions from the last hour. `scripts/dsh-superagent status` includes that error.

    The panel asks for human attention only when the unavailable Decision is less than 24 hours old, is still the latest Decision on its Thread, has not been dismissed, and the Thread is neither queued nor evaluating. A person can evaluate the Trigger again or dismiss the problem.

!!! quote "译文（中文）"

    # 002 授权

    授权决定谁可以启动工作，以及该工作可以包含什么。本文档建立在 [001 领域模型](proto-001-domain.md) 之上。

    宿主强制执行允许列表、规则与 Mandate。消息、评论与引用的文档是决策的输入；它们不能改变这些权限。

    ## Trigger 的两条路径

    ### Eval：决定是否启动工作

    Source 记录消息是否指向 agent：`mention` 表示 @ 了 bot 或 agent 账号，`direct` 表示私信。门控检查发送者与 Source 的 Policy：

    - 允许列表内且指向 agent 的人构成 **explicit（明确）** 请求。
    - 某人在已有 Ticket 的 Thread 上的跟帖遵循下文规则。
    - 其他人的人类消息按其接收范围查准入表。
    - Source 的 bot 列表上的 bot 只能产生 **inferred（推断）** 候选，永远不能构成 explicit 请求。其他 bot 不准入。

    ### Subscriptions：延续已有工作

    订阅 Thread 上的每条消息都会作为上下文到达其订阅的 Ticket，包括 bot 与 CI 的消息。该投递绕过门控，且不授予新权限。每个 Ticket 仍受其 Mandate 限制。

    某人在 Ticket 自有 Thread 上的消息由该 Ticket 的 Executor 决定如何处理。主 Session 将消息 attach 到 Ticket 时同理。Executor 拥有该消息期间，不进入 Eval、不产生 Decision。Executor 要么在 Ticket 范围内处理请求，要么交还以便别处路由。

    只要 Ticket 仍为 live，拥有权就持续，即使有人移除了其 Subscription。它也涵盖未被人工 stop、且消息正等待 resume 的 ended Ticket。

    主 Session 会收到 follow-up 通知。Executor 拥有消息期间，主 Session 仅可在该人请求时 stop 工作；accept、attach、ignore 与 requeue 均被拒绝。stop 仍经门控，并带有下文所述的 follow-up 强度。

    ### 交还与释放 follow-up

    当消息请求超出 Ticket 范围或属于另一 Ticket 时，Executor 将其交还一次。Ticket 保留 Subscription。若 attach 的 Thread 无关，Executor 交还在该处拥有的消息。

    仅当没有任何 Ticket 仍拥有该消息时，才进入新的 Eval。其候选包含各 Executor 的理由。门控按该 Thread 上的普通消息评判，主 Session 可 accept、attach 到别处或 ignore。交还 Ticket 本不拥有的消息（如 bot 消息或人已送 Eval 的消息）不会排队任何内容。

    Ticket 结束时，释放其 Executor 未读或未接管的 follow-up。包括从未投递给 Executor 的消息，以及在最后一次 Report 或 turn 结束之后投递、且 Executor 既未确认、未答复也未交还的消息。若无其他 Ticket 拥有，每条被释放的消息进入 Eval。stop 仅结束被 stop 的 Ticket 的工作。

    被 stop 的 Ticket 不会再次将其 stop 消息送 Eval，除非另一 Ticket 交还了该消息。执行 stop 的主 Session 可将消息的其余部分作为新工作 accept。

    人可从面板将已拥有的 follow-up 送 Eval。若主 Session 随后将其 attach 回 live Ticket，Executor 会再次收到。当没有有效 judgement 覆盖该消息时，面板也提供重新评估。

    ### 仅上下文的订阅

    Executor 可 follow 相关 Thread（如另一个 Issue 或 pull request）以获取上下文。其消息到达 Ticket，但不授予权限或 follow-up 强度。该处的人类消息不会 resume ended Ticket，除非主 Session attach 来自该 Thread 的消息。主 Session 关于其他工作的说明同样仅为上下文。

    未到达任何 Ticket 的人类消息（包括 Ticket 被人 stop 的 Thread）走 Eval 路径。bot 消息走该路径，与其是否作为上下文投递到订阅 Ticket 无关。

    ## 强度

    | 强度 | 情形 |
    | --- | --- |
    | explicit | 列表内的人指向 agent，或在已有 Ticket 的 Thread 上跟帖；准入表单元格设为 explicit request |
    | inferred | 非列表的人在有 Ticket 的 Thread 上跟帖（除非其准入表单元格为 explicit）；准入表单元格设为 inferred candidate；Source bot 列表上的 bot 指向 agent，或消息到达 Source 接收全部消息的范围 |
    | not admitted | 任何其他 bot 的消息、准入表 ignore 的一切，以及 accept 模式为 `off` 时的所有 inferred 情形 |

    对未列入列表的人，explicit 准入单元格优先，即使 Thread 上已有 Ticket，且在任意 accept 模式下有效。否则其跟帖为 inferred，在 `off` 模式下不准入。仅因上下文而 follow 的 Thread 上的消息使用准入表，而非 follow-up 强度。

    ## 准入

    每个 Source 的 Policy 有一张准入表。行是接收范围：

    - **飞书：** 每个已配置群、bot 收到 @ 的其他群，以及私信。其他群仅在飞书卡片将 **其他群** 设为 **仅 @** 时才会被接收。
    - **GitHub：** 新 Issue、新 pull request、评论，以及默认分支的 CI。

    每行最多三个单元格。每个单元格决定消息成为 explicit request、inferred candidate 还是 nothing：

    | 单元格 | 选项 | 默认 |
    | --- | --- | --- |
    | 在列表上，未指向 agent | inferred, ignore | inferred |
    | 不在列表上，指向 agent | explicit, inferred, ignore | ignore |
    | 不在列表上，未指向 agent | inferred, ignore | ignore |

    未指向 agent 的消息的单元格，仅在 Source 接收全部消息处可用，例如飞书群设为 **全部消息**，或 GitHub 范围开启 receive-all。CI 结果无发送者，其行仅提供最后一个单元格。bot 消息使用 bot 列表，而非这些单元格。

    将「非列表且 @ agent」单元格设为 explicit，则该范围内任何人 @ agent 即可请求工作；专用支持群可能有用。inferred 候选仍取决于 accept 模式。跟帖通常绕过此表，但 explicit 单元格仍授予 explicit 强度。

    准入表属于 Policy 版本。削弱某单元格不会立即停止在其下 accept 的开放 Ticket；但在 resume 前 revalidation 时可撤销其 Mandate。

    ### Bot 与自动化 reviewer

    宿主将 GitHub Apps、以 `[bot]` 结尾的 login 与飞书 bot 识别为 bot。Source 可列出如 `dependabot[bot]` 等 bot，其消息可成为 inferred 候选。它们必须指向 agent，或在接收全部消息的范围发帖。它们不能构成 explicit 请求，也不能 stop Ticket。

    使用普通账号的自动化 reviewer 在宿主侧视为人。其评论到达订阅 Ticket 并产生 follow-up 通知。团队 prompt 文本必须将此类账号标识为自动化 reviewer。

    ## 路由候选

    主 Session 用 `orchestrator_route` 路由 explicit 候选。一次调用可含多个 accept、attach、stop 或 ignore 决策。各项独立处理：某项被拒绝或缺字段不会阻止其余项执行。

    inferred 候选通常需要完整 assessment。有一个捷径：若其卡片明显未触及任何已启用规则（如闲聊或无关讨论），主 Session 可直接 ignore。Decision 记录 `ignoredFromCard`，以区别于规则 assessment。主 Session 在其 Policy prompt 段读取已启用规则。

    accept inferred 候选始终需要 assessment。直接 accept 会拒绝门控未准入为 explicit 的 Trigger。

    ## Accept 模式与规则

    accept 模式是 Policy 的一部分：

    | 模式 | 效果 |
    | --- | --- |
    | `off` | 任何 inferred 都不会成为候选，例如未 @ 的消息、bot 消息、或非列表者的跟帖 |
    | `observe`（默认） | 对 inferred 候选做 assessment；正面 assessment 记为带「would accept」标记的 ignore，且不 accept |
    | `auto` | inferred 候选的正面 assessment 会 accept |

    explicit 请求在任意模式下有效。从 `observe` 开始，审阅记录的 Decision 后再切到 `auto`。每次 assessment 对每个已启用规则恰好评判一次，无论是否匹配。主动 accept 至少需要一条匹配规则。

    每条规则描述一类主动工作。Ticket 的 Mandate 记录匹配的规则，其 Executor 收到规则名与说明。

    对该 Ticket，规则的流程与沟通要求覆盖默认值。silence 规则可抑制确认；review 规则可指定 merge 前所需的 reviewer 与批准。适用于每个 Ticket 的要求应写在团队 prompt 段，而非规则里。

    ## Policy 版本

    规则、accept 模式与各 Source 的 Policy 构成 Policy 版本。每个 Mandate 记录 accept 时的版本。

    版本变更丢弃运行中的 Eval 并重新排队其 Trigger。也向每个受影响的 live Ticket 发送 policy-changed Input。Input 说明是否：

    - 某条匹配规则已变更（含新文本）或不再适用；
    - 请求者可能不再能启动工作；或
    - 不再 accept 主动工作，例如模式从 `auto` 改变。

    未受影响的 Ticket 无通知。变更不会立即撤销 live Ticket 的 Mandate。

    Source 配置与 Policy 分离。它控制系统入口：读哪些群、某范围是收全部消息还是仅 @。Mandate 记录其 Source 与接收范围。resume 需要 Source 仍监听该 Thread，但不要求仍接收该处全部消息。

    Source 关闭时回复等待。若 Source 不再监听目标，回复被丢弃。

    关于 Executor 对其结果可做什么的条件（如何时可 merge）不决定谁可授权工作。它属于 agent 说明，不属于 Policy。

    ## Resume 与 stop

    ended Ticket 保留其 Mandate。某人在其自有 Thread 之一上的消息可在无新 Eval 或 Decision 的情况下 resume 同一工作，与发送者是谁无关。bot 与 CI 不会唤醒 ended Ticket。Executor 判断请求是否在范围内；若否则结束本轮。

    仅三种操作改变 Mandate：accept 创建；人的 stop 撤回；revalidation 失败撤销。被人 stop 的 Ticket 保持 ended 且不再接收任何内容。该处请求的工作需要新的 accept。

    ### Resume 前的 revalidation

    Source 仍须监听原始 Thread。其余检查取决于 Mandate 的依据：

    - explicit follow-up 的 Mandate 通过。
    - inferred Mandate 需要 `auto` 模式且匹配规则仍启用。
    - 经准入表单元格准入的 Mandate 须在当前表与发送者成员关系下仍至少获得原强度。从列表移除发送者因此可撤销经「列表内发送者」单元格 accept 的 Mandate。
    - bot 的 Mandate 要求该 bot 仍在 Source 的 bot 列表上。inferred 表与 bot Mandate 还需要 `auto` 模式与已启用的匹配规则。
    - 其他 Mandate 要求发送者仍在 Source 的允许列表上。

    若 revalidation 失败，Ticket 保持 ended 且不接收任何等待消息。每条消息视为未到达任何 Ticket；若无其他 Ticket 拥有，则进入 Eval。主 Session 不会被告知消息到达了 worker。processing pause 之后同样适用。attach 到该 Ticket 被拒绝，进一步工作需要新 accept。

    - **某人在 Thread 中说「stop」是普通 Trigger，** 由 Eval 决定 stop。Executor 不能宣称某人要求它 stop。
    - **面板中的 stop 是指令。** 直接作用于 Ticket，记为 manual stop。
    - **等待条件属于 Source。** 如「N 小时无 assignee」之类的条件应是 Source 自行等待并再次检查；Eval 从不等待。

    ## Eval 未完成时

    时限从候选到达主 Session 时开始。等待投递的时间不计入，未投递的候选永不过期。

    过期的 Eval 将其 Trigger 排队重试。连续两次重试后，第三次过期记录 `unavailable`。assessment 失败也记录 `unavailable`；后续 Trigger 可启动新 Eval。Policy 变更或针对过时 Policy 的决策记录 `unavailable`，并立即在当前版本下重新排队该批次。

    `evals` 健康项报告过去一小时内未解决的 unavailable Decision。`scripts/dsh-superagent status` 包含该错误。

    面板仅在 unavailable Decision 不足 24 小时、仍是其 Thread 上最新 Decision、未被 dismiss，且 Thread 既未排队也未在评估时，才请求人工关注。人可再次评估 Trigger 或 dismiss 该问题。
