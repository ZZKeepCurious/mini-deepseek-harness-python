# 10 轨迹投影折叠引擎：把事件日志折叠成回合台账

> 本章回答一个问题：事件溯源日志是给机器读的，人怎么看一次会话？Trajectory 给出了答案——**投影**：把同一份日志在浏览器端折叠成按 turn 组织的可检查台账。我们复现折叠语义本身（事件流 → 结构化台账），headless 的 `summarize` 是它的最简版，本模块是向完整折叠的演进。
>
> 对应 dsh 真实源码：`packages/client/ui-trajectory`（Trajectory 是 web 专属视图；数据源 = 同一份事件溯源日志，经 `session.history` RPC 分页后在浏览器端投影）。mini 复现于 `miniharness/client/trajectory.py`。

## 10.1 直觉：为什么是"投影"而不是"第二份数据"

Trajectory 是 web 专属的"Agent 的 DevTools"：按 turn 组织的事件台账，带 Overview 时间线（TTFT 两色）、局部检查器、搜索、虚拟化滚动。但设计上它**不是独立数据系统**——没有 session-timeline 包，不额外落盘。它就是同一份会话日志的浏览器端折叠（`packages/client/ui-trajectory/README.md:5`）。好处：日志是唯一数据源，折叠永远可重放、可纠正。

上游折叠的三个要点（已核实）：

1. **折叠是纯函数**：每个折叠 target 用独立 definition（`match/update/finalNode`）物化，无副作用、可重入（`trajectory-*-definition.ts`）；
2. **保留边界**：设计笔记明确拒绝"把日志拍平成裸记录流"——Turn/Step/Request 边界保留因果结构（`2026-07-27-trajectory-inspection-ledger.md:44`）；
3. **产物是快照**：`TrajectorySnapshot = { eventNodes, eventLocations, requests, callSchemas, partial, runningCalls }`（`trajectory-contract.ts:60-68`）。

## 10.2 mini 复现：`fold_trajectory`

折叠核心是一个纯函数：`events → TrajectorySnapshot`。节点保留 turn/step/seq 边界：

```python
@dataclass
class TrajectoryNode:
    id: str
    kind: str            # 'turn' | 'user' | 'assistant' | 'tool-call' | 'tool-result'
    turn: int
    step: int
    seq: int
    started_at: int
    ended_at: int | None = None
    text: str = ""
    call_id: str | None = None
    parent_id: str | None = None
    children: list["TrajectoryNode"] = field(default_factory=list)
```

折叠规则（对应上游 definition 的语义）：

| 事件 | 折叠动作 |
|---|---|
| `turn/start` | 开新 turn 节点 + 登记 turn 摘要框架（turn 摘要**由 turn/start 驱动**，不是由消息推断） |
| `user/message` | `'user'` 节点（text 块拼接） |
| `assistant/chunk` | 只观测 TTFT（不产生节点） |
| `assistant/message` | `'assistant'` 节点（text 块） |
| `tool/call` | `'tool-call'` 节点（callId）——**权威节点**，assistant 消息里的 tool-call 块不重复展开 |
| `tool/result` | `'tool-result'` 节点，按 callId 挂为 tool-call 的子节点（父子树） |
| `request/header` | 进 `requests` 元数据（model/provider/reason） |
| `turn/end` | 回填 turn 节点 `ended_at`（duration 可算） |

产出 `TrajectorySnapshot`：`nodes`（有序层级列表）、`turns`（每 turn 摘要：user_texts / assistant_texts / tool_calls / ttft_ms / 起止）、`requests`、`partial`（末尾有未闭合 turn = 崩溃尾部）。

TTFT 语义：turn/start 到该 turn 首个 `assistant/chunk` 的时间差（上游 Overview 的 TTFT 两色投影即基于此）。

## 10.3 消费端：三个只读视图

```python
snapshot.messages()            # 折叠后的消息序列（role + text，按 seq 序）
snapshot.last_assistant_text() # 最后一条非空 assistant 文本（与 headless 语义呼应）
snapshot.format_text()         # 终端可读台账（turn 分组 + 缩进 + 耗时）
```

`last_assistant_text()` 与 headless 的 `summarize` 语义一致——`summarize` 是最简投影（只拼 text 块），本模块是完整折叠（保留边界 + 工具树 + TTFT + partial 标记）。

## 10.4 与 headless 的关系

`summarize`（第 07 章）是"投影"的一次极简使用：`firstSeq` 起、turn/start 之后收集、最后一条非空文本胜出。折叠引擎是它的泛化：同一份事件流，可以只取最后文本（headless），也可以展开成完整台账（web Trajectory），还可以序列化成 JSON 给 CI 断言：

```python
fold_events_json(events)   # {"partial":…, "turns":[…], "requests":[…]}
```

## 10.5 硬性规定（被测试钉住）

1. 折叠是**纯函数**：同一事件流两次折叠结果一致（无状态泄漏）。
2. **turn 摘要由 turn/start 驱动**：`first_seq` 过滤掉 turn/start 后不产生 turn 摘要（消息节点仍在）。
3. **tool-call 节点唯一**：只来自 `tool/call` 事件；`tool/result` 按 callId 挂为子节点（父子树）。
4. **崩溃尾部 = partial**：末尾仍有未闭合 turn 时 `partial=True`，duration 为 None。
5. **TTFT**：turn/start 到首个 assistant/chunk 的时间差；无 chunk 则为 None。

验证：`python -m unittest tests.test_trajectory -v`；真实 loop 回合折叠也覆盖（`TestFoldFromRealLoop`）。

## 10.6 检查点

- [ ] 说出"投影"设计的价值：日志唯一数据源，折叠可重放可纠正；
- [ ] 解释 turn 摘要为什么必须由 turn/start 驱动（而不是从消息推断）；
- [ ] 手动构造一个崩溃尾部事件流，观察 partial 标记与 duration=None；
- [ ] 说出 mini 相对上游的简化（无虚拟化/增量搜索/UI，折叠语义对齐）。

> 下一章：运行时自我修改——agent 在进程内给自己加插件的生命周期。