# 论文 · 05 持久化与 Watchdog

<p class="lead">对应论文第 5–6 节：算力/状态/执行环境分离与 Watchdog 外环。</p>

!!! info "延伸材料"
    系统解读见 [设计抽象 §6](design-abstract.md#sec-6-persistence)。本节保留原文与译文；部署见 [proto-006](proto-006-deployment.md)。

## 5–6. Persistence and Watchdog

!!! note "原文（English）"

    ## 5. Separating Compute, State, and Environments

    Existing local agents bind together, on the same device, the compute that advances the Agent Loop, the session state that must be persisted, and the execution environment, such as the code repository and local toolchain. Once the device sleeps or goes offline, the agent stops running and cannot access environments on other devices. To make a Super-agent persistently available and flexible enough to connect to different real working environments, we can borrow the idea of compute-storage separation from computer systems and decouple these three elements in two steps. First, separate the compute that advances the Agent Loop from persistent session state, freeing the agent from the lifecycle of a single machine and enabling continuous operation in the cloud (Section 5.1). Then decouple the cloud-based Agent Loop from state-heavy execution environments, leaving real working environments where they are while supporting access across devices (Section 5.2).

    ### 5.1 Separating Compute from State

    The compute that advances the Agent Loop can be stateless; what needs to be persisted is session state. The first layer of decoupling therefore places this compute in stateless, elastic containers in the cloud, while storing persistent state such as Sessions in a database. Stateless compute containers can scale horizontally, restart, or be replaced. Persistent storage preserves continuity of sessions and tasks, freeing the agent from the lifecycle of any single local device and enabling it to remain continuously online in the cloud.

    ### 5.2 Separating Compute from Execution Environments

    Once the Agent Loop and session state reside persistently in the cloud, the next question is how to access the execution environments tasks require. Much of the context a model needs for its work must be obtained through tool calls into code repositories, local files, dependent services, and other parts of an execution environment. In this broader sense, context includes the execution environment itself.

    There are two common approaches. One is to launch a sandbox in the cloud, pull the code, and try to reconstruct and synchronize the environment. But real working environments often contain uncommitted local changes, private credentials, dependencies on internal network services, and specific hardware or toolchains. Synchronizing such environments is technically expensive and prone to omitting information. The other approach is to ask users to abandon their local environments and move their work to the cloud, but the migration cost is too high for many users.

    If the environment itself is what is difficult to copy and synchronize, there is no need to move it to the cloud alongside the Agent Loop. The second layer of decoupling lets the Agent Loop run in the cloud while leaving state-heavy execution environments in place. A lightweight daemon on the device stores no session state and is responsible only for executing commands and returning results. In effect, the execution environment remains outside the cloud, and the agent reads context from it on demand.

    This aligns with Anthropic's idea of "decoupling the brain from the hands": keeping the harness stateless, storing session logs outside it, and connecting to execution environments on demand through a unified interface. The difference lies in the setting. That article describes a managed service, mainly discussing sandboxes created on demand and resources inside customers' VPCs. Here, the first environments to connect are the personal machines people use every day, which may be online only intermittently and contain uncommitted changes and local credentials. This design therefore supports both BYOM—Bring Your Own Machine, connecting a user's everyday development machine or personal computer to a cloud agent—and cloud-hosted machines or compute environments created on demand.

    An agent is consequently no longer limited to one machine. It can switch between execution devices, choosing an environment based on the task to execute commands and retrieve context. It can even support intermittently connected devices: after a user's personal device comes back online, the cloud agent can automatically trigger and complete tasks that need to run there.

    ## 6. Watchdog

    Persistent operation requires more than the compute-storage separation described in Section 5. The system also needs to recover automatically from exceptions over long periods of operation. It is difficult for an always-on distributed system to remain reliable over time using only its own internal logic. An independent external control loop is needed outside the main system.

    In an agent system, traditional process-liveness probes or monitoring scripts are not enough. Many failures do not involve a process crashing; instead, the model gets stuck in an unproductive loop or drifts away from its objective. A long-running agent system must therefore monitor progress on the tasks themselves, not just infrastructure liveness.

    We configured an independent Watchdog Session outside the main system, driven by scheduled messages to form an external control loop. In addition to addressing infrastructure failures such as API errors and failed state restoration, the Watchdog uses a model to judge whether Workers are making substantive progress and whether they have deviated from the overall objective. When it detects an exception, it intervenes promptly to repair it. For intermittent external failures, it keeps checking until normal operation resumes.

    Because the Watchdog is itself an Agent Session with a full set of tools, maintainers can ask it about failures in natural language. They can also ask it to modify the main system's behavior while it is running, for example by adding or removing prompts or adjusting trigger rules.

!!! quote "译文（中文）"

    **第 5 节 分离计算、状态与环境** — 现有本地 agent 常在同一设备上绑定：推进 Agent Loop 的计算、需持久化的 Session 状态、以及代码库与本地工具链等执行环境。设备休眠或离线则 agent 停止，也无法访问其他设备上的环境。要使 Super-agent 持久可用并灵活连接不同真实工作环境，可借鉴**存算分离**，分两步解耦：先把推进 Loop 的计算与持久 Session 状态分离，使 agent 脱离单机生命周期、可在云端持续运行（5.1）；再解耦云端 Agent Loop 与「重状态」执行环境，环境留在原地、跨设备访问（5.2）。

    **5.1 计算与状态分离** — 推进 Loop 的计算可无状态；需持久的是 Session 状态。第一层解耦把计算放在云端无状态弹性容器中，Session 等状态存入数据库。无状态容器可水平扩展、重启、替换；持久存储保留 Session 与任务连续，agent 不再绑死单台本地设备。

    **5.2 计算与执行环境分离** — Loop 与状态已在云端持久化后，问题是任务所需执行环境如何访问。模型所需大量上下文须通过工具访问代码库、本地文件、依赖服务等——广义上**上下文包含执行环境本身**。

    常见做法一：云端起沙箱、拉代码、重建同步环境——真实环境常有未提交改动、私有凭据、内网依赖、特定工具链，同步昂贵且易漏。做法二：要求用户放弃本地迁到云端——迁移成本对许多人过高。

    若难复制同步的正是环境本身，则不必把环境随 Loop 一起上云。第二层解耦：Loop 在云端，重状态执行环境留在原地；设备上轻量 **daemon** 不存 Session 状态，只执行命令并返回结果。环境留在云外，agent 按需读取。

    这与 Anthropic「脑手分离」思路一致： harness 无状态、日志外置、统一接口按需连执行环境；差异在于场景——该文偏托管服务与 VPC 沙箱；此处优先连接日常个人机器（可能间歇在线、含未提交改动与本地凭据），故支持 **BYOM（自带机器）** 与按需云主机。

    agent 不再限于单机，可按任务切换执行设备；间歇在线设备恢复后，云端 agent 可自动触发须在该设备完成的任务。

    **第 6 节 Watchdog** — 持久运行仅靠第 5 节存算分离不够，长跑还需自动从异常恢复；常在线分布式系统很难仅靠内部逻辑长期可靠，需要主系统外的**独立外环**。

    传统进程存活探测不够：许多故障非进程崩溃，而是模型陷入无效循环或偏离目标。长跑 agent 须监控**任务进展**，而非仅基础设施存活。

    团队在主系统外配置独立 **Watchdog Session**，由定时消息驱动外环：除 API 错误、状态恢复失败等基础设施故障外，用模型判断 Worker 是否有实质进展、是否偏离总目标；发现异常及时干预修复；间歇外部故障则持续检查直至恢复。

    Watchdog 本身是具有完整工具的 Agent Session，维护者可用自然语言询问故障，也可在运行时修改主系统行为（增删 prompt、调整触发规则等）。
