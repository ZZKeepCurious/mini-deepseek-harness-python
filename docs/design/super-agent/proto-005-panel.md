# Prototype · 005 面板

<p class="lead">对照 `dsh-superagent-prototype/docs/005-panel.md`。</p>

!!! info "延伸材料"
    注意力与面板原则见 [设计抽象 §5](design-abstract.md#sec-5-interaction)。

!!! note "原文（English）"

    # 005 Panel

    Open **Superagent** in the DeepSeek Harness Web sidebar to supervise the agent. The panel prioritizes work that needs a person, then system health and history. This document builds on [000 Philosophy](proto-000-philosophy.md) through [004 Prompts](proto-004-prompts.md).

    ## Three questions

    Opening the panel answers three questions, in this order:

    1. Does anything need me now?
    2. Is the system healthy and doing the right things?
    3. What is the history of one piece of work, from Trigger through Eval to Ticket?

    ## Tabs

    The tabs follow the workflow: **Overview**, **Source**, **Trigger**, **Eval**, **Execution**, and **Settings**. Until repository and account setup succeeds, Overview shows what is missing and links to the relevant setting.

    | Tab | Shows |
    | --- | --- |
    | Overview | Tickets needing action, system problems, activity counts for the last 24 hours, and Executor load. Each block links to its detailed page |
    | Source | Source state, setup problems and non-zero counts. Filtered Triggers and dropped messages cover the same 24-hour window as Overview |
    | Trigger | Threads filtered by Subscribed, Evaluating, New Triggers in 24 h, or All Threads. Each Thread shows its Triggers newest first, Decisions and Subscriptions |
    | Eval | Decisions with their reason and matched rules |
    | Execution | Executor state, Pause processing, and Tickets filtered by Open, Pending, Working, Waiting for a Trigger or Ended. Ticket details show actions, authorization, waiting Inputs, Subscriptions and Assignments |
    | Settings | Sources, Eval, Execution, Prompts and Deployment. Each group includes its save history |

    ### Reading individual records

    A dropped message links to its Ticket, or to its Thread when it has no Ticket. The Source page explains whether its conversation disappeared or the Source stopped listening to it.

    A human follow-up still owned by an Executor is marked **Delivered to Ticket**, without an Eval. The Thread also shows hand-back reasons and context-only Subscriptions. **Evaluate again** can send that follow-up to Eval, or retry a Trigger with no effective judgement: one filtered by its Source, an unavailable Eval, or a delivered message with neither a non-stop Decision nor an Executor acknowledgement.

    Ticket details show the originating Trigger, Decision and reason, scope and Policy version. Assignments and secondary details are folded below the immediate actions. **Retry** is available for failed Input delivery or a Ticket waiting for a person after a blocked or failed Report. Ended Tickets show outcomes such as **Pull request merged** or **Stopped by a person**.

    ## Settings groups

    - **Sources:** one card per Source with its switch, fields and verified account facts. GitHub also offers receive-all switches for new Issues, pull requests, comments and default-branch CI. Polling and read limits are under Advanced.
    - **Eval:** the Policy version and rules, followed by **On each Source** and the Eval settings. Each Source tab contains its allowed list, membership sync, admission table and bot list. Sync runs at startup and on **Sync now**. The admission table distinguishes receiving scope, sender membership and whether the message addresses the agent. Eval settings select the accept mode, timeout and assessment model.
    - **Execution:** worker model, concurrency, directory root, prepare command and blocked commands. Clicking an available variable inserts it into the prepare command.
    - **Prompts:** **Edit** changes team sections, built-in parts and skills. **Full prompt** shows the assembled text and compares it with saved text or defaults. **History** lists versions. Edited built-in parts are marked changed and can be reset; templates list their required placeholders.

      An upgrade that changes an overridden default marks it **Default updated** (默认内容已更新). The panel compares the old and new defaults above the team's text. If the old default was not recorded, it compares the team's text with the new default instead. Choose **Keep my text** or **Use the new default**. Overrides for removed parts are listed below the tab bar and dropped on the next save.
    - **Deployment:** startup problems with **Check again**, the state directory and machine limits. Saved limits apply without restart. The DeepSeek Harness Plugin Manager exposes the same page.

    ## Search

    The header search finds Triggers by text, id, Feishu message id or a stored link, such as a GitHub comment URL. A Feishu link missing from the original message becomes searchable only after the host reads it during the current process lifetime.

    Search also finds Threads by references such as `#12` or a GitHub URL, and Tickets by title or a matching Thread.

    ## Principles

    1. **Focus on what matters.** Every element answers one of the three questions, or it is removed. A number appears only when it changes what the supervisor does.
    2. **Precise definitions.** Categories have checkable definitions, are mutually exclusive and cover every case, as the Ticket stages do. A label says whom a row waits for and what it needs, not an internal status code.
    3. **One place per concept.** Source state belongs in Source; Threads and Triggers in Trigger; Decisions in Eval; Tickets in Execution. Settings are grouped by the concept they configure. Overview summarizes these pages and links to them instead of becoming another detailed view.
    4. **Domain structure.** The panel uses the domain terms. Internal identifiers appear only in folded details, and names replace identifiers where they can be resolved.
    5. **Honest state.** A count equals the length of the list it links to, and no state contradicts another fact.
    6. **Quiet by default.** Healthy states take no extra space. Problems must be actionable; a one-off network error is not enough. Rows needing a person offer their action directly. Keep page explanations to one sentence and put further detail in the (?) tooltip. Destructive actions require confirmation.
    7. **Consistent controls.** Use DeepSeek Harness design tokens and control behavior. The same information should look the same throughout the panel. Each settings block has one save bar, visible only while changes exist, a save runs or a save fails.

!!! quote "译文（中文）"

    在 DeepSeek Harness Web 侧栏打开 **Superagent** 以监督 agent。面板优先展示需要人处理的工作，其次是系统健康与历史。本文建立在 [000 哲学](proto-000-philosophy.md) 至 [004 提示](proto-004-prompts.md) 之上。

    ## 三个问题

    打开面板按此顺序回答三个问题：

    1. 现在有没有需要我处理的事？
    2. 系统是否健康、行为是否正确？
    3. 某一项工作从 Trigger 经 Eval 到 Ticket 的历史是什么？

    ## 标签页

    标签页跟随工作流：**Overview**、**Source**、**Trigger**、**Eval**、**Execution**、**Settings**。在仓库与账号 setup 成功之前，Overview 显示缺失项并链到相应设置。

    | 标签 | 展示内容 |
    | --- | --- |
    | Overview | 待处理的 Ticket、系统问题、过去 24 小时活动计数与 Executor 负载。各块链到其详情页 |
    | Source | Source 状态、setup 问题与非零计数。被过滤的 Trigger 与被丢弃的消息与 Overview 使用同一 24 小时窗口 |
    | Trigger | 按 Subscribed、Evaluating、24 小时内新 Trigger 或 All Threads 过滤的 Thread。每个 Thread 按 Trigger 从新到旧展示 Decision 与 Subscription |
    | Eval | Decision 及其理由与匹配规则 |
    | Execution | Executor 状态、Pause processing，以及按 Open、Pending、Working、Waiting for a Trigger 或 Ended 过滤的 Ticket。Ticket 详情展示操作、授权、等待中的 Input、Subscription 与 Assignment |
    | Settings | Sources、Eval、Execution、Prompts 与 Deployment。每组含其保存历史 |

    ### 阅读单条记录

    被丢弃的消息链到其 Ticket；若无 Ticket 则链到其 Thread。Source 页说明其会话是否消失，或 Source 是否停止监听。

    仍由 Executor 拥有的人 follow-up 标为 **Delivered to Ticket**，不经 Eval。Thread 还展示交还理由与仅上下文的 Subscription。**Evaluate again** 可将该 follow-up 送入 Eval，或重试尚无有效判定的 Trigger：被其 Source 过滤、Eval unavailable，或已投递的消息既无 non-stop Decision 也无 Executor acknowledgement。

    Ticket 详情展示 originating Trigger、Decision 与理由、范围与 Policy 版本。Assignment 与次要详情折叠在即时操作之下。Input 投递失败，或 Report blocked/failed 后 Ticket 待人时，可用 **Retry**。已结束的 Ticket 展示结果，如 **Pull request merged** 或 **Stopped by a person**。

    ## 设置分组

    - **Sources：** 每个 Source 一张卡片，含开关、字段与已验证账号事实。GitHub 还提供接收全部新 Issue、pull request、评论与默认分支 CI 的开关。Polling 与读取限制在 Advanced 下。
    - **Eval：** Policy 版本与规则，随后是 **On each Source** 与 Eval 设置。每个 Source 标签含允许列表、成员同步、准入表与 bot 列表。启动时与 **Sync now** 时运行同步。准入表区分接收范围、发送者成员关系以及消息是否指向 agent。Eval 设置选择 accept 模式、超时与 assessment 模型。
    - **Execution：** worker 模型、并发、目录根、prepare 命令与 blocked 命令。点击可用变量将其插入 prepare 命令。
    - **Prompts：** **Edit** 修改团队节、内置部件与 skills。**Full prompt** 展示组装后的文本，并与已保存文本或默认对比。**History** 列出版本。已编辑的内置部件标为已改且可重置；模板列出所需占位符。

      升级改变某已覆盖的默认时，将其标为 **Default updated**（默认内容已更新）。面板在团队文本上方对比新旧默认。若未记录旧默认，则改为对比团队文本与新默认。选择 **Keep my text** 或 **Use the new default**。已移除部件的覆盖列在标签栏下方，下次保存时丢弃。
    - **Deployment：** 启动问题与 **Check again**、state 目录与机器限制。保存的限制无需重启即生效。DeepSeek Harness Plugin Manager 暴露同一页面。

    ## 搜索

    页头搜索按文本、id、飞书 message id 或存储的链接（如 GitHub 评论 URL）查找 Trigger。原消息中缺失的飞书链接，仅在宿主于当前进程生命周期内读取后才可搜索。

    搜索还可按 `#12` 或 GitHub URL 等引用查找 Thread，按标题或匹配的 Thread 查找 Ticket。

    ## 原则

    1. **聚焦要紧之事。** 每个元素回答三个问题之一，否则移除。数字仅在其改变监督者行为时出现。
    2. **定义精确。** 类别有可检查的、互斥且覆盖全部情况的定义，如 Ticket 阶段。标签说明一行在等谁、需要什么，而不是内部状态码。
    3. **一概念一处。** Source 状态在 Source；Thread 与 Trigger 在 Trigger；Decision 在 Eval；Ticket 在 Execution。设置按其所配置的概念分组。Overview 汇总这些页面并链向它们，而不是成为又一个详情视图。
    4. **领域结构。** 面板使用领域术语。内部标识符仅出现在折叠详情中；能解析处用名称替代标识符。
    5. **状态诚实。** 计数等于其链入列表的长度，且没有状态与另一事实矛盾。
    6. **默认安静。** 健康状态不占额外空间。问题须可行动；一次性网络错误不够。待人的行直接提供其操作。页面说明保持一句，进一步细节放在 (?)  tooltip。破坏性操作须确认。
    7. **控件一致。** 使用 DeepSeek Harness 设计 token 与控件行为。相同信息在面板各处应看起来相同。每个设置块一条保存栏，仅在存在变更、保存进行中或保存失败时可见。
