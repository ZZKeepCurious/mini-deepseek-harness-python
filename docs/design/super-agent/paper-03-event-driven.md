# 论文 · 03 事件驱动

<p class="lead">对应论文第 3 节：Trigger、Fast/Slow Path、Thread 订阅直送、Inline/Spill、Chain of Recall、推测执行。</p>

!!! info "延伸材料"
    系统解读见 [设计抽象 §4](design-abstract.md#sec-4-events)。本节保留原文与译文。

## 3. Event-Driven Operation

!!! note "原文（English）"

    For a Super-agent to be proactive, we first need to change what drives it. The main inputs driving the agent are no longer prompts typed by humans one at a time into a Session, but semistructured events from its environment. In a Super-agent, environmental events such as group chat messages, email and calendar reminders, code review comments, and CI results are called Triggers. By listening for and responding to Triggers, the agent no longer has to wait passively for a human instruction before executing once, as it would in the single-Session model.

    Following an environmental event through its lifecycle from arrival to processing, we discuss four successive questions: how to filter large amounts of noise with a two-stage strategy combining code and models (Section 3.1); how to route events to Workers through subscription and direct delivery (Section 3.2); how to use Inline & Spill and Chain of Recall to inject information-dense context and retrieve it on demand (Section 3.3); and how to use speculative execution to act in advance in the background based on environmental signals (Section 3.4).

    ### 3.1 Event Filtering

    Once connected to real group chats and code platforms, the environmental event stream has a pronounced "high noise, low action rate" character: the vast majority of everyday messages do not require the agent to intervene. Calling a large model to assess every event from scratch would make both latency and compute costs difficult to sustain. Drawing on the Fast Path / Slow Path concept from computer systems, large-model inference can be treated as a flexible but expensive Slow Path. Encoding well-understood, stable, frequent decisions in deterministic code provides a low-latency, low-cost Fast Path.

    The first step after events enter the system is therefore filtering. The goal is to remove noise using deterministic policies before invoking a large model, sending only candidate events that require semantic understanding to the model for judgment. We use a two-stage filtering mechanism combining programmatic policies with a large model:

    - Programmatic policies—the Fast Path: Code performs quick admission checks against deterministic conditions such as event source, group scope, and user or bot allowlists. For example, different sources can be configured to "respond only to explicit requests," "consider as candidate events," or "ignore directly," filtering out most irrelevant messages before the model is invoked.
    - Semantic policies—the Slow Path: Candidate events admitted by the programmatic layer go to the LLM, which decides whether to act based on policies written in prompts and on context. The semantic layer also serves as a natural-language extension point for humans, making it easy to enable, disable, or add policies through prompts, as shown below.

    ### 3.2 Event Routing

    The global Router layer introduced in Section 2 maintains continuity across channels. But if every follow-up event for an assigned task still has to be forwarded through it, this global node will remain heavily loaded, increasing system response latency. The Fast Path / Slow Path idea therefore applies to routing filtered events as well. The global Router layer is responsible only for starting new tasks and coordinating across tasks. Once ownership has been established, code sends subsequent events directly to the corresponding Worker Session, without forwarding them through the Router layer.

    In real workflows, successive events under the same GitHub Pull Request, IM topic, or email conversation form an event stream: a Thread.

    After the Router layer creates a Worker Session from a Thread's initial event, that Worker subscribes directly to the Thread. Event routing is then divided into the two paths shown below:

    - Subscribed—the Fast Path: Subsequent events in the same Thread are delivered directly by code to the subscribed Worker. The Router layer does not need to call a tool to forward them again. If a new event falls outside the scope of the current task, the Worker can unsubscribe or return it to the Router layer for reassignment.
    - Unsubscribed—the Slow Path: The Router layer can still observe all events to maintain a global view, but it only needs to assess and assign new events that have not yet been subscribed to.

    The subscription relationship between Threads and Workers is many-to-many. A Worker can subscribe to several related Threads, and a Thread can have multiple subscribed Workers. When Workers cross paths in the same work environment or discussion, new events in that Thread are delivered directly to all its subscribers. Without point-to-point communication, they can learn about one another's actions and have the opportunity to resolve conflicts themselves. On the output side, Workers still report key conclusions to the Router layer, which replies to users and updates global memory.

    ### 3.3 Context Organization

    Once an event has been filtered and routed to its target Session, the next question is how much information to inject into the model's context window. As system inputs shift from a limited set of human prompts to a continuous stream of semistructured environmental events, the structure of tool calls and event injection becomes more important. Injecting too much consumes the window and dilutes the model's attention. Injecting too little forces repeated tool calls to retrieve information, increasing the number of execution steps. Wasting the context window means wasting the model's intelligence.

    We can therefore manage the limited context window as a cache. Borrowing concepts from compiler optimization and computer systems, context injection can be described in terms of two basic operations. Inline means placing information likely to be needed soon directly into the current context window, avoiding additional tool-call round trips. Spill means leaving complete information that exceeds the window budget or is used infrequently outside the window, retaining only references or summaries inside it so the model can read it on demand.

    Combining Inline & Spill with the classic systems principle of locality, we can progressively optimize event injection and tool calls:

    - Start with minimal information: Initially provide only the current object's ID and a summary of its content.
    - Gradually Inline related information using locality: Inline information likely to be needed soon. First attach the IDs of closely related objects, then content that is nearby in time or structure—for example, the first 50 characters of the content, messages before and after an IM message and the message it replies to, or adjacent comments in the review discussion containing a code comment.
    - Spill when the limit is exceeded: Set a small length limit for long content that cannot be fully Inlined, and Spill the remainder outside the window. A straightforward implementation truncates the excess while retaining an entry point for continued reading. The model can call a tool to retrieve the full content when needed.

    In addition, once many follow-up events are delivered directly to Workers and the system has accumulated a long history of tasks, the Router layer cannot keep the full history of every task in its own context window. Yet it still needs to recover context when a user refers to an old task through another channel. Applying Inline & Spill consistently to the system's different object types gives us an on-demand, multihop retrieval mechanism: Chain of Recall. This process helps address the Router layer's loss of earlier information over long periods, as illustrated below.

    Specifically, the system assigns unique IDs to objects such as Triggers, Threads, and Worker Sessions. When injecting an object, it inlines the IDs of closely related objects, forming an underlying reference graph. Each object recalled through a tool call follows the same Inline & Spill approach, so its returned data includes summaries and IDs of related objects for the next hop. Starting from the current event, the model can follow these references to recall context progressively as needed.

    For example, it can read the Thread containing a current message, follow an ID inlined in the Thread to the associated Worker Session, and then find another code review Thread associated with that Worker.

    ### 3.4 Acting in Advance

    Drawing on speculative execution in computer systems, an agent can act in the background based on environmental signals before it is certain that the result will be used. It spends additional background compute to reduce human waiting time and intervention. If the result is valuable, it delivers it; otherwise, it discards it.

    For a personal assistant, receiving a meeting invitation email or a to-do message could trigger background checks for calendar conflicts, collection of relevant background information, and preparation of a reply draft. Once the user reviews it, they can confirm and send it immediately. The same applies to collaborative development. For example, we configured a "silently reproduce and fix" policy in our company's internal DeepSeek Harness user group. When someone reports a problem or discusses a proposed change, the agent first attempts reproduction and looks for a fix in the background. It delivers the result only after confirming the problem and finding a simple fix; otherwise, it remains quiet throughout.

!!! quote "译文（中文）"

    要让 Super-agent 具备主动性，首先要改变驱动方式：主要输入不再是人类逐条敲进 Session 的 prompt，而是来自环境的半结构化事件。群聊消息、邮件与日历提醒、代码评审评论、CI 结果等环境事件称为 **Trigger（触发）**。监听并响应 Trigger 后，agent 不必像单 Session 模型那样被动等待人类指令才执行一次。

    沿事件从到达到处理的生命周期，依次讨论四个问题：用代码与模型结合的两阶段策略过滤大量噪声（3.1）；通过订阅与直送将事件路由到 Worker（3.2）；用 Inline（内联）与 Spill（外溢）及 Chain of Recall（召回链）注入高密度上下文并按需检索（3.3）；用推测执行在不确定结果是否被使用前先在后台行动（3.4）。

    **3.1 事件过滤** — 接入真实群聊与代码平台后，事件流呈「高噪声、低行动率」：绝大多数日常消息不需要 agent 干预。每条事件都从头调用大模型评估，延迟与算力成本都难以持续。借鉴计算机系统的 Fast Path / Slow Path：大模型推理是灵活但昂贵的 **Slow Path**；把已理解、稳定、频繁的判定写成确定性代码，即低延迟低成本的 **Fast Path**。

    事件进入系统后先过滤：在调用大模型前用确定性策略去噪，仅把需要语义理解的候选事件交给模型。两阶段机制为：程序化策略（Fast Path）——按事件来源、群范围、用户/机器人白名单等做快速准入，可配置「仅响应显式请求」「视为候选」「直接忽略」等；语义策略（Slow Path）——准入后的候选由 LLM 结合 prompt 策略与上下文决定是否行动，并作为人类用自然语言扩展策略的扩展点。

    **3.2 事件路由** — 第 2 节的全局 Router 维护跨信道连续；若已分派任务的每条后续事件仍经 Router 转发，全局节点负载过高、响应变慢。路由同样分 Fast/Slow：Router 只负责**启动新任务**与**跨任务协调**；归属建立后，后续事件由**代码直送**对应 Worker Session，不再经 Router 工具转发。

    真实工作流中，同一 GitHub PR、IM 话题或邮件线程下的连续事件构成事件流：**Thread（线程）**。Router 从 Thread 首事件创建 Worker 后，Worker **直接订阅**该 Thread。路由分两条路：**已订阅（Fast Path）**——同 Thread 后续事件直送已订阅 Worker，Router 不必再转发；事件超出当前任务范围时 Worker 可退订或交回 Router。**未订阅（Slow Path）**——Router 仍可观察全量事件以保全局视图，但只需评估尚未被订阅的新事件。

    Thread 与 Worker 为**多对多**订阅；同 Thread 新事件直送所有订阅者，Worker 可据此感知彼此动作并自行化解冲突；输出侧 Worker 仍向 Router 汇报要点，由 Router 回复用户并更新全局记忆。

    **3.3 上下文组织** — 事件过滤并路由到目标 Session 后，要决定向上下文窗口注入多少信息。输入从少量人类 prompt 变为持续环境事件流，工具调用与事件注入结构更重要：注入过多消耗窗口、稀释注意力；过少则反复工具取数、步数增加——浪费窗口即浪费模型能力。

    可把有限上下文当作 **cache**。借鉴编译优化与系统概念：**Inline** 把近期可能用到的信息直接放进窗口，避免额外工具往返；**Spill** 把超预算或不常读的全文留在外部，窗口内只留引用或摘要，按需再读。

    结合**局部性**：先给最小信息（对象 ID + 内容摘要）；再按局部性逐步 Inline 相关对象 ID、时间上/结构上邻近的内容（如 IM 前后消息、评审讨论中相邻评论）；超长内容设小上限，超出部分 Spill，保留继续阅读的入口，模型可工具拉全文。

    Worker 直送与任务历史变长后，Router 无法把全部任务历史塞进自身窗口，却需在用户从另一信道提及旧任务时恢复上下文。对各类对象一致运用 Inline/Spill，可得按需多跳检索 **Chain of Recall**，缓解 Router 长期丢失早期信息。

    系统为 Trigger、Thread、Worker Session 等分配唯一 ID；注入对象时 Inline  closely related 对象的 ID，形成引用图；工具召回的对象同样 Inline/Spill，返回数据含下一跳相关 ID 与摘要。从当前事件出发沿引用逐步扩展上下文（例如：当前消息所在 Thread → Inline 的 Worker Session ID → 该 Worker 关联的另一评审 Thread）。

    **3.4 提前行动** — 借鉴**推测执行**：在不确定结果是否会被使用前，根据环境信号在后台行动，用额外后台算力换更少人类等待；结果有价值则交付，否则丢弃。

    个人助手可在收到会议邀请或待办时后台查日历冲突、收集背景、起草回复；协作开发中可配置「静默复现并修复」——有人报问题或讨论改动时先在后台尝试复现与找简单修复，确认问题且找到简单修复才交付，否则全程安静。
