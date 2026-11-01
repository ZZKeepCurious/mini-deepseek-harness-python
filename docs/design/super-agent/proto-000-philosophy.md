# Prototype · 000 哲学

<p class="lead">对照 `dsh-superagent-prototype/docs/000-philosophy.md`。</p>

!!! info "延伸材料"
    注意力与交互原则见 [设计抽象 §5](design-abstract.md#sec-5-interaction)、[§9](design-abstract.md#sec-9-principles)（SA-P-01）。本节保留原文与译文。

!!! note "原文（English）"

    dsh-superagent is a framework, and also a way of working with an agent. Its core principle:

    **Take as little human attention as possible, and give as much value as possible.**

    Every part of the project is judged by the attention it costs. A message, a notification, a question, a setting, a panel element and a paragraph of documentation each cost someone's attention, and each has to give more value than that, or it is removed.

    ## What follows for the agent

    - **It acts only when asked, or when a written rule allows it.** A request counts only when it addresses the agent directly and comes from a person the team allowed: one on an allowed list, or anyone in a conversation the team opened to requests. Work nobody asked for needs a rule the team wrote, and starts in a mode that only records what the agent would have done.
    - **It speaks only when a person must act or can use a result.** It confirms a request once and names what it will do. It stays quiet while it works. It writes again when a result is ready, when it needs a decision, or when it cannot finish. Messages are short and go to the conversation where the request was made.
    - **It asks one concrete question at a time,** in a structured form, so the question reaches the person instead of staying inside a long report.
    - **It stops by impact, not by effort.** A local change goes as far as a reviewable result. A change that needs a design decision stops with a clear question.
    - **It reports honestly.** A draft is marked unfinished, a failure is reported as a failure, and a limit is never bypassed to avoid asking.

    ## What follows for the design

    - **Few concepts.** The design has only its domain concepts, such as a Source, and their concrete implementations, such as GitHub and Feishu. A business need becomes an attribute of one of them, or a prompt the agent follows. The design does not add a concept for it.
    - **Few settings.** A default replaces a setting when one value suits almost every team. Behaviour that a team describes in words is prompt text, not a setting.
    - **A panel that shows only what needs a person.** It lists first what waits for a person. A healthy state takes no space.
    - **Documents read in order.** Each numbered document builds only on the ones before it, so a reader never needs a concept that has not been introduced yet.

    ## Responsibility

    The agent comments, opens pull requests and messages people under accounts your team sees every day. Every message it sends costs your colleagues' attention, and you are responsible for it. The more the agent can do, the more care its operator owes the team:

    - **Start narrow.** Begin with a short allowed list and with proactive work only recorded. Widen both once what the agent would have done looks right.
    - **Tune for less noise.** When the agent is noisy, make its prompts shorter and stricter, or pause it. Do not add more messages to explain the noise.
    - **Do not use it to produce volume.** Mass pull requests, broadcast messages and busywork cost the team more than they give.
    - **Own what it says.** Read what the agent posts in your name, and correct it in the prompts when it is wrong.

    The goal is the right help at the right time, and nothing at any other time.

!!! quote "译文（中文）"

    dsh-superagent 既是一个框架，也是一种与 agent 协作的方式。核心原则是：

    **尽可能少占用人类注意力，尽可能多交付价值。**

    项目的每个部分都用所消耗的注意力来衡量。一条消息、一次通知、一个问题、一项设置、面板上的一个元素、文档里的一段文字，都在消耗某个人的注意力；每一项给出的价值必须大于消耗，否则应被移除。

    **对 agent 的要求**

    - **仅在被动请求或书面规则允许时行动。** 请求须明确指向 agent，且来自团队允许的人：白名单上的人，或团队已对之开放请求的会话中的任何人。无人请求的工作须有团队写下的规则，并从未真正接单、只记录「本会怎么做」的模式开始。
    - **仅在有人必须行动或可使用结果时发言。** 确认请求一次并说明将做什么；工作期间保持安静；结果就绪、需要决策或无法完成时再写。消息要短，发在提出请求的对话中。
    - **一次只问一个具体问题，** 用结构化形式，让问题抵达人而不是埋在长报告里。
    - **按影响停手，不按费力程度。** 局部改动做到可评审结果；需要设计决策的改动以清晰问题停止。
    - **诚实汇报。** 草稿标未完成，失败报失败，不为避免提问而绕过限制。

    **对设计的要求**

    - **概念要少。** 设计只有领域概念（如 Source）及其实现（如 GitHub、飞书）；业务需求应成为其中之一的属性，或 agent 遵循的 prompt，不为需求新增概念。
    - **设置要少。** 几乎对所有团队都合适的默认值取代设置；用自然语言描述的行为写进 prompt，而非设置项。
    - **面板只展示需要人的项。** 优先列出等待人的工作；健康状态不占空间。
    - **文档按序阅读。** 编号文档逐层叠加，读者不必在未介绍过的概念上阅读。

    **责任**

    agent 会以团队日常见到的账号评论、开 PR、发消息；每条消息都在消耗同事注意力，**运营者负责**。能力越大，运营者对团队的审慎义务越大：

    - **从窄开始。** 短白名单 + 主动工作仅记录；确认「本会怎么做」合理后再放宽。
    - **为减噪而调优。** 太吵则缩短加严 prompt 或暂停；不要用更多消息解释噪声。
    - **不要用来刷量。** 批量 PR、广播与忙闲杂务给团队的消耗大于收益。
    - **对发言负责。** 阅读 agent 以你名义发布的内容，错误则在 prompt 中纠正。

    目标是在正确的时间提供正确帮助，其他时间什么都不做。
