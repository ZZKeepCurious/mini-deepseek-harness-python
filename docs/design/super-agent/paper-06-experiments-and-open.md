# 论文 · 06 实验、开放问题与结论

<p class="lead">对应论文第 7–9 节。实验、§8 开放方向与 SA-S-01 摘要见 [设计抽象 §9](design-abstract.md#sec-9-principles)；检验口径见 [principles.md](principles.md)。</p>

!!! info "延伸材料"
    本节保留原文与译文；开放问题默认 **不采纳** 为 mini 义务。

## 7–9. Experiments, Open Questions, Conclusion

!!! note "原文（English）"

    ## 7. Preliminary Experiments

    To evaluate the core scheduling and routing designs above, we conducted preliminary experiments at two levels. First, we used end-to-end experiments to compare the two-layer architecture and subscription-based direct delivery against single-Session approaches (Section 7.1). We then tested batching in the Router layer separately (Section 7.2). Because the experiments use synthetic workloads and simplified environments, and some aspects of their setup could be made more rigorous, the results below should be treated only as preliminary reference points.

    ### 7.1 Ablation Study

    We drove four end-to-end approaches with streams of group chat and GitHub CI messages. Each experiment contained roughly 300 messages, about 20 of which were work requests; the rest were casual conversation, follow-ups, stop requests, and CI messages. Coding work was replaced with a tool that only consumed time, so the experiment measured system scheduling and coordination rather than the ability to execute individual tasks. Each approach was run five times.

    The four approaches were Super-agent, Super-agent without direct delivery, and a single Session with a Subagent tool under two different prompt configurations.

    For task completion time, Super-agent achieved a p50 of 12.4 minutes and a p95 of 15.7 minutes, substantially better than the single Session with a Subagent tool. Under the neutral prompt, the model did not invoke a Subagent in any of the five runs. All work took place in the main Session and was repeatedly interrupted by new messages, with p95 reaching 29.5 minutes. Compared with relying on prompt engineering and the model's moment-to-moment judgment about delegation within a single Session, the two-layer architecture alleviates this problem at the system level: the Router does not execute specific work, and Workers receive messages only from the Threads they subscribe to.

    Removing direct delivery from Super-agent left task completion times essentially unchanged, but increased the number of decisions the Router had to make by approximately 30%. Total cache-read tokens per experiment rose from 23.23 million to 31.77 million, an increase of approximately 37%.

    ### 7.2 Batching in the Router Layer

    When the arrival rate of Triggers requiring a decision increased from approximately one to approximately 40 per minute, the average batch size rose from 1.0 to 2.8. Individual request duration increased only from 2.5 to 3.9 seconds. Inference time amortized over each decision fell from 6.4 to 1.6 seconds.

    ## 8. Open Questions

    (Sections 8.1–8.4: scaling two-layer architecture; collaboration beyond Sessions; context boundaries and A2A; security and privacy — see full English in offloop mirror and 译文 below.)

    ## 9. Conclusion

    Looking back at the design as a whole, we have not proposed a new theory. Instead, we have applied classic engineering ideas from computer systems to building agent systems. Some of the designs in this article have been implemented in dsh-superagent-prototype. This plugin is only a prototype provided to validate the technical approach; it is not an official DeepSeek product.

!!! quote "译文（中文）"

    **第 7 节 初步实验** — 为评估调度与路由设计，在两层做初步实验：端到端对比双层架构+订阅直送与单 Session 方案（7.1）；单独压测 Router 批处理（7.2）。实验为合成负载与简化环境，结果仅作**初步参考**。

    **7.1 消融** — 用群聊与 GitHub CI 消息流驱动四种方案；每实验约 300 条消息、约 20 条工单请求，编码工作替换为耗时工具，测调度协调而非单任务执行质量；各跑 5 次。四种方案：Super-agent、去掉直送的 Super-agent、单 Session + Subagent 工具（中性 prompt / 委派优先 prompt）。

    任务完成时间：Super-agent **p50 12.4 分钟、p95 15.7 分钟**，明显优于单 Session + Subagent。中性 prompt 下 5 次运行均未调用 Subagent，工作全在主 Session 并被新消息反复打断，**p95 29.5 分钟**。相较单 Session 内靠 prompt 与模型即时判断是否委派，**双层在系统层缓解该问题**：Router 不执行具体工作，Worker 只收订阅 Thread 的消息。

    去掉直送：完成时间几乎不变，Router 决策量约 **+30%**，cache-read token 约 **23.23M → 31.77M（+37%）**——直送不提速但显著省 Router 负载。

    **7.2 Router 批处理** — 需决策的 Trigger 到达率约 1→40/分钟时，平均批大小 1.0→2.8，单次请求时长 2.5→3.9 秒，摊销决策推理时间 6.4→1.6 秒。

    **第 8 节 开放问题** — **8.1** 双层如何扩展：「单 Router Session + 订阅直送 + Worker 不直连」覆盖当前日常任务，但规模与耦合度上升可能超出单 Router；何时分片、Worker 何时直连、何种协作拓扑需更多验证；Router 层事件评估/分派/召回的系统-模型协同设计值得探索；Chain of Recall 主要服务近期任务，**月级长期记忆**成扩展瓶颈。**8.2** Session 对用户隐藏后，任务如何呈现、项目管理 UX 如何自然；聊天窗口中心交互有路径依赖；Issue/PR 等领域界面可承载更多交互；进行中的人机协同（介入、接管、与后台并行协作）仍待探索；体验高度依赖领域与个人风格，DSH 开放架构利于定制。**8.3** 单实例内全局上下文假设在组织扩大时变为**上下文共享/隔离边界**部署决策；多实例间协作为 **A2A**，在无共享上下文与不同权限边界下如何安全可靠仍开放。**8.4** 事件驱动扩大不可信输入面；云 Loop + 本地 daemon 假设互信，prompt 注入或入侵可波及连接设备；须避免凭据进入模型上下文的机制，否则难以广泛采用。

    **第 9 节 结论** — 整体设计并非新理论，而是把**经典系统工程思想**用于 agent 系统；实现随技术演进，系统设计思想或更长久。基于 DSH 的内部实例已持续运行。瓶颈转向人类注意力；双层、事件驱动、跨信道异步、存算分离、Watchdog 外环让系统接管繁琐 Session 管理与上下文同步，人更远离执行细节、面向决策与验收。Agent 从被动聊天窗口演变为数字环境中常在线的协作体。文中部分设计已在 **dsh-superagent-prototype** 验证；该插件仅为验证技术路线，**非官方 DeepSeek 产品**。
