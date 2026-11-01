[← 第 7 章导读](07-external-entry-points.md)

入口组合：[Fig S5](../system-map.md#fig-s5)。

# 第 7 章 · §7.6 JSON-RPC SDK

## 7.6 JSON-RPC 信封最小子集（`miniharness/protocol/sdk.py`） {#sec-7-6}

> 对应 dsh 真实源码：`packages/sdk/protocol`（`transport.ts` + `types.ts`）。信封层完全一致，三个方法（initialize / session/prompt / shutdown）接在内存假模型上，"可编程驱动 harness"成立。

### 7.6.1 线协议（`JsonRpcLineTransport`）

newline-delimited JSON-RPC 2.0，每行一个紧凑 JSON 帧。帧分类与上游 `transport.ts` 逐条一致：

1. `id` + `method` → **请求**：无 handler 答 `-32601`；handler 抛错答 `-32603`（带 message）；
2. 仅 `id` → **响应**：`error` 对象 → 以 `JsonRpcResponseError` 拒绝 pending（保留 wire `code` 与 `data`）；否则 resolve `result`；
3. 仅 `method` → **通知**：无 handler 直接丢弃，params 可省略（省略时不带 `params` 成员）。

细节：畸形 JSON 行忽略；`request` 的 id 为 `req_` + uuid（无连字符）；params 归一化（数组/标量折叠为 `{}`）；`close()` 拒绝全部 pending 而不销毁流；`flush()` 以空行 barrier 表达语义。

mini 的同步近似：上游是字节流 + async，mini 是"行馈送 + 内存输出"——`request()` 返回 `PendingRequest`，feed 到对应响应帧时 settle。帧分类、错误码、id 配对语义完整保留。

### 7.6.2 最小运行服务（`SdkRuntime`）

同 `types.ts` 的三个请求方法：

| 方法 | 语义（上游） | mini |
|---|---|---|
| `initialize` | cwd/provider/model + 可选 maxTokens → `serverInfo` | 记录参数，返回 `{"serverInfo": {"name": "deepseek-harness-sdk-runtime", "version": "0.0.1"}}`（name 是 wire 稳定标识） |
| `session/prompt` | 未知 sessionId **懒创建** agent+session；返回 durable enqueue 回执 `messageId` | 懒创建 `AgentLoop`，同步跑完一个回合，返回真实消息 id |
| `shutdown` | → `{}` | `{}` |

`messageId` 只标识入队的 user 消息，不标识任何后续 assistant 消息或回合结束（上游 README 明确）。mini 的 `messageId` 是 `create_message` 签发的真实 id，与会话日志中 `agent/inbox/spliced` 的 `inserted` 消息 id 一致——官方 SDK 客户端 `Session.run` 依赖这条回执确认投递（`python/sdk api.py _is_inbox_receipt`，互操作测试见 §7.6.4）。

通知（`session.event` / `session.status` / `subagent.*`）在 mini 中按回合透传：一次跑完后逐条发 inbox 回执、assistant/message、turn/end，末尾发 status idle。上游是逐块流式，mini 是回合级，属简化标注。

### 7.6.3 硬性规定

1. 帧分类三态判定与上游一致；畸形行忽略不产生输出。
2. 无 handler → `-32601`；handler 抛错 → `-32603` 且 message 原样；错误响应带 `JsonRpcResponseError(code, data)`。
3. 响应帧只 settle 匹配 id 的 pending；未知 id 忽略；close 后拒绝写入。
4. `serverInfo.name` 恒为 `deepseek-harness-sdk-runtime`。
5. `session/prompt` 未知 sessionId 懒创建会话，返回真实消息 id（与 inbox 回执一致，非递增计数器）。

验证：`python -m unittest tests.test_sdk_protocol -v`（含端到端 stdio 行仿真）。

### 7.6.4 互操作验证：官方 Python SDK 驱动 mini worker

互操作测试用上游官方 `python/sdk` 的 `DeepSeekHarness` 客户端，经私有 `_launch_args` 关键字把 mini worker 起成子进程（`python -m miniharness.seams.subagent.worker sdk`）。它回答的是"wire 约定是否双向成立"，而不是"mini 自己能不能跑"。

- **客户端不把响应当答案**：`Session.run` 发出 `session/prompt` 后，先等 `agent/inbox/spliced` 的 `inserted` 里出现回执的 `messageId`，再收集 `assistant/message` 与 `turn/end`，直到 `session.status` 变成 `idle` 才结算 `final_response` / `finish_reason`。这验证了两条约定：回执 id 是真实入队消息 id；回合结束以 status 通知为准。
- **会话复用只推新事件**：同一会话第二次 run 时，mini worker 用 `_event_boundary` 记住上次透传的边界，只发本次投递后的事件。若重发历史回合，客户端会拿到重复消息，测试会因客户端 `received` 门控侥幸遮蔽而假通过（上游服务端同样只推新增事件）。第二个回合的 `turn/end` 里 turn == 2，证明会话状态被复用而不是重建。
- **通知先于响应帧**：worker 的 `_SdkWorkerRuntime` 先同步跑完整个回合，再逐条写 `session.event`（inbox 回执 → assistant/message → turn/end），末尾写 `session.status == idle`，最后才写 `session/prompt` 的响应。客户端依赖这个顺序在响应到达前收齐事件。

两个可选前提缺一即 skip：本机装 `pydantic>=2.12`；上游 SDK 源码可达（`MINIHARNESS_UPSTREAM_SDK` 指向 `python/sdk/src`，缺省探测 `../deepseek-harness/python/sdk/src`）。测试不假设工作区布局，找不到就跳过，不进入默认 CI。

验证：`python tests/test_upstream_sdk_interop.py`（需环境变量 + pydantic）。

