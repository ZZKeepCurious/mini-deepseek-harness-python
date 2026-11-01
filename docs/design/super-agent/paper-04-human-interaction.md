# 论文 · 04 人机交互

<p class="lead">对应论文第 4 节：全双工异步 ReAct、跨信道输出密度。</p>

!!! info "延伸材料"
    系统解读见 [设计抽象 §5](design-abstract.md#sec-5-interaction)。本节保留原文与译文；prompt/面板细节见 [proto-004](proto-004-prompts.md)、[proto-005](proto-005-panel.md)。

## 4. Human-Centered Interaction

!!! note "原文（English）"

    The two-layer architecture in Section 2 ensures continuity of context across channels. But achieving both continuity and efficiency in actual human-agent interaction requires solving two further problems involving interaction methods and channels. First, an asynchronous ReAct Loop must remove the blocking effect of slow background operations on foreground conversation, allowing a session to accept new instructions without waiting for the previous command or task to finish (Section 4.1). Second, when interaction happens across familiar work channels, the system's output must match each channel's bandwidth and people's usage habits (Section 4.2).

    ### 4.1 Full-Duplex Interaction

    In a classic ReAct Loop, once the model makes a synchronous `run()` tool call, the loop blocks until the tool finishes executing and returns its final result. During that period, the session cannot respond to new user input. Removing this blockage requires moving conversation from a half-duplex, question-and-answer mode to a full-duplex mode, as illustrated below.

    To achieve this, we replace time-consuming command and subtask tool calls with asynchronous `start()` and `stop()` operations, creating an asynchronous ReAct Loop. After calling `start()`, the model immediately receives a "Started" response, and the loop no longer waits for the tool. When the background tool finishes, the system delivers its final result to the session as a new event message. The model can also call `stop()`, based on context, to terminate background commands that have been running too long.

    This decouples foreground conversation from background task execution. The agent can respond to human input while running time-consuming tasks in the background. New input is handled as soon as the current inference step ends, without waiting for background tools to finish.

    This asynchronous task mechanism has already been implemented in DeepSeek Harness, but it is not yet enabled by default.

    ### 4.2 Interaction Across Channels

    Different work channels have different information densities and interaction conventions, and the system's output must match them. Moving an interface designed for a single-user terminal directly into a group chat—reproducing long reasoning traces and frequent tool calls verbatim—can easily overwhelm people collaborating in a group.

    In a Super-agent, the two-layer architecture already solves part of this problem. Long reasoning traces and frequent tool calls for specific tasks are isolated inside Worker Sessions rather than sent directly to external channels. The Router layer handles external interaction centrally. On top of this, the system needs to connect to the work environments people use every day and use prompts to tailor interaction conventions to each channel:

    - Instant messaging channels: Emphasize lightweight communication with minimal interruption. Like a human colleague, send notifications only when a decision or acceptance of results is needed, ask direct questions, and provide brief conclusions. One simple, effective hard rule we have tested is to keep each reply to no more than 100 words.
    - Asynchronous channels such as GitHub, email, and documents: Emphasize structured delivery with a high information density. For example, when creating or updating a Pull Request, write a complete account of the problem, changes, and validation results according to the project's conventions. In a personal-assistant setting, draft structured emails and document summaries directly.

!!! quote "译文（中文）"

    第 2 节的双层架构保证跨信道上下文连续，但要在真实人机交互中同时做到连续与高效，还需解决交互方式与信道两类问题：其一，**异步 ReAct 循环**须消除慢速后台操作对前台对话的阻塞，使 Session 不必等上一条命令或任务结束就能接受新指令（4.1）；其二，在熟悉的工作信道上交互时，系统输出须匹配各信道的**带宽**与使用习惯（4.2）。

    **4.1 全双工交互** — 经典 ReAct 中，模型一旦发起同步 `run()` 工具调用，循环会阻塞到工具执行完毕并返回最终结果，期间 Session 无法响应新输入。消除阻塞需从半双工问答转向**全双工**：耗时命令与子任务改为异步 `start()` / `stop()`。调用 `start()` 后立即收到「Started」，循环不再等待；后台工具结束时以新事件消息把最终结果送入 Session；模型也可 `stop()` 终止运行过久的后台命令。

    前台对话与后台任务解耦：后台跑长任务时仍可响应人类输入；新输入在当前推理步结束即可处理，不必等后台工具结束。该异步机制 **DeepSeek Harness 已实现，但默认尚未开启**（论文表述；mini 对齐状态见 A 线 jobs/shell 域）。

    **4.2 跨信道交互** — 各信道信息密度与交互惯例不同，输出须匹配。把单用户终端界面原样搬进群聊、逐字回放长推理与频繁工具调用，很容易淹没协作者。Super-agent 中 Worker 内长推理不直灌外部信道，Router 集中对外；在此基础上接入日常工作环境，并用 prompt 按信道定制惯例：**即时通讯**——轻量、少打扰，像同事一样仅在需决策或验收结果时通知，问题直接，结论简短；实测有效硬规则之一是每条回复不超过 100 词。**GitHub、邮件、文档等异步信道**——结构化、高信息密度，如 PR 按项目惯例写清问题、变更与验证；个人助手场景可起草结构化邮件与文档摘要。
