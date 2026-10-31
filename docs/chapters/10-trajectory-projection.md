# 第 10 章：轨迹投影折叠引擎——把事件日志折叠成回合台账

> 对应 dsh 真实源码：`packages/client/ui-trajectory`（Trajectory 是 web 专属视图；数据源 = 同一份事件溯源日志，经 `session/page` RPC 分页后在浏览器端投影）。
> 前置：第 1、4、7 章。本章产出：`miniharness/client/trajectory.py`；测试 `tests/test_trajectory.py`。

## 10.1 这一章要做什么

事件溯源日志是给机器读的，人怎么看一次会话？Trajectory 给出的答案是**投影**：把同一份日志折叠成按 turn 组织的可检查台账。本章讲这个折叠怎么算——事件流进，`TrajectorySnapshot` 出。

Trajectory 是 web 专属的"Agent 的 DevTools"：按 turn 组织的事件台账，带 Overview 时间线（TTFT 两色）、局部检查器、搜索、虚拟化滚动。设计上它不是独立数据系统——没有 session-timeline 包，不额外写入磁盘，就是同一份会话日志的浏览器端折叠（`packages/client/ui-trajectory/README.md`）。这样日志是唯一数据源，折叠永远可重放、可纠正。

```mermaid
flowchart LR
  Log["Session 事件日志"]
  Fold["fold_trajectory&lt;br/&gt;纯函数"]
  Out["终端 / JSON / webui"]
  Log --> Fold --> Out
```

上游折叠的三个要点：

1. **折叠是纯函数**：每个折叠 target 用独立 definition（`match/update/finalNode`）物化，无副作用、可重入（`trajectory-*-definition.ts`）；
2. **保留边界**：设计笔记明确拒绝"把日志拍平成裸记录流"——Turn/Step/Request 边界保留因果结构（`.agents/notes/archived/feature/2026-07-27-trajectory-inspection-ledger.md:44`）；
3. **产物是快照**：`TrajectorySnapshot` 含 `systemPrompts?`、`eventNodes`、`eventLocations`、`requests`、`callSchemas`、`partial`、`runningCalls`（`trajectory-contract.ts:69-78`）。

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

### 折叠算法

`fold_trajectory(events, first_seq=0)` 按事件顺序扫一遍，边扫边建节点，收尾时再汇总 turn 摘要：

1. **窗口**：`seq < first_seq` 的事件整条跳过。只关心某段区间时靠这个参数（headless 的 `summarize` 是同构的另一份实现，见 §10.4）。
2. **`turn/start`**：新建一个 `turn` 节点（`id` 由 `mkid("turn")` 生成，形如 `turn-1`），记为当前打开的 turn，并登记该 turn 的摘要框架（`user_texts` / `assistant_texts` / `tool_calls` / `ttft_ms` / 起止时间）。
3. **`user/message`**：建 `user` 节点，`text` 取 `data.content` 里所有 text 块拼接；`turn`/`step` 从事件 data 取。
4. **`assistant/message`**：建 `assistant` 节点，`text` 取 `data.message.content` 里的 text 块拼接；tool-call 块不并进消息文本。
5. **`tool/call`**：建 `tool-call` 节点，`call_id` 取 `data.callId`。
6. **`tool/result`**：`call_id` 取 `data.message.source.callId`，反向扫描已建节点栈，找最近一个 `call_id` 相同的 `tool-call` 作父节点，把本节点追加进 `parent.children` 并记 `parent_id`。**父子树按 callId 关联装配**，与节点位置无关。
7. **`request/header`**：追加进 `requests` 元数据（seq / turn / step / model / provider / reason）。
8. **`turn/end`**：回填当前打开 turn 节点的 `ended_at`。

扫完后做两件事：

- 把消息节点按 turn 归并进摘要（user/assistant 文本累积、tool-call 计数），并补每个 turn 的 `ended_at`；
- 判定 `partial`：折叠结束时如果还有一个打开的 turn（只有 `turn/start` 没有配对的 `turn/end`），`partial=True`，该 turn 的 `ended_at` 为 `None`，`duration_ms` 返回 `None`——这就是"崩溃尾部"。

**节点顺序**：`snapshot.nodes` 就是事件到达顺序。事件 seq 单调递增，所以它等价于按 seq 排序；上游是把 contributions 先按 `anchorSeq` 排序、最终节点再按 `seq` 排序（`trajectory-snapshot-builder.ts`），结果相同。`assistant` 与 `user` 节点的 `id` 前缀不同（`assistant-1`、`user-1`），计数器各算各的。

**TTFT** 是每个 turn 的独立量：取该 turn 内第一条带内嵌 `stream` 的 `assistant/message`，用其首条记录时间减去该 turn 的 `turn/start` 时间。`stream_first_time` 读 `stream[0]`：记录 `type == 'chunk'` 时取 `time`，否则取 `time0`；该 turn 没有内嵌流时 `ttft_ms` 为 `None`。上游 Overview 的 TTFT 两色投影就基于这个差值。

### 快照字段与上游的映射

mini 的字段名与上游不完全相同，对照如下：

| mini | 上游 | 说明 |
|---|---|---|
| `nodes` | `eventNodes` | 按 seq 排序的节点列表；mini 用 dataclass，上游用 `ConversationNode` |
| `turns` | 无同名项 | 每 turn 摘要（user_texts / assistant_texts / tool_calls / ttft_ms / 起止）；上游把这些信息分散在节点与 `requests`，Overview 从图重算 |
| `requests` | `requests` | mini 只留 seq/turn/step/model/provider/reason 元数据 |
| `partial` | `partial` | 同名不同型：mini 是 `bool`（末尾有未闭合 turn），上游是 `TrajectoryPartialAssistant \| null`（流中的部分 assistant 投影） |
| `parent_id` / `children` | 节点内 `subCalls` + `callSchemas` | mini 用父子指针挂 tool-result；上游把工具 schema 单独放 `callSchemas`（callId → schema） |
| （无） | `eventLocations` / `runningCalls` / `systemPrompts?` | mini 不做虚拟化定位、运行中调用清单与独立 system prompt 块 |

`fold_events_json(events)` 把 `partial` / `turns` / `requests` 序列化成 JSON，供终端或 CI 断言。

## 10.3 消费端：三个只读视图

```python
snapshot.messages()            # 折叠后的消息序列（role + text，按 seq 序）
snapshot.last_assistant_text() # 最后一条非空 assistant 文本（与 headless 语义呼应）
snapshot.format_text()         # 终端可读台账（turn 分组 + 缩进 + 耗时）
```

`last_assistant_text()` 与 headless 的 `summarize` 语义一致——`summarize` 是最简投影（只拼 text 块），本模块是完整折叠（保留边界 + 工具树 + TTFT + partial 标记）。

## 10.4 与 headless 的关系

`summarize`（第 7 章）是"投影"的一次极简使用：`firstSeq` 起、turn/start 之后收集、最后一条非空文本胜出。折叠引擎是它的泛化：同一份事件流，可以只取最后文本（headless），也可以展开成完整台账（web Trajectory），还可以序列化成 JSON 给 CI 断言：

```python
fold_events_json(events)   # {"partial":…, "turns":[…], "requests":[…]}
```

## 10.5 硬性规定

1. 折叠是**纯函数**：同一事件流两次折叠结果一致（无状态泄漏）。
2. **turn 摘要由 turn/start 驱动**：`first_seq` 过滤掉 turn/start 后不产生 turn 摘要（消息节点仍在）。
3. **tool-call 节点唯一**：只来自 `tool/call` 事件；`tool/result` 按 callId 挂为子节点（父子树）。
4. **崩溃尾部 = partial**：末尾仍有未闭合 turn 时 `partial=True`，`duration_ms` 为 None。
5. **TTFT**：turn/start 到该 turn 首条 `assistant/message` 内嵌流首条记录时间的时间差；无内嵌流则为 None。

验证：`python -m unittest tests.test_trajectory -v`；真实 loop 回合折叠也覆盖（`TestFoldFromRealLoop`）。

## 10.6 检查点

1. 说出"投影"设计的价值：日志唯一数据源，折叠可重放可纠正。
2. 解释 turn 摘要为什么必须由 turn/start 驱动（而不是从消息推断）。
3. 手动构造一个崩溃尾部事件流，观察 partial 标记与 duration=None。
4. 说出 mini 折叠引擎（`client/trajectory.py`）相对上游的简化：折叠语义一致，无 UI 层；`nodes`/`eventNodes`、`turns` 等字段的映射见 §10.2 表。
5. 解释 tool-result 为什么按 callId 挂父子树，而不是按事件先后简单串联。
