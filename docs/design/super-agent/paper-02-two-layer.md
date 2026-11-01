# 论文 · 02 双层架构

<p class="lead">对应论文第 2 节（Router 层与 Worker 层）。</p>

!!! info "延伸材料"
    系统解读见 [设计抽象 §3](design-abstract.md#sec-3-two-layer)。本节保留原文与译文；prototype 对照见主文 [§7](design-abstract.md#sec-7-prototype)。

## 2. A Two-Layer Architecture

!!! note "原文（English）"

    To provide both continuous interaction across channels and efficient concurrency across tasks, the system faces a fundamental trade-off between continuity and throughput in how it organizes its underlying sessions.

    Sending every event to the same Session provides the greatest continuity of context. But it is limited by the speed of a single ReAct Loop executing serially, resulting in long task completion times and low throughput. Mixing intermediate details from multiple tasks can also hurt the quality of the results.

    Conversely, opening a new independent Session for every event maximizes concurrent throughput. But when tasks are not completely independent, the lack of global context leaves their results disconnected. Having multiple Sessions reply independently to the user also creates confusion.

    To balance continuity and throughput, we can separate "maintaining global continuity" from "high-concurrency execution" into two different layers: a persistent Router layer handles human interaction and global coordination (Section 2.1), while a Worker layer executes specific tasks concurrently in isolated contexts (Section 2.2), as shown below.

    With this two-layer architecture, the underlying system takes over context management and concurrent execution. Sessions become less visible to users, who always interact with the same Super-agent. From this perspective, making users manage Sessions manually is largely an inherited design habit from the underlying LLM APIs. Sessions should be an implementation detail of the system, not a cognitive burden for users.

    In existing designs, a Session serves both technical context management and user-facing project management. Once the product experience is decoupled from the underlying Session, user-facing project management is no longer constrained by it. Deciding what interaction and task management experience to expose becomes a separate product question. We discuss this later in the article.

    The "two layers" here describe a logical division of responsibilities. Neither layer has to be implemented as a single Session; each can have its own internal structure. We describe the Router and Worker layers below.

!!! quote "译文（中文）"

    要在跨信道保持连续交互的同时实现跨任务的高效并发，系统在组织底层 Session 时面临**连续性与吞吐量**之间的根本权衡。

    把所有事件都送入同一个 Session，上下文连续性最好，但受限于单个 ReAct 循环串行执行的速度，任务完成时间长、吞吐量低；多任务中间细节混在一起也会损害结果质量。

    反过来，为每个事件打开新的独立 Session 可以最大化并发吞吐，但在任务并非完全独立时，缺少全局上下文会使结果脱节；多个 Session 各自独立回复用户也会造成混乱。

    为平衡连续与吞吐，可以把「维护全局连续」与「高并发执行」拆到两层：持久的 **Router（路由）层** 负责人机交互与全局协调（2.1 节）；**Worker（工人）层** 在隔离上下文中并发执行具体任务（2.2 节）。

    采用双层架构后，底层系统接管上下文管理与并发执行；Session 对用户不再显眼，用户始终在与同一个 Super-agent 交互。让用户手动管理 Session，很大程度上是底层 LLM API 遗留的习惯——Session 应是系统实现细节，而非用户的认知负担。

    现有设计中，Session 同时承担技术上下文管理与面向用户的项目管理；一旦产品体验与底层 Session 解耦，面向用户的项目管理不再被 Session 束缚，暴露怎样的交互与任务管理体验成为独立的产品问题（后文再论）。

    此处的「两层」是**职责的逻辑划分**；每一层不必实现为单个 Session，内部可以有自己的结构。下面分别说明 Router 与 Worker。

### 2.1 The Router Layer

!!! note "原文（English）"

    The Router layer is a persistent global routing layer that handles human interaction from every entry point. Information and decisions spanning tasks are brought together here, making it the global sequence point, coordination point, and memory. To keep this global node from becoming a performance bottleneck, the Router layer is designed not to execute specific tasks itself. It only handles human interaction and task assignment; it may not even have a Bash tool.

    This structure, in which a central node assigns work to multiple execution nodes, may look like a typical Agent Team, but the emphasis is different. A typical Agent Team is primarily used to break down steps and coordinate roles within a single complex task. Because those substeps are usually tightly coupled, the Team Lead often needs to understand the task in depth, plan subtasks, and coordinate team members frequently, taking on a substantial reasoning load. In a Super-agent, the Router layer handles only high-level relationships and assignments between tasks; it does not need to break down individual tasks internally. Conversely, if an Agent Team's main session is constrained through prompting to avoid executing specific work itself, and instead only handles external interaction and assigns different tasks to members to advance independently, it can also be regarded as this kind of two-layer architecture. In our experience, the Router layer's work is often less complex than it might appear. A lighter model can often provide both speed and good results.

    Internally, the Router layer does not have to be a single Session either, and some of its logic may not require an LLM at all. We chose the most straightforward implementation: a single persistent LLM Session at its core. We found that it was less prone to becoming a throughput bottleneck than it might seem. Environmental events arriving during one round of inference—the Triggers introduced in Section 3—accumulate and are processed together in the next round. This creates batching, with little additional overhead from processing events together:

    - When the external event stream is steady, Triggers enter the Router Session one at a time, and individual response latency is low.
    - When events arrive in bursts and queues start building up in the Router Session, multiple Triggers accumulated during the previous round of inference are automatically combined and injected together in the next LLM call. The Router Session performs only lightweight interaction and assignment, with each step typically involving a long input and a short output. Combining queued Triggers into one LLM API call allows them to be computed in parallel during prefill. The duration of an individual request therefore grows much more slowly than the batch size. Within a certain range, the larger the backlog, the less inference time is amortized over each decision. The upper limit depends on context length and how many Triggers the model can accurately handle in one round.

    Section 7.2 quantifies the effects of this batching mechanism through experiments. Combined with the subscription and direct-delivery mechanisms and Chain of Recall introduced later (Sections 3.2 and 3.3), a single persistent Router Session has been sufficient at our current team scale while preserving continuity of context.

    If the scale grows further and a single session genuinely becomes insufficient, the Router layer could use a multilayer filtering structure or be horizontally partitioned—sharded—across multiple sessions. At that point, its internal design is fundamentally a trade-off between continuity and throughput. Finer-grained partitioning increases concurrent throughput in the Router layer but reduces continuity of memory and context across shards. Until the global memory problem is solved, this trade-off will remain.

!!! quote "译文（中文）"

    Router 层是持久的全局路由层，承接各入口的人机交互；跨任务的信息与决策在此汇聚，成为全局序点、协调点与记忆。为避免该全局节点成为性能瓶颈，Router 层**被设计为不亲自执行具体任务**，只负责人机交互与任务分派，甚至可以没有 Bash 工具。

    中心节点向多个执行节点分派工作的结构，看起来像典型的 Agent Team，但侧重点不同：Agent Team 主要用于**单个复杂任务内部**拆步与角色协调，子步骤通常紧耦合，Team Lead 需深入理解任务、规划子任务并频繁协调成员，推理负载大。Super-agent 的 Router 只处理**任务之间**的高层关系与分派，不必在内部拆解单个任务。反之，若 Agent Team 的主 Session 经 prompt 约束不执行具体工作、只负责对外交互并把不同任务分给成员独立推进，也可视为这种双层架构。实践中 Router 的工作往往不如表面看起来复杂，较轻的模型常能兼顾速度与效果。

    Router 内部也不必是单个 Session，部分逻辑甚至可以不用 LLM。团队当前采用最直白的实现：核心是一个持久的 LLM Session。发现其吞吐瓶颈往往不如预想严重——推理轮次中到达的环境事件（第 3 节的 Trigger）会累积，在下一轮一并处理，形成**批处理**，合并处理的额外开销很小：

    - 事件流平稳时，Trigger 逐个进入 Router Session，单次响应延迟低。
    - 突发到达、Router 内排队时，上一轮推理期间累积的多个 Trigger 会在下一次 LLM 调用中自动合并注入。Router 每步通常是长输入、短输出的轻量交互与分派；合并排队 Trigger 到一次 API 调用可在 prefill 阶段并行计算，单次请求时长随批大小增长远慢于线性。在一定范围内，积压越大，摊到每次决策上的推理时间越少；上限取决于上下文长度与模型一轮能准确处理的 Trigger 数量。

    7.2 节用实验量化批处理效果。结合后文的订阅直送与 Chain of Recall（3.2、3.3 节），在当前团队规模下，单个持久 Router Session 已足够且保持上下文连续。

    规模继续扩大、单 Session 确不足时，Router 可采用多层过滤或水平分片（shard）；此时内部设计仍是连续性与吞吐的权衡——分片越细，Router 并发越高，但跨分片记忆与上下文连续越弱。全局记忆问题未解决前，这一权衡将长期存在。

### 2.2 The Worker Layer

!!! note "原文（English）"

    The Worker layer consists of sessions responsible for execution. Each Worker is isolated from the others in terms of context and has a full set of execution tools, allowing it to focus on one task within its own context. Since Worker Sessions directly determine task outcomes and code quality, they usually need the most capable model available.

    Workers do not interact directly with humans. Their reasoning and tool calls are not sent directly to external channels. Instead, they report their status to the Router layer, which replies to users on their behalf. This preserves concurrency and delivery quality while avoiding the confusion of multiple sessions replying independently.

    When multiple tasks run concurrently, each Worker advances its task independently. Workers at the same layer generally do not need to communicate directly. Conflicts during execution often arise in shared work environments, such as the same Pull Request. As long as the Workers subscribe to the corresponding Thread, they receive the same event notifications (Section 3.2), giving them the opportunity to identify and resolve conflicts themselves. The Router layer relays the limited coordination needed across tasks.

    Like the Router layer, each Worker Session need not be limited internally to a single session. When executing a complex individual task, a Worker can create Subagents or itself be an Agent Team. These details remain transparent to the Router layer above it. This structure applies both to collaborative coding—where independent issue reproductions, Pull Request reviews, and feature development can proceed without blocking one another—and to personal assistants. Although a personal assistant's interface usually presents a single-session chat page, a user may discuss several topics in the same conversation. The single chat page maintains continuity of interaction, while the system underneath can assign a separate Worker to each topic to advance tasks and draft replies in parallel.

!!! quote "译文（中文）"

    Worker 层由负责执行的 Session 组成。各 Worker 上下文隔离，并配备完整执行工具集，可在自有上下文中专注一项任务。Worker 直接决定任务结果与代码质量，通常需要最强模型。

    Worker **不直接与人交互**；推理与工具调用不直发外部信道，而是向 Router 汇报状态，由 Router 代其回复用户。这样既保留并发与交付质量，又避免多 Session 各自对外回复造成混乱。

    多任务并发时，各 Worker 独立推进；同层 Worker 一般无需直接通信。冲突常出现在共享工作环境（如同一 PR）。只要 Worker 订阅对应 Thread（3.2 节），就会收到相同事件通知，有机会自行发现并化解冲突；跨任务所需的有限协调由 Router 转发。

    Worker 内部也不必限于单 Session；复杂单任务内可创建 Subagent 或自身构成 Agent Team，对上层 Router 透明。协作开发中，独立的 issue 复现、PR 审查与功能开发可互不阻塞；个人助手中，界面常呈现单聊页面，用户可在同一对话谈多个话题——页面保持交互连续，底层可为每个话题分配独立 Worker 并行推进与起草回复。
