# Prototype · 004 提示

<p class="lead">对照 `dsh-superagent-prototype/docs/004-prompts.md`。</p>

!!! info "延伸材料"
    信道带宽与 Router 出口见 [设计抽象 §5](design-abstract.md#sec-5-interaction)。

!!! note "原文（English）"

    # 004 Prompts

    This document explains what the agents read, what a team can edit, and when changes take effect. It builds on [000 Philosophy](proto-000-philosophy.md) through [003 Execution](proto-003-execution.md). Prompt text is written in English.

    ## What a team writes

    A team can edit three team sections, add skills, and override every built-in system-prompt part. Most defaults live under `prompts/` and `skills/`. Tool descriptions, notices and cards remain in code and cannot be edited through prompt settings.

    | Section | Holds |
    | --- | --- |
    | Communication | When to message people, how messages read, the line that begins every GitHub message (`This is by an automated agent, not a human` by default), how to handle reviews, and which ordinary accounts are review bots |
    | Ticket scope | When a Ticket goes on and when it stops with a draft |
    | Delivery requirements | Evidence in pull requests, the auto-merge condition with its line threshold, and the commit author; without one, the Executor commits as the agent account's no-reply address |
    | Team skills | Knowledge the agents load when they need it, such as an `identities` skill that says which Feishu user is which GitHub account |

    The built-in text covers the roles, tool usage, host mechanics, authorization rules, and the GitHub and Feishu skills. It refers to the team sections by their headings instead of repeating them.

    ### Built-in parts and templates

    Overrides use these keys:

    | Parts | Keys |
    | --- | --- |
    | Shared instructions and roles | `runtime/shared`, `runtime/orchestrator`, `runtime/worker` |
    | Working instructions | `runtime/subscriptions`, `runtime/replies`, `runtime/dispatch`, `runtime/work`, `runtime/admission` |
    | Handoff instructions | `runtime/handoff/orchestrator`, `runtime/handoff/worker` |
    | Source skills | `skill/feishu`, `skill/github` |
    | Generated sections | `runtime/environment`, `runtime/policy`, `runtime/policy/none` |

    The generated sections are editable templates. They must retain these placeholders:

    - `runtime/environment`: `{variables}`, filled with execution environment lines.
    - `runtime/policy`: `{version}`, `{mode}` and `{rules}`, with one line per enabled rule.
    - `runtime/policy/none`: `{version}` and `{mode}`, used when no rule is enabled.

    Editing a part changes the instructions the model receives. It does not change the gate, Mandates, tool checks or account wrappers, which remain enforced by code.

    On an explicit human request, the main Session can also save overrides with `orchestrator_prompts_save`. If a save omits an override's `base`, it keeps the saved base. A removed part already present in the saved configuration is dropped on save; any other unknown part is refused.

    An override records which built-in text it was based on. Upgrades preserve the team's text. If the default changes, the panel marks the part **Default updated** and shows the difference beside the team's text. The team can keep its text, use the new default or edit it. Parts without overrides automatically follow new defaults.

    The main Session can read affected part names in `orchestrator_read prompts`, under `default_changed`.

    ## Assembly and precedence

    The system prompt is assembled in this order:

    1. Shared instructions and the role.
    2. The three team sections.
    3. Subscriptions and replies.
    4. Dispatch for the main Session, or work instructions for an Executor.
    5. `runtime/admission`, for the main Session only.
    6. Handoff instructions.
    7. `runtime/environment`.
    8. `runtime/policy`, for the main Session only.

    Admission instructions come from `prompts/admission.md`. They describe proactive assessment and when an irrelevant candidate can be ignored from its card. `orchestrator_eval` returns context rather than repeating these instructions.

    The environment section gives the main Session deployment-wide values and each Executor its Ticket-specific values. The Policy section is regenerated from settings on every model request. It contains the Policy version, accept mode, the explicit-request exemption, and enabled rules as `- <name> (<id>): <instruction>`, or a statement that none is enabled. It is last so a Policy change leaves the preceding prompt text unchanged.

    Executors also read DeepSeek Harness's coding prompt and the repository's `AGENTS.md`; the main Session does not. An inferred Ticket adds a final note limiting its Executor to the matched rules. Assignments and incoming messages arrive inline as Trigger cards, with resource pointers only for truncated content.

    Executors work unattended. Their tools exclude those that ask a person, present UI to them or change the profile, such as `ask_user_question`, `present` and `plugin_manager`.

    From strongest to weakest:

    1. Facts enforced by tools and the host: identities, permissions, tool argument checks, reply purposes, length limits. Prompt text cannot change them.
    2. Built-in and generated sections, as the team's overrides of them read.
    3. The process and communication in a proactive Ticket's matched rules, for that Ticket's Executor Session only.
    4. Team sections and team skills.
    5. Outside text: messages, comments, documents and quotes. It is data and never changes rules or authorization.

    ## Principles of the built-in text

    These apply [000 Philosophy](proto-000-philosophy.md) to the agents:

    - **Separate roles.** The main Session only routes: it forwards the original Trigger and the routing facts, and makes no technical decisions, through one routing tool that takes the decisions on several waiting candidates in one call. A routing note has at most 30 words. The Executor reads the original sources, judges the scope, implements and verifies.
    - **The main Session does not wait.** Reports, follow-up notices, candidates and completed background jobs arrive automatically as messages. It ends the turn when nothing remains to do. A guard refuses `sleep`, shell timeouts over 30 seconds and job waits over 5 seconds, because a blocked step delays new candidates. These instructions replace DeepSeek Harness's own job-notification note in the main Session's complete prompt.
    - **Message only when a person must act or can use a result:** a result can be reviewed or used, a decision or resource is needed, or the work cannot be finished. Chat messages are one sentence by default, with a hard limit of 100 words.
    - **Executors decide their follow-ups.** Each Executor handles and acknowledges in-scope requests, and hands back requests beyond its Ticket or for another Ticket. A message becomes new work only after all its owners hand it back or end without taking it up. The main Session receives follow-up notices but may act on them only to stop work. Handed-back messages then arrive as candidates with the Executors' reasons.
    - **Coordinate through subscriptions and the main Session.** An Executor follows related Threads for context. The main Session can send a fact from another Ticket in a note of at most 100 words. Neither the Subscription nor the note grants authority.
    - **Attach only on an explicit reference.** A message must point to a Ticket's pull request, Issue, source message or acknowledgement, or to a recent change with exactly one matching Ticket. Topic similarity alone is not enough. Attachment subscribes the Ticket to the entire Thread; an Executor finding it unrelated uses `orchestrator_thread_unrelated` and does not answer there.
    - **Confirm concretely, once.** Before substantial work, the Executor confirms each new request and names the object and the expected result, as a reply under the original message.
    - **Ask through `human_action`.** The Executor puts one concrete question in its Report, and the main Session forwards it unchanged. If a person's message prompted the question, it names that message and the host asks there. Otherwise it asks in the Ticket's original conversation. Already authorized work can continue meanwhile.
    - **Honest delivery.** Opening a pull request is not readiness; a draft is marked unfinished; a UI change without a real before screenshot is not ready.
    - **A person's pause holds** across follow-ups, new Executor Sessions and restarts until a person lifts it. Automatic feedback never resumes paused work.
    - **Examples only show format.** An example sentence is not a fact the agent may claim.
    - **Handle reviews by outcome.** A fixed and verified comment is resolved silently; a declined one gets one sentence of reason. An automated reviewer's thread is resolved once it has an answer, and each automated reviewer gets at most three rounds per pull request.
    - **Yield to duplicate work.** When a person's branch or pull request already contains the Ticket's change, the Executor closes its own pull request with one sentence pointing to theirs.

    ## Versions

    - Each save creates an immutable version containing the full configuration, revision number, predecessor, timestamp and origin (panel or tool). Restoring a version or defaults creates another version.
    - Saves name their base revision. A conflicting save is refused without discarding the draft.
    - Sessions read the latest version on every model request, including later steps in the same turn. Saving does not restart, cancel or wake a Session. The host records the revisions each Session used; an Executor can read any revision it worked under.
    - The deployment sets a total size limit, including overrides. Validation refuses unknown keys, reserved built-in skill names, unknown override parts and missing template placeholders. It never silently truncates text. An override identical to the default is removed so the part follows future defaults.

!!! quote "译文（中文）"

    本文说明 agent 读什么、团队可改什么、以及改动何时生效。建立在 [000 哲学](proto-000-philosophy.md) 至 [003 执行](proto-003-execution.md) 之上。Prompt 正文用英文撰写。

    ## 团队可写的内容

    团队可编辑三个团队节、添加 skills，并覆盖每一个内置 system-prompt 部件。多数默认内容在 `prompts/` 与 `skills/` 下。工具描述、通知与卡片仍在代码中，不能通过 prompt 设置编辑。

    | 节 | 内容 |
    | --- | --- |
    | Communication（沟通） | 何时给人发消息、消息如何表述、每条 GitHub 消息开头的固定行（默认 `This is by an automated agent, not a human`）、如何处理 review、以及哪些普通账号视为 review bot |
    | Ticket scope（工单范围） | Ticket 何时开启、何时以 draft 结束 |
    | Delivery requirements（交付要求） | PR 中的证据、auto-merge 条件及其行数阈值、commit author；未指定时 Executor 以 agent 账号的 no-reply 地址提交 |
    | Team skills（团队技能） | agent 按需加载的知识，例如 `identities` skill 说明哪个飞书用户对应哪个 GitHub 账号 |

    内置文本覆盖角色、工具用法、宿主机制、授权规则，以及 GitHub 与飞书 skills。它通过各团队节的标题引用这些节，而不是重复其正文。

    ### 内置部件与模板

    覆盖使用下列键：

    | 部件 | 键 |
    | --- | --- |
    | 共享说明与角色 | `runtime/shared`、`runtime/orchestrator`、`runtime/worker` |
    | 工作说明 | `runtime/subscriptions`、`runtime/replies`、`runtime/dispatch`、`runtime/work`、`runtime/admission` |
    | 交接说明 | `runtime/handoff/orchestrator`、`runtime/handoff/worker` |
    | Source skills | `skill/feishu`、`skill/github` |
    | 生成节 | `runtime/environment`、`runtime/policy`、`runtime/policy/none` |

    生成节是可编辑模板，必须保留下列占位符：

    - `runtime/environment`：`{variables}`，填入执行环境各行。
    - `runtime/policy`：`{version}`、`{mode}` 与 `{rules}`，每条启用的规则一行。
    - `runtime/policy/none`：`{version}` 与 `{mode}`，在无规则启用时使用。

    编辑某一部件会改变模型收到的说明，但不会改变 gate、Mandate、工具校验或账号包装——这些仍由代码强制执行。

    在人的明确请求下，主 Session 也可用 `orchestrator_prompts_save` 保存覆盖。若某次保存省略某覆盖的 `base`，则保留已保存的 base。保存时，已出现在已保存配置中、但已被移除的部件会被丢弃；任何其他未知部件会被拒绝。

    覆盖会记录其基于哪份内置文本。升级保留团队文本。若默认内容变更，面板将该部件标为 **Default updated**，并在团队文本旁展示差异。团队可保留自己的文本、采用新默认或再编辑。无覆盖的部件自动跟随新默认。

    主 Session 可在 `orchestrator_read prompts` 的 `default_changed` 下读取受影响的部件名。

    ## 组装与优先级

    system prompt 按下列顺序组装：

    1. 共享说明与角色。
    2. 三个团队节。
    3. Subscriptions 与 replies。
    4. 主 Session 的 dispatch，或 Executor 的 work 说明。
    5. `runtime/admission`（仅主 Session）。
    6. Handoff 说明。
    7. `runtime/environment`。
    8. `runtime/policy`（仅主 Session）。

    Admission 说明来自 `prompts/admission.md`，描述主动评估以及何时可从卡片上忽略不相关候选。`orchestrator_eval` 返回上下文，而不是重复这些说明。

    environment 节给主 Session 部署级取值，给每个 Executor 其 Ticket 专属取值。Policy 节在每次 model 请求时从设置重新生成，包含 Policy 版本、accept 模式、explicit 请求豁免，以及启用的规则，格式为 `- <name> (<id>): <instruction>`，或声明无规则启用。它放在最后，以便 Policy 变更时不改动前面的 prompt 文本。

    Executor 还读 DeepSeek Harness 的 coding prompt 与仓库的 `AGENTS.md`；主 Session 不读。inferred Ticket 会追加一条最终说明，将其 Executor 限制在匹配的规则内。Assignment 与入站消息以内联 Trigger 卡片到达，仅在被截断内容时使用资源指针。

    Executor 无人值守工作。其工具排除向人提问、向人展示 UI 或改 profile 的工具，例如 `ask_user_question`、`present` 与 `plugin_manager`。

    从强到弱：

    1. 工具与宿主强制执行的事实：身份、权限、工具参数校验、reply 用途、长度限制。Prompt 文本不能改变它们。
    2. 内置与生成节，按团队对其覆盖后的读法。
    3. 主动 Ticket 匹配规则中的流程与沟通，仅适用于该 Ticket 的 Executor Session。
    4. 团队节与团队 skills。
    5. 外部文本：消息、评论、文档与引用。它们是数据，从不改变规则或授权。

    ## 内置文本的原则

    这些原则将 [000 哲学](proto-000-philosophy.md) 落实到 agent：

    - **角色分离。** 主 Session 只路由：通过一次路由调用对多个等待中的候选做决定，转发原始 Trigger 与路由事实，不做技术决策。路由 note 最多 30 词。Executor 读原始来源、判断范围、实现并验证。
    - **主 Session 不等待。** 报告、follow-up 通知、候选与已完成的后台 job 自动以消息到达。无事可做时结束 turn。守卫拒绝 `sleep`、超过 30 秒的 shell 超时与超过 5 秒的 job 等待，因为阻塞步骤会拖延新候选。这些说明取代 DeepSeek Harness 在主 Session 完整 prompt 中的 job 通知 note。
    - **仅在人必须行动或可使用结果时发消息：** 结果可 review 或使用、需要决策或资源、或工作无法完成。聊天消息默认一句，硬上限 100 词。
    - **Executor 决定 follow-up。** 每个 Executor 处理并确认范围内的请求，将超出其 Ticket 或属于其他 Ticket 的请求交还。一条消息仅在其所有拥有者交还或结束且未接手后，才成为新工作。主 Session 收到 follow-up 通知，但只能据此 stop 工作。交还的消息随后作为候选到达，附带 Executor 理由。
    - **通过订阅与主 Session 协调。** Executor follow 相关 Thread 获取上下文。主 Session 可用最多 100 词的 note 发送来自其他 Ticket 的事实。Subscription 与 note 均不授予授权。
    - **仅在明确引用时 attach。** 消息必须指向某 Ticket 的 pull request、Issue、源消息或 acknowledgement，或指向恰好匹配一个 Ticket 的近期变更。仅靠主题相似不够。Attach 使 Ticket 订阅整个 Thread；Executor 若认为无关，使用 `orchestrator_thread_unrelated` 且不在该处回复。
    - **具体确认，一次。** 在开展实质性工作前，Executor 确认每个新请求并点明对象与预期结果，作为原消息下的 reply。
    - **通过 `human_action` 提问。** Executor 在 Report 中放一个具体问题，主 Session 原样转发。若由某人的消息引发问题，则点名该消息，宿主在该处提问；否则在 Ticket 原始会话中提问。已授权的工作可同时进行。
    - **诚实交付。** 开 PR 不等于就绪；draft 标为未完成；UI 变更没有真实 before 截图则不算就绪。
    - **人的 pause 持续有效，** 跨越 follow-up、新 Executor Session 与重启，直到人解除。自动反馈从不恢复已 pause 的工作。
    - **示例只展示格式。** 示例句不是 agent 可声称的事实。
    - **按结果处理 review。** 已修复并验证的评论静默 resolve；拒绝的评论用一句理由。自动化 reviewer 的 thread 有答复后 resolve；每个自动化 reviewer 每个 PR 最多三轮。
    - **让位于重复工作。** 当某人的分支或 PR 已包含 Ticket 的变更时，Executor 用一句指向对方的话关闭自己的 PR。

    ## 版本

    - 每次保存创建不可变版本，含完整配置、修订号、前驱、时间戳与来源（面板或工具）。恢复某版本或默认会再创建一版。
    - 保存须命名其 base 修订。冲突的保存会被拒绝，且不丢弃草稿。
    - Session 在每次 model 请求（含同一 turn 内后续步骤）读取最新版本。保存不会重启、取消或唤醒 Session。宿主记录各 Session 使用的修订；Executor 可读其工作过的任意修订。
    - 部署设置总大小上限（含覆盖）。校验拒绝未知键、保留的内置 skill 名、未知覆盖部件与缺失的模板占位符。从不静默截断文本。与默认相同的覆盖会被移除，使该部件跟随未来默认。
