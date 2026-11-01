# 候选原则总表

<p class="lead">全部为<strong>候选</strong>，默认未采纳。生效 subset 仅见 <a href="adopted.md">adopted.md</a>。设计叙事与图见 <a href="design-abstract.md#sec-9-principles">设计抽象 §9</a>。</p>

| ID | 原则（一句） | 来源 | 可检验口径（采纳后用于验收） | 状态 |
|----|--------------|------|------------------------------|------|
| SA-A-01 | 用户侧呈现单一连续 Agent，底层 Session 为实现细节 | 论文 §1 | 产品入口不强制用户选择/记忆多个 Session 才能延续同一任务叙事 | 未采纳 |
| SA-A-02 | 协调层不承担重执行任务 | 论文 §2.1 | 路由/评估层不配置与 Worker 等价的重执行工具面 | 未采纳 |
| SA-A-03 | 环境事件经确定性 Fast Path 过滤后再进入模型 Slow Path | 论文 §3.1 | 存在可配置的代码级准入，未过准入的不调用评估模型 | 未采纳 |
| SA-A-04 | 任务归属建立后，同 Thread 后续事件优先代码直送执行 Session | 论文 §3.2 | 已订阅 Thread 的 follow-up 不经协调层 LLM 转发即可达 Worker | 未采纳 |
| SA-A-05 | 对外交互经统一出口 | 论文 §2.2 | 同一外部信道不出现多个执行 Session 并行直接回复 | 未采纳 |
| SA-A-06 | 长耗时工具异步化，步边界可接新输入 | 论文 §4.1 | 慢工具 start 后当前 Session 可在下一步处理新用户消息 | 未采纳 |
| SA-A-07 | 输出匹配信道带宽 | 论文 §4.2 | IM 类信道有短回复上限或等价策略；PR/邮件类有结构化长文模板 | 未采纳 |
| SA-A-08 | 主动行为采用推测/静默策略 | 论文 §3.4 | 无把握时不向外部信道推送结果 | 未采纳 |
| SA-P-01 | 占用人类注意力的一切输出必须净收益为正 | proto 000 | 每条对外通知可对应「需人行动或可用的结果」 | 未采纳 |
| SA-P-02 | 领域概念极简：需求优先成为既有概念属性 | proto 001 | 新增实体类型需有「无法作为属性」的论证 | 未采纳 |
| SA-R-01 | Ticket 自有生命周期，不随 Executor Session 机械绑定 | proto 001 | 执行 Session 切换/中断不自动等价于工单状态 | 未采纳 |
| SA-S-01 | 系统级双层分派优于单 Session 内 prompt 委派 | 论文 §7.1 | 多任务并发场景有固定 Worker 分派路径，非仅依赖模型即时决定 | 未采纳 |

## 来源与延伸译文

| ID | 主文 | 英文/逐句核对 |
|----|------|----------------|
| SA-A-01 | [§1–§2](design-abstract.md#sec-1-motivation) | [paper-01](paper-01-motivation-and-four-traits.md) |
| SA-A-02, SA-A-05 | [§3](design-abstract.md#sec-3-two-layer) | [paper-02](paper-02-two-layer.md) |
| SA-A-03, SA-A-04, SA-A-08 | [§4](design-abstract.md#sec-4-events) | [paper-03](paper-03-event-driven.md) |
| SA-A-06, SA-A-07 | [§5](design-abstract.md#sec-5-interaction) | [paper-04](paper-04-human-interaction.md) |
| SA-S-01 | [§9](design-abstract.md#sec-9-principles) | [paper-06](paper-06-experiments-and-open.md) |
| SA-P-01 | [§5](design-abstract.md#sec-5-interaction) | [proto-000](proto-000-philosophy.md) |
| SA-P-02, SA-R-01 | [§4](design-abstract.md#sec-4-events)、[§7](design-abstract.md#sec-7-prototype) | [proto-001](proto-001-domain.md) |

Prototype 全文译文见 [proto-000](proto-000-philosophy.md)–[008 Cookbook](proto-008-cookbook.md)；粒度说明见 [sources.md](sources.md#sec-translation-granularity)。
