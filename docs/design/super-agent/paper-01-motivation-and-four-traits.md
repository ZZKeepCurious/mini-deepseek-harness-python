# 论文 · 01 动机与四特征

<p class="lead">对应《Exploring Super-agent Design》第 1 节。与 A 线无关：此处不承诺 mini 实现 Super-agent 产品形态。</p>

!!! info "延伸材料"
    系统解读、流程与架构图见 [设计抽象](design-abstract.md) [§1](design-abstract.md#sec-1-motivation)–[§2](design-abstract.md#sec-2-modules)。本节保留英文原文与中文译文，供逐句核对；候选原则见 [principles.md](principles.md)。

## 1. Super-agent

!!! note "原文（English）"

    Over the past year, single-task agents, particularly coding agents, have demonstrated that models can use tools to solve complex problems with less and less continuous human supervision and step-by-step intervention. At the same time, models are becoming better at coordinating multiple agents, making organization and scheduling across sessions feasible. As human involvement in low-level execution decreases, people's role is gradually moving toward higher-level decisions and acceptance of results.

    This improvement in model capabilities is also giving rise to new forms of applications. Personal agents have recently begun to emerge in large numbers. They connect directly to everyday messaging, calendars, email, and work platforms, continuously observing information streams and helping handle tasks. This always-on, event-driven mode of operation is not limited to personal use: personal assistants are just one application. It is equally valuable in commercial settings such as software development.

    However, today's mainstream agent designs still struggle to support this mode at the scale of multiple tasks and team collaboration. Agents continue to use a single Session as their basic unit of work, with different Sessions remaining independent of one another. When users work on several things in parallel, they must manually switch between Sessions and remember the context of each. In our observation, one person can usually keep track of only three to five active Sessions at a time. Under this design, the ceiling for a single task is set by model capability; the ceiling for multiple tasks is set by human attention.

    Simply extending an existing local, single-session agent through a CLI, MCP, or external connectors may allow it to receive external messages, but it remains constrained by synchronous blocking within a single session, tangled context across tasks, and execution stopping whenever the local device goes offline. Considerable manual maintenance is still required.

    To move beyond these limitations, we need to break the binding between an agent and a single manually initiated session. The agent should become a continuous entity that can retain context across sessions and advance several complex tasks at once without interference. We call this form of agent, and its system architecture, a Super-agent.

    For example, in a Super-agent instance we run internally on DeepSeek Harness, the same development task can continue across instant messaging and GitHub. The instance also continuously monitors feedback in public group chats, proactively identifies root causes, and submits fix Pull Requests even when nobody @mentions it.

    Specifically, we believe a Super-agent has four fundamental characteristics:

    - Continuity: Users can interact with it directly in familiar work interfaces such as IM chats and GitHub comments. It feels as though there is always just one agent, without users having to ask, "Does the context of Session X contain information Y?" For example, if a user first proposes approach A in IM and later switches to approach B in a GitHub Pull Request comment, the agent can understand the change and adjust its work accordingly.
    - Efficiency: It responds quickly while completing multiple complex tasks in parallel and to a high standard. It can accept the next instruction without waiting for the previous command or task to finish.
    - Proactivity: It continuously observes events in its environment and takes action on matters worth addressing, without waiting for users to issue every instruction. When no intervention is needed, it stays quiet.
    - Persistence: It is always online, connects directly to users' real working environments, and automatically recovers from exceptions encountered during long-running operation.

    A single ReAct Loop cannot support all four characteristics at once. An appropriate underlying architecture is needed. Similar design constraints lead to similar architectural choices. We believe that whether an agent is a personal assistant or serves a commercial setting such as software development, building an always-on system that responds concurrently to environmental events may lead to similar underlying architectures. Many of the problems encountered along the way also closely resemble problems that traditional computer systems have studied for decades, making many classic systems design ideas applicable here as well.

    Implementations of this architecture will vary. Nevertheless, we believe the following five modules form the basic framework supporting these four characteristics: a two-layer architecture balances continuous interaction with efficient concurrency (Section 2); event-driven operation enables proactive responses (Section 3); human-centered interaction provides both continuity and efficiency in communication (Section 4); and separation of compute, state, and environments (Section 5), together with a Watchdog (Section 6), enables persistent operation across devices and automatic recovery from exceptions.

!!! quote "译文（中文）"

    过去一年里，单任务智能体——尤其是编码智能体——已经表明：模型可以借助工具解决复杂问题，且越来越少需要人类持续监督与逐步干预。与此同时，模型在多智能体协调方面也在进步，跨 Session 的组织与调度变得可行。随着人类从低层执行中抽身，人的角色逐渐转向更高层的决策与结果验收。

    模型能力的提升也催生了新的应用形态。个人智能体近来大量出现：它们直接接入日常即时通讯、日历、邮件与工作平台，持续观察信息流并协助处理任务。这种常在线、事件驱动的运行方式并不限于个人场景——个人助手只是一种应用，在软件开发等商业场景中同样有价值。

    然而，当今主流的 agent 设计仍难以在**多任务与团队协作**规模上支撑这种模式。Agent 仍以**单个 Session** 为基本工作单元，不同 Session 彼此独立。用户并行处理多件事时，必须手动切换 Session 并记住各自的上下文。据我们观察，一个人通常只能同时跟踪大约三到五个活跃 Session。在这种设计下，单任务的上限由模型能力决定；**多任务的上限由人类注意力决定**。

    仅通过 CLI、MCP 或外部连接器扩展现有的本地单 Session agent，或许能接收外部消息，但仍受限于单 Session 内的同步阻塞、多任务上下文纠缠，以及本地设备离线即停止执行，仍需大量人工维护。

    要突破这些限制，需要打破 agent 与「由人手动发起的单个 Session」之间的绑定。Agent 应成为能在 Session 之间保持上下文、并并行推进多项复杂任务且互不干扰的**连续体**。我们把这种 agent 形态及其系统架构称为 Super-agent（超级智能体）。

    例如，我们在 DeepSeek Harness 上运行的内部 Super-agent 实例中，同一开发任务可以跨越即时通讯与 GitHub 延续；实例还会持续监控公开群聊中的反馈，主动定位根因并提交修复 Pull Request，即使没有人 @ 它。

    具体而言，我们认为 Super-agent 具备四项基本特征：

    - **连续（Continuity）**：用户可在熟悉的界面（如 IM、GitHub 评论）直接与 agent 交互，感觉始终面对**同一个** agent，而不必追问「Session X 的上下文里有没有信息 Y？」。例如用户在 IM 中先提出方案 A，随后在 GitHub PR 评论中改为方案 B，agent 应能理解变化并调整工作。
    - **高效（Efficiency）**：在快速响应的同时，以较高标准**并行**完成多项复杂任务；不必等上一条指令或上一个任务结束才能接受下一条指令。
    - **主动（Proactivity）**：持续观察环境中的事件，对值得处理的事项采取行动，而不必等待用户逐条下达指令；不需要干预时保持安静。
    - **持久（Persistence）**：常在线，直接连接用户真实工作环境，并在长期运行中能从异常中自动恢复。

    单个 ReAct 循环无法同时支撑四项特征，需要合适的底层架构。相似的设计约束会导致相似的架构选择——无论个人助手还是软件开发等商业场景，构建常在线、并发响应环境事件的系统，底层架构可能趋同。沿途许多问题也与传统计算机系统研究的问题高度相似，经典系统设计思想因此适用。

    具体实现会各异，但我们认为以下**五个模块**构成支撑四特征的基本框架：双层架构平衡连续交互与高效并发（第 2 节）；事件驱动实现主动响应（第 3 节）；以人为中心的交互兼顾连续与沟通效率（第 4 节）；计算、状态与执行环境分离（第 5 节）配合 Watchdog（第 6 节），实现跨设备持久运行与异常自恢复。
