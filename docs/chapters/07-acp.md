[← 第 7 章导读](07-external-entry-points.md)

入口组合：[Fig S5](../system-map.md#fig-s5)。

# 第 7 章 · §7.7 ACP

## 7.7 ACP 最小子集（`miniharness/protocol/acp.py`） {#sec-7-7}

> 对应 dsh 真实源码：`packages/acp/acp`（`apply` + `codec.ts`）。自动化专用约定完全一致，跑在假模型上。

### 7.7.1 握手与会话

- `initialize` → `agentInfo.name == 'deepseek-harness-acp'`、`promptCapabilities.image` 按 worker 实际能力**条件声明**（`supports_acp_image_prompts(attachment, adapter)`：附件服务在场且 adapter 声明 image 输入模态时置 `true`，否则 `false`）、`audio`/`embeddedContext` 恒 `false`、`authMethods: []`、`sessionCapabilities:{close, list, resume}`（mini 不宣称 mcpCapabilities.http）——本桥承诺 text / resource_link，并在能力具备时受理 image；
- `new_session` / `resume`（selectionFor 恢复路由）/ `list`（keyset 分页）/ `close`：cwd 必须绝对路径；`additionalDirectories` 非空拒绝；`mcpServers` 非空拒绝；mint sessionId；
- `cancel`：未知 session **no-op**，已知 session 取消 agent。

### 7.7.2 prompt 的结算语义

同步模型下 `prompt` 直接跑完整回合再返回 stopReason，等价于上游"等到 whole-agent idle"：

1. session 必须存在（unknown → invalid params）；已有 inflight → 拒绝（"a prompt is already in flight for this session"）；
2. 只支持 `text` 与 `resource_link`（resource_link 渲染为显式文本引用，不静默丢弃）；空 prompt 拒绝；
3. turn/end reason → stopReason 映射（`codec.ts` 同构）：`completed→end_turn`、`max-tokens→max_tokens`、`aborted→end_turn`（`cancelled` 保留给显式 client 取消）、`interrupted→cancelled`、`blocked/error→end_turn`；
4. turn/end kind='error' → 以 "turn failed: …" 立即拒绝（模型失败直接表现为 prompt 错误）。

### 7.7.3 审批桥

`approval/request` 监听器：仅当 `callId` 存在时提供**二选一**（`allow-once` → `allowed-once`、`reject-once` → `rejected`、`cancelled` → `cancelled`）；callId 缺失 → 委派 next（不处理）。一次决策、绝不从未知客户端响应推断持久授权。

### 7.7.4 硬性规定

1. 握手按能力声明富媒体：`promptCapabilities.image` 动态（`supports_acp_image_prompts`），`audio`/`embeddedContext` 恒 false；admitAcpPrompt 受理 image 时走富媒体管线（`attachment/`，见 6 章）。
2. cwd 非绝对路径、additionalDirectories 非空、mcpServers 非空 → invalid params（-32602）。
3. prompt：未知 session / inflight / 空 prompt / 非 text·resource_link 内容 → 拒绝；回合真实跑完（turn/start + turn/end 落日志）。
4. stopReason 映射表逐项与上游一致。
5. 审批桥：callId 缺失委派；allow-once/reject-once/cancelled 三态映射；默认 answerer 允许（测试注入可换）。
6. close 后一切请求 → internal error（-32603，文案 "the ACP bridge has been disposed"，acp.py）。

验证：`python -m unittest tests.test_acp -v`。

### 7.7.5 简化标注

- 上游 async（whenIdle 等待、stream 通知、`agent_message_chunk` 增量）；mini 同步跑完整个回合但 `session/update` **并发逐块流式**——`AcpServer._install_update_stream` 订阅 `session/event`（同上游 onSessionEvent）把已提交事件逐事件实时投影，`update_sink` 即时外发（stdio worker 经 `_acp_update_sink` 逐块写通知、先于 prompt 响应帧）；in-process 载体 `update_sink=None` 时收敛 `server.updates` 批量。assistant/message 带 usage 且会话有 contextWindow 时另发 `usage_update`；
- 上游经 cordis 插件挂载（`inject: ['agents']`）+ ACP SDK 的 stdio 连接；mini 直接操作服务对象；
- inflight 拒绝在同步模型下只能手动置标志触发（真并发不存在）。

