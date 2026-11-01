# Prototype · 003 执行

<p class="lead">对照 `dsh-superagent-prototype/docs/003-execution.md`。</p>

!!! info "延伸材料"
    Worker 层与异步 shell 见 [设计抽象 §3](design-abstract.md#sec-3-two-layer)–[§5](design-abstract.md#sec-5-interaction)。本节保留全文原文与译文。

!!! note "原文（English）"

    # 003 Execution

    This document follows an accepted Ticket through execution, delivery and recovery. It builds on [001 Domain model](proto-001-domain.md) and [002 Authorization](proto-002-authorization.md).

    ## Ticket stages

    Each open Ticket has one stage, determined by what it is waiting for. Read the table from top to bottom; the first matching row wins.

    | Stage | Condition | Panel label |
    | --- | --- | --- |
    | person / delivery | Delivering an Input to the Executor Session failed; it is retried after 2 seconds, then after a delay that doubles up to a minute, and a success, a new Assignment or the Ticket's end clears it | Input undelivered |
    | person / answer | It has an Attention | Awaiting answer |
    | person / review | The latest Report is `ready` | Awaiting review |
    | person / blocked, failed | The latest Report is `blocked` or `failed`, and no person's message reached the Ticket since | Blocked · Failed |
    | working / queued | No Assignment, or a pending one | Queued |
    | idle | The latest Report is `waiting`, or the Assignment ended | Waiting for a Trigger |
    | working | Everything else | Working |

    A `person` stage means the Ticket cannot continue without human input. A new human message releases a `blocked` or `failed` Ticket to its Executor, so it moves to the next matching stage, usually `working`.

    An `idle` Ticket has finished its turn without an outstanding question. A Trigger on a subscribed Thread can continue it.

    ## The Executor

    Each Ticket gets an Executor Session named `worker-<ticket id>` and a directory named after the Ticket's short name. Directories are created under the configured execution root, which defaults to `<stateDir>/work`.

    Executor Sessions run commands without approval as the operator's OS user. Their `dsh-superagent-worker` preset contains the tools needed for work and moves foreground shell commands to background jobs after 5 seconds. This lets a worker receive new messages at the next step instead of waiting for a long command to finish.

    The main Session uses `dsh-superagent-router`, with the same 5-second shell and job-wait limits and a smaller tool set for routing. Sessions retain the preset selected at creation. Worker subagents use DeepSeek Harness's `standard` preset.

    ### Background work and worker slots

    When a turn ends with shell jobs or subagents still running, the Ticket keeps its worker slot and stays working. It is not reported as waiting, and the main Session receives no waiting report. A completed job or subagent wakes the Executor with its result in the same slot. Once a turn ends with no background work, the Executor reports waiting normally.

    After each turn, background work has at most 30 minutes to finish. At that limit, shell jobs are killed and the Ticket is reported waiting. The report includes the Executor's last words and which jobs were killed or subagents remain active. Subagents are allowed to continue. The Executor reads killed-job results on its next turn.

    A handoff to a person frees the slot immediately and leaves the Report in effect. This applies after a `ready` Report or a question, unless another Input arrived since, and after any `blocked` or `failed` Report.

    At handoff, subagents stop. Shell jobs, such as a server offered for review, may continue until the Assignment ends. They hold no slot and are exempt from the 30-minute limit, including after later turns. Their completion does not wake the Executor; it reads the results on its next turn.

    A person can also start a turn by writing directly in the Executor Session. Its background work follows the same rules.

    When an Assignment ends, its shell jobs are killed without waking the Session. Its subagents are released, and any completion turns they wake are dropped. The replacement Assignment and its waiting Inputs start after that shutdown finishes.

    Stopping or restarting the plugin inside the DeepSeek Harness process uses the same shutdown path. Inputs not yet taken into Session history are retained. Shutdown waits up to 10 seconds for each worker's background work; a process restart ends that work outright. An Assignment interrupted while busy restarts with a recovery note.

    Worker subagents, including nested ones, use DeepSeek Harness's `standard` preset with the worker's permissions. They retain only the worker's allowed work tools, and lose those tools when the Ticket ends. Under `standard`, a shell command moves to the background after 60 seconds. A subagent's jobs are killed when it ends.

    ### Preparing a Ticket directory

    Before the first Assignment, if the Ticket directory does not exist, the prepare command runs once with `sh` in the execution root. It has ten minutes to finish. Without a prepare command, the worker starts in an empty directory.

    A failed command fails the Ticket and records its output. A retry or a person's message on one of the Ticket's Threads tries preparation again. A typical command uses `git worktree` to create a checkout. A pull request belongs to the Ticket when its branch's last path segment matches the Ticket's short name.

    ### Concurrency

    A Ticket holds a worker slot from the start of a launch, including preparation, or from the delivery that wakes its idle Executor. It also holds the slot throughout an active turn.

    The configured concurrency limit applies to these slots, with one extra slot for a person's message or a status request. Such a wake can use the extra slot when normal slots are full, and takes the next free slot before a new launch. New work therefore cannot prevent a person from continuing an existing Ticket.

    Other Inputs, such as CI results, wait for a normal slot. A launch waits in `working / queued` until a slot is free. Launches run concurrently, so a slow prepare command delays only its own Ticket.

    ### Execution variables

    The prepare command and Executor instructions receive the same execution variables. The command reads environment variables; the Executor reads their values and descriptions in its prompt. None contains credentials.

    | Variable | Registered by | Value |
    | --- | --- | --- |
    | `DSH_SUPERAGENT_DIR`, `DSH_SUPERAGENT_NAME`, `DSH_SUPERAGENT_TICKET` | Executor | The Ticket's directory, short name and id |
    | `DSH_SUPERAGENT_BLOCKED_COMMANDS` | Executor | Commands the Executor may not run |
    | `DSH_SUPERAGENT_GITHUB_REPOSITORY` | GitHub | The repository the Source watches, `owner/name` |
    | `DSH_SUPERAGENT_GITHUB_ACCOUNT`, `DSH_SUPERAGENT_GITHUB_COMMIT_EMAIL` | GitHub | The agent account and its commit email |
    | `DSH_SUPERAGENT_GITHUB_NUMBER`, `DSH_SUPERAGENT_GITHUB_PR_HEAD` | GitHub | The Issue or pull request that started the Ticket, and its head ref when it is a pull request |
    | `DSH_SUPERAGENT_FEISHU_BOT`, `DSH_SUPERAGENT_FEISHU_CHAT` | Feishu | The bot, and the chat that started the Ticket |

    Ticket-specific values come from its directory and originating Trigger. Other values come from the current deployment settings. Only the panel can change execution environment settings; text sent to the main Session cannot change the configured shell command.

    Workers reach GitHub through the agent account's `gh` and Feishu through the bot's `lark-cli`. Blocked commands fail when invoked by name.

    These wrappers and directories establish defaults, not a security boundary. A worker can still execute a program by absolute path.

    ## Merging

    Auto-merge is governed by the **Delivery requirements** prompt. The host has no merge-specific decision logic.

    By default, the Executor never enables auto-merge as the agent account: GitHub attributes the merge to whoever enabled it. Delivery requirements must name another account's CLI for merging. Without one, people merge the pull request.

    The default instructions allow auto-merge only for a pull request opened for the Ticket, when it is ready, the required approvals cover the head commit, and additions plus deletions total fewer than 200 lines. Before pushing another revision, the Executor withdraws auto-merge.

    Reviews without a body, such as approvals, also arrive as Triggers and can wake an Executor waiting for review. These instructions govern the agent, not GitHub's merge rules. To enforce approvals, require them in branch protection and dismiss stale approvals. Omit the merge CLI or remove the auto-merge paragraph to require manual merging.

    ## How a Ticket ends

    - **Ready without a pull request:** a `ready` Report ends the Ticket as completed.
    - **Nothing to do:** an Executor that finds no work for a person to act on, such as a request that does not reproduce or is superseded, reports `dropped`. The Ticket ends without a message, unless a matched rule asks for one, and a person's later message on its Threads resumes it.

    ### Pull requests

    A Ticket follows the pull request it started from once its Executor reports it. It also follows each pull request opened from its branch once reported by the Executor or found by the GitHub Source. Reopened pull requests are followed again.

    The Ticket is done when none of the pull requests it opened remain open. If it opened none, all pull requests it follows must be closed. It then ends with the outcome of the last relevant close and keeps its Subscriptions, allowing a later human comment to resume it.

    A close does not end the Ticket while resumed, retried or recovered work is unfinished. It also waits if the Executor acknowledged or answered a follow-up it owns but has not since made a non-progress Report or ended a turn after reading that message. A later `ready` Report ends the Ticket once the same pull-request completion rule is satisfied.

    Chat Channels receive one final notice about the pull-request outcome. GitHub already displays the outcome on the pull request itself. Removing a Ticket's Subscription to a pull request also stops it following that pull request.

    "Awaiting review" therefore means a `ready` Report with a pull request.

    ### Follow-ups and context Threads

    The Executor decides human follow-ups on its Ticket's own Threads. It handles in-scope requests and hands back requests beyond its scope or for another Ticket. Once no Executor owns the message, it becomes a candidate for the main Session. The main Session may otherwise act on the follow-up only to stop work at the person's request.

    An Input counts as read after the Session takes it into a step and subsequently reports or ends a turn. When a Ticket ends, unread messages that were not acknowledged, answered or handed back return to Eval, as described in [002 Authorization](proto-002-authorization.md). Read messages left without action, such as thanks, do not.

    Threads followed only for context still deliver messages, but do not give the Ticket ownership of human follow-ups or resume ended Tickets. Those messages can be assessed independently.

    Sources poll subscribed Threads while a live Ticket subscribes, and for two weeks after the later of the last Trigger and the subscribing Tickets' end. Afterwards, messages arrive only through ordinary intake: GitHub notifications or Feishu live events.

    ## Failure and recovery

    ### Executor blocked or failed

    When an Executor Session reports `blocked` or `failed`, or fails to start or errors, which records `failed`, the Ticket takes no automatic Input.

    A passing failure of the model provider or the network, an HTTP 408, 429 or 5xx or a refused, reset or timed-out connection, is tried again first, three times with a growing backoff: a launch runs again, and a worker whose turn failed is told to go on; a failed prepare command is not tried again.

    Once failed, CI and bot messages are recorded on it, but they are not delivered and do not wake the Session.

    Its tools stay available, so a person can continue in the Session directly.

    A launch that failed is not tried again on its own.

    A person's message, a later Report that is neither `blocked` nor `failed`, or a retry ends the hold.

    It is worked out from the stored Reports and Inputs, so a restart keeps it.

    ### Retry

    Available while an open Ticket is in `person / delivery`, `person / blocked` or `person / failed`: a new Assignment, which starts its worker again and takes the Inputs that wait, is the remedy for each.

    Once a person's message reached a blocked or failed Ticket, the Executor has the message, so the Ticket shows as working, or as queued when a failed launch starts again, and offers no retry, unless delivering that message failed.

    A retry ends the current Assignment and starts a new one in the same Executor Session, directory, branch and pull request.

    The Executor model applies only when a Session is created; an existing Session keeps its model.

    ### Restart

    Running Assignments end with "host restarted" and new ones start with a recovery note that tells the Executor to check its directory, git history and GitHub before repeating any outside action, oldest Ticket first.

    Assignments that are waiting, ready, blocked or failed stay as they are, unless an Input reached them after that Report, in which case they start again too.

    Sessions, directories, Subscriptions, pending messages and the stored work below survive a restart, and so does the record of which messages each Executor Session took into its history, which a step does once it claimed them and before its model call: a message taken by a turn the restart cut off counts as read once a Report or the end of a turn follows, while one a step claimed but a cancel or error cut off before the history counts as unread.

    A retry or a restart gives the new Assignment again the messages delivered to the Assignment it replaces that its Executor Session did not take, which a cancel or a cut-off step may have dropped; a copy still waiting in the Session, kept by a restart or persisted across a crash, is taken out before the new Assignment's first message, so the new delivery is the only copy.

    ### Stored work

    What dsh-superagent does after a decision, such as relaying a candidate, a follow-up or a report to the main Session, launching an Assignment or delivering an Input, is stored first and done afterwards.

    A relay or a delivery that fails, such as when its Session cannot be reached, is retried after 2 seconds, then after a delay that doubles up to a minute; a launch that fails fails its Ticket, unless the failure was a passing one, as above.

    Each kind of work reports an error among the overview's system problems only after one item failed three times in a row; a failed Input delivery shows on its Ticket instead.

    Relays to the main Session keep their order, and so do notices; a failed one holds back the later ones of its kind.

    ### Source failure

    The GitHub Source reads each Issue, pull request and Thread on its own: a failed read keeps its error on its Thread, is read again on the next poll, and counts towards an error of the Source, which it reports after three failed polls in a row by default; any successful poll resets the count.

    GitHub answers 404 also when the agent lost access to a private repository, so a 404 is retried.

    A notified Issue or pull request whose reads keep failing is dropped a day after its first failed read, and at once when GitHub reports it as deleted; it then no longer fails the poll, its Thread keeps the error, and the Source's state names it.

    Its read position stays where it was, so a later notification or a poll of its Thread reads what the failed reads missed.

    The Feishu event consumer reports every failure, and polling of subscribed topics keeps each failure on its Thread.

    A malformed item in a GitHub list or a Feishu page is skipped, and the Source's state counts it.

    ### Channel failure

    Outgoing messages are stored and sent as stored work, retried with backoff from 5 seconds, doubling up to 300.

    A Ticket's messages to one conversation go out in the order they were queued, so its acknowledgement comes before its later answer or result: a message waits while an earlier one of the same Ticket to the same conversation has not gone out, until that one has failed three times in a row and shows as a problem.

    Messages of other Tickets, and messages of no Ticket, do not wait for it.

    A message with failed attempts is a system problem until it goes out or is withdrawn.

    A message is dropped only when its Source no longer listens to the conversation, or when the Source answers that the conversation can never receive it; while its Source is off, it waits.

    The Sources answer that for a deleted Issue or a locked conversation on GitHub, and for a recalled message or one Feishu no longer finds on Feishu.

    A reply to a review comment that no longer exists goes to the pull request's conversation instead.

    A dropped message keeps its record, with when and why it was dropped and the Ticket it was for, so it is never queued again; a dropped acknowledgement reads as dropped, not sent.

    ### Pause processing

    Stops dispatch, new Evals, Eval timeouts, Input delivery and replies; a launch already under way waits until processing resumes.

    Sources keep recording Triggers, and a running Executor turn is not interrupted.

    A person's message to an ended Ticket waits: the first pass after the pause resumes the Ticket, if its Mandate holds under the Policy then.

    An Eval in progress does not expire during a pause; its deadline moves later by the pause's length.

    ### Invalid configuration

    A stored Source configuration that its kind rejects disables that Source instance.

!!! quote "译文（中文）"

    # 003 执行

    本文档跟踪已被 accept 的 Ticket：执行、投递与恢复。建立在 [001 领域模型](proto-001-domain.md) 与 [002 授权](proto-002-authorization.md) 之上。

    ## Ticket 阶段

    每个未关闭的 Ticket 只有一个阶段，由它在等待什么决定。表格自上而下阅读；首条匹配的行生效。

    | 阶段 | 条件 | 面板标签 |
    | --- | --- | --- |
    | person / delivery | 向 Executor Session 投递 Input 失败；2 秒后重试，随后延迟加倍直至一分钟；成功、新 Assignment 或 Ticket 结束可清除 | Input 未投递 |
    | person / answer | 存在 Attention | 等待答复 |
    | person / review | 最新 Report 为 `ready` | 等待审查 |
    | person / blocked, failed | 最新 Report 为 `blocked` 或 `failed`，且此后无人消息到达该 Ticket | 阻塞 · 失败 |
    | working / queued | 无 Assignment，或有 pending 的 Assignment | 排队中 |
    | idle | 最新 Report 为 `waiting`，或 Assignment 已结束 | 等待 Trigger |
    | working | 其余一切 | 工作中 |

    `person` 阶段表示 Ticket 无人输入无法继续。新的人消息会把 `blocked` 或 `failed` 的 Ticket 交还给其 Executor，从而进入下一条匹配阶段，通常是 `working`。

    `idle` 的 Ticket 在本轮结束时没有未决问题。已订阅 Thread 上的 Trigger 可以继续它。

    ## Executor

    每个 Ticket 获得名为 `worker-<ticket id>` 的 Executor Session，以及以 Ticket 短名命名的目录。目录创建在配置的执行根下，默认为 `<stateDir>/work`。

    Executor Session 以操作者的 OS 用户身份、无需审批运行命令。其 `dsh-superagent-worker` preset 包含工作所需工具，并在 5 秒后将前台 shell 命令移入后台 job。这样 worker 可在下一步接收新消息，而不必等长命令结束。

    主 Session 使用 `dsh-superagent-router`，同样 5 秒 shell 与 job 等待上限，工具集更小用于路由。Session 保留创建时选定的 preset。Worker 子 agent 使用 DeepSeek Harness 的 `standard` preset。

    ### 后台工作与 worker slot

    若一轮结束时仍有 shell job 或 subagent 在跑，Ticket 保留 worker slot 并保持 working。不会上报 waiting，主 Session 也收不到 waiting 报告。完成的 job 或 subagent 在同一 slot 内用结果唤醒 Executor。一旦一轮结束时无后台工作，Executor 正常上报 waiting。

    每轮之后，后台工作最多还有 30 分钟完成。到限则杀死 shell job，Ticket 上报 waiting。报告含 Executor 最后的话、哪些 job 被杀死或哪些 subagent 仍活跃。Subagent 允许继续。Executor 在下一轮读取被杀死 job 的结果。

    交给人立即释放 slot，Report 继续生效。适用于 `ready` Report 或提问之后（除非此后又有 Input 到达），以及任何 `blocked` 或 `failed` Report 之后。

    交给人时 subagent 停止。Shell job（例如供审查的服务器）可继续到 Assignment 结束。它们不占 slot，不受 30 分钟上限约束，包括后续轮次。其完成不唤醒 Executor；Executor 在下一轮读取结果。

    人也可直接在 Executor Session 中书写以开始一轮。其后台工作遵循相同规则。

    Assignment 结束时，其 shell job 被杀死且不唤醒 Session。其 subagent 被释放，它们唤醒的任何完成轮次被丢弃。替代 Assignment 及其等待的 Input 在该关闭完成后开始。

    在 DeepSeek Harness 进程内停止或重启插件走同一关闭路径。尚未进入 Session 历史的 Input 保留。关闭时每个 worker 的后台工作最多等 10 秒；进程重启则直接终止该工作。忙碌中被中断的 Assignment 以恢复说明重启。

    Worker subagent（含嵌套）使用 DeepSeek Harness 的 `standard` preset 及 worker 权限。仅保留 worker 允许的工作工具，Ticket 结束时失去这些工具。在 `standard` 下，shell 命令 60 秒后移入后台。Subagent 结束时其 job 被杀死。

    ### 准备 Ticket 目录

    首次 Assignment 之前，若 Ticket 目录不存在，则在执行根下用 `sh` 运行一次 prepare 命令。最多十分钟完成。无 prepare 命令时 worker 在空目录启动。

    命令失败则 Ticket 失败并记录输出。Retry 或人在 Ticket 某 Thread 上的消息会再次尝试准备。典型命令用 `git worktree` 创建 checkout。当分支最后路径段与 Ticket 短名匹配时，该 pull request 属于该 Ticket。

    ### 并发

    Ticket 从 launch 开始（含准备）或从唤醒 idle Executor 的投递起占用 worker slot。活跃轮次全程也占用 slot。

    配置的并发上限适用于这些 slot，另有一个额外 slot 供人消息或状态查询。正常 slot 满时此类唤醒可用额外 slot，并在新 launch 之前占用下一个空闲 slot。因此新工作不能阻止人继续已有 Ticket。

    其他 Input（如 CI 结果）等待正常 slot。Launch 在 `working / queued` 等待直到 slot 空闲。Launch 可并发，慢的 prepare 只延迟自己的 Ticket。

    ### 执行变量

    Prepare 命令与 Executor 指令接收相同的执行变量。命令读环境变量；Executor 在 prompt 中读其值与说明。均不含凭据。

    | 变量 | 注册方 | 值 |
    | --- | --- | --- |
    | `DSH_SUPERAGENT_DIR`、`DSH_SUPERAGENT_NAME`、`DSH_SUPERAGENT_TICKET` | Executor | Ticket 目录、短名与 id |
    | `DSH_SUPERAGENT_BLOCKED_COMMANDS` | Executor | Executor 不得运行的命令 |
    | `DSH_SUPERAGENT_GITHUB_REPOSITORY` | GitHub | Source 监视的仓库，`owner/name` |
    | `DSH_SUPERAGENT_GITHUB_ACCOUNT`、`DSH_SUPERAGENT_GITHUB_COMMIT_EMAIL` | GitHub | Agent 账号及其 commit 邮箱 |
    | `DSH_SUPERAGENT_GITHUB_NUMBER`、`DSH_SUPERAGENT_GITHUB_PR_HEAD` | GitHub | 启动 Ticket 的 Issue 或 pull request；若为 PR 则含 head ref |
    | `DSH_SUPERAGENT_FEISHU_BOT`、`DSH_SUPERAGENT_FEISHU_CHAT` | Feishu | Bot，以及启动 Ticket 的会话 |

    Ticket 特有值来自其目录与 originating Trigger。其余值来自当前部署设置。仅 panel 可改执行环境设置；发给主 Session 的文本不能改配置的 shell 命令。

    Worker 经 agent 账号的 `gh` 访问 GitHub，经 bot 的 `lark-cli` 访问 Feishu。Blocked 命令按名称调用时失败。

    这些包装与目录建立默认值，不是安全边界。Worker 仍可通过绝对路径执行程序。

    ## 合并

    Auto-merge 由 **Delivery requirements** prompt 管辖。宿主无合并专用决策逻辑。

    默认 Executor 不以 agent 账号启用 auto-merge：GitHub 将合并归因于启用者。Delivery requirements 须指定另一账号的 CLI 用于合并。没有则由人工合并 pull request。

    默认指令仅当：为 Ticket 打开的 pull request、已 ready、所需 approval 覆盖 head commit、增删合计少于 200 行时，才允许 auto-merge。推送新修订前 Executor 撤回 auto-merge。

    无正文的 review（如 approval）也作为 Trigger 到达，可唤醒等待审查的 Executor。这些指令约束 agent，不是 GitHub 合并规则。要强制 approval，在分支保护中要求并 dismiss stale approval。省略 merge CLI 或删除 auto-merge 段落则要求手动合并。

    ## Ticket 如何结束

    - **Ready 且无 pull request：** `ready` Report 以 completed 结束 Ticket。
    - **无事可做：** Executor 发现无人可处理的工作（例如无法复现或被取代的请求）时上报 `dropped`。Ticket 无消息结束，除非匹配规则要求消息；人稍后在其 Thread 上的消息可恢复它。

    ### Pull request

    Ticket 在其 Executor 上报后跟随它启动时的 pull request。也跟随从其分支打开的每个 pull request（由 Executor 上报或 GitHub Source 发现）。Reopen 的 pull request 再次跟随。

    当 Ticket 打开的所有 pull request 都不再 open 时完成。若未打开任何 PR，则其跟随的所有 PR 须 closed。随后以最后一次相关 close 的结果结束，并保留 Subscriptions，允许日后人工评论恢复。

    在恢复、重试或恢复中的工作未完成时，close 不结束 Ticket。若 Executor 已 acknowledge 或答复其拥有的 follow-up，但此后未做非进展 Report 或在读该消息后未结束一轮，也等待。稍后 `ready` Report 在满足相同 PR 完成规则时结束 Ticket。

    Chat Channel 收到关于 PR 结果的一条最终通知。GitHub 已在 PR 本身展示结果。移除 Ticket 对某 PR 的 Subscription 也停止跟随该 PR。

    因此「等待审查」指带 pull request 的 `ready` Report。

    ### Follow-up 与上下文 Thread

    Executor 决定其 Ticket 自有 Thread 上的人工 follow-up。处理范围内请求；超出范围或属于其他 Ticket 的请求交还。一旦无 Executor 拥有该消息，它成为主 Session 的 candidate。主 Session 否则仅可在人要求时停止工作。

    Input 在 Session 将其纳入一步并随后 report 或结束一轮后计为已读。Ticket 结束时，未 acknowledge、未答复、未交还的未读消息回到 Eval，见 [002 授权](proto-002-authorization.md)。已读但未行动的消息（如感谢）不回到 Eval。

    仅作上下文跟随的 Thread 仍投递消息，但不赋予 Ticket 对人工 follow-up 的所有权，也不恢复已结束 Ticket。这些消息可独立评估。

    Source 在 live Ticket 订阅期间轮询已订阅 Thread，并在最后 Trigger 与订阅 Ticket 结束二者中较晚者之后两周内继续。之后消息仅通过普通 intake 到达：GitHub 通知或 Feishu 实时事件。

    ## 失败与恢复

    ### Executor blocked 或 failed

    当 Executor Session 上报 `blocked` 或 `failed`，或启动失败或出错（记为 `failed`）时，Ticket 不自动接收 Input。

    模型提供商或网络的 passing failure、HTTP 408/429/5xx 或连接 refused/reset/timeout，先重试三次、递增退避：再次 launch；turn 失败的 worker 被告知继续；失败的 prepare 命令不重试。

    一旦 failed，CI 与 bot 消息记录在其上，但不投递、不唤醒 Session。

    其工具仍可用，人可直接在 Session 中继续。

    失败的 launch 不会自行重试。

    人消息、后续既非 `blocked` 也非 `failed` 的 Report，或 retry 结束 hold。

    由存储的 Report 与 Input 推导，重启后仍保持。

    ### Retry

    在 open Ticket 处于 `person / delivery`、`person / blocked` 或 `person / failed` 时可用：新 Assignment 再次启动 worker 并取走等待的 Input，是各自的补救。

    一旦人消息到达 blocked 或 failed 的 Ticket，Executor 已有该消息，Ticket 显示为 working，或 failed launch 再次启动时为 queued，且不提供 retry，除非投递该消息失败。

    Retry 结束当前 Assignment，在同一 Executor Session、目录、分支与 pull request 上启动新 Assignment。

    Executor 模型仅在创建 Session 时应用；已有 Session 保留其模型。

    ### Restart

    运行中的 Assignment 以「host restarted」结束；新的以恢复说明开始，要求 Executor 在重复任何对外动作前检查目录、git 历史与 GitHub，最旧 Ticket 优先。

    处于 waiting、ready、blocked 或 failed 的 Assignment 保持不变，除非该 Report 之后有 Input 到达，则也再次启动。

    Session、目录、Subscriptions、pending 消息及下述 stored work 在重启后存活；每个 Executor Session 哪些消息纳入历史的记录也存活——一步在 claim 之后、模型调用之前完成纳入：被重启截断的 turn 所取消息在随后 Report 或 turn 结束时计为已读；被 cancel 或 error 在写入 history 前截断的 step 所 claim 的消息计为未读。

    Retry 或 restart 向新 Assignment 再次给出被替代 Assignment 已投递但其 Executor Session 未取的消息（可能被 cancel 或截断 step 丢弃）；仍在 Session 中等待的副本（由 restart 保留或 crash 后持久化）在新 Assignment 首条消息前取出，使新投递为唯一副本。

    ### Stored work

    dsh-superagent 在决策之后要做的事——如向主 Session 中继 candidate、follow-up 或 report、launch Assignment、投递 Input——先存储后执行。

    中继或投递失败（例如 Session 不可达）时 2 秒后重试，随后延迟加倍至一分钟；launch 失败则 Ticket 失败，除非为上述 passing failure。

    每类工作仅在一项连续失败三次后于概览系统问题中报告错误；失败的 Input 投递显示在其 Ticket 上。

    发往主 Session 的中继保持顺序，通知亦然；失败项阻塞同类型后续项。

    ### Source 失败

    GitHub Source 分别读取每个 Issue、pull request 与 Thread：读取失败将错误保留在其 Thread，下次 poll 再读，并计入 Source 错误；默认连续三次 poll 失败后 Source 上报；任一次成功 poll 重置计数。

    GitHub 在 agent 失去私有仓库访问时也返回 404，故 404 会重试。

    已通知但读取持续失败的 Issue 或 pull request 在首次失败读取一天后 drop，GitHub 报告删除时立即 drop；此后不再使 poll 失败，其 Thread 保留错误，Source 状态命名它。

    读取位置保持不动，以便后续通知或对该 Thread 的 poll 读到失败读取所遗漏的内容。

    Feishu 事件消费者报告每次失败；已订阅主题的轮询将每次失败保留在其 Thread。

    GitHub 列表或 Feishu 页面中的 malformed 项跳过，Source 状态计数。

    ### Channel 失败

    出站消息作为 stored work 存储并发送，从 5 秒起退避、加倍至 300。

    同一 Ticket 发往同一会话的消息按排队顺序发出，故其 acknowledgement 早于后续 answer 或 result：若同 Ticket 同会话较早消息尚未发出，则后消息等待，直到较早者连续失败三次并显示为问题。

    其他 Ticket 的消息及无 Ticket 的消息不为其等待。

    有失败尝试的消息在发出或撤回前为系统问题。

    消息仅在 Source 不再监听该会话，或 Source 答复该会话永远无法接收时 drop；Source 关闭期间等待。

    Source 对 GitHub 已删除 Issue 或 locked 会话、Feishu 已撤回或不再能找到的消息作此答复。

    对已不存在的 review comment 的回复改投 pull request 会话。

    Dropped 消息保留记录（何时、为何 drop、所属 Ticket），不再入队；dropped 的 acknowledgement 显示为 dropped，非 sent。

    ### 暂停处理（Pause processing）

    停止分派、新 Eval、Eval 超时、Input 投递与回复；已在进行的 launch 等待处理恢复。

    Source 继续记录 Trigger；运行中的 Executor turn 不被中断。

    发往已结束 Ticket 的人消息等待：暂停后首次 pass 若当时 Policy 下 Mandate 仍成立则恢复 Ticket。

    进行中的 Eval 在暂停期间不过期；其 deadline 延后暂停时长。

    ### 无效配置

    其种类拒绝的已存储 Source 配置会禁用该 Source 实例。
