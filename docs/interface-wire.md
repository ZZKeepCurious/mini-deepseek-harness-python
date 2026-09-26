# web 接口约定参考（Wire Reference）

> 定位：这是 `mini-deepseek-harness` 前后端的**唯一耦合面**——`miniharness/web/` 传输层对
> 前端发布的所有接口约定（信封、帧、错误语义）。产品化前端（仓库顶层 `webui/`）与任何第三方
> 客户端只依赖本约定，禁止 import Python 内部。正文与当前实现逐条对应，接口约定与上游
> `dsh-v0.1.5-alpha.1` 一致（`packages/client/connection` + `packages/api/gateway` +
> `packages/api/session-controller` + `packages/api/remotes` + `host/frontend-static`）。
> mini 侧保留的简化差异另见项目状态记录。
>
> 载体实现：`web/server.py`（FastAPI）；语法层：`web/envelope.py` + `web/stream_protocol.py`；
> 域实现：`web/api.py` + `web/streams.py` + `web/events.py` + `web/approvals.py` +
> `web/downloads.py` + `web/frontend.py`。教程对照：`docs/chapters/07-external-entry-points.md`
> §7.5（按实现顺序讲解）；本文件是**约定的权威速查**，不讲实现细节。

## 1. 通道总览与载体状态码

| 通道 | 载体 | 覆盖范围 | 载体状态码 |
|---|---|---|---|
| unary RPC | `POST /api/<endpoint>` | `session/*` 14 个 unary 端点 + `$events/result` 特判 | 401* / 404 / 415 / 400 / 500 / 200 |
| Remote 流 | `WS /api/remote.mux` | `$events` + `session/follow` + `session/control` 全部长期流 | HTTP 401*（升级拒绝）/ WS 关闭码 1003 / 1008 / 1011 |
| 会话导出 | `GET /api/session.export` | 会话日志 zip 下载 | 401* / 200 / 400 / 404 / 501 / 500 |
| SPA 静态 | `GET`/`HEAD /{path}`（非 `/api/`） | `webui/dist/` 或 `web/static/` 产物承载 | 403 / 200 / 404 / 405 |

`*` 401 仅在配置了认证 token 时出现（见 §1.1）；未配置 token = 无门（回环开发形态）。

### 1.1 认证门（可选 token，`web/auth.py`）

上游以可插拔的 `connection.requestRejection(req)` 决定 WS 升级拒绝（返回 401/403 →
`rejectRemoteStreamUpgrade` 写 `HTTP/1.1 401 Unauthorized` 后销毁 socket）。mini 的等价物
= **可选 token 门**：未配置（缺省）→ 无门；配置 `MINIHARNESS_WEB_TOKEN`（或
`create_app(token=)` / `run_web(token=)`）后：

- **覆盖面**：`/api/*` 全域（unary POST、`$events/result`、WS mux 升级、`session.export`）；
  SPA 静态不设门（浏览器从页面 URL 携带 token 调 API）。
- **token 载体**（任一命中即可）：`Authorization: Bearer <token>` 头；`?token=<token>`
  查询参数（浏览器原生 WebSocket 无法设头，webui 从页面 URL `?token=` 读取：unary 走
  Bearer 头、WS 追加到 mux URL——`webui/src/wire/auth.ts`）。
- **拒绝形态**：http → `401 {"error":"unauthorized"}`；WS 升级 → 经 ASGI
  `websocket.http.response.*` 写 HTTP 401 响应后 `websocket.close(1008)`（与上游
  `rejectRemoteStreamUpgrade` 同形，uvicorn 支持）。比较为常数时间。
- **部署纪律**：`run_web` 监听 `0.0.0.0` 时**必须**已配置 token，否则启动即
  `ValueError`（非回环裸听 fail loud）。

### 1.2 unary 载体状态码语义（`web/server.py`）

- `404`：非 POST、路径不在 `/api/` 下、或 `method` 不在路由表（`session/*` 之外）。
- `415`：`content-type` 非 `application/json`（跨站写围栏，上游同款安全设计；无 CORS 头）。
- `400`：body 非 JSON（含空体）；`GET/HEAD /api/session.export` 参数缺失/非法同样归 400 文本。
- `500`：信封合法但实现崩溃（纯文本 `handler failure: <error>`）。
- `200`：**一切业务结果**——业务错误恒 200 + `server-response` 且 `result.ok=false`，不借 HTTP 状态码表达业务错误。
- 200 的响应体有两种 `content-type`：`application/json`（默认）与 `multipart/form-data`
  （结果含二进制，见 §3.1）。

路径约束：endpoint 段匹配 `[A-Za-z0-9_$.-]+`（`$` 是真实约定：`$events`、`$events/result` 为网关
内端点）；body `client-request` 的 `payload` 必须**恰**为 `{args:{...}}` 单字段 plain object
（多余键 / 缺 args / 非对象一律 `bad-request`，同 gateway `remoteRequest`）。

### 1.3 WS 关闭码（`web/mux.py`）

- `1003`：收到二进制帧（协议错误）。
- `1008`：文本非 JSON / 帧形状非法 / 重复 `open` 同一 `streamId`。
- `1011`：某流 `error` 帧自身发送失败。
- 心跳：每条连接每 **2s** 一次 transport 级协议 Ping（`web/launcher.py` uvicorn
  `ws_ping_interval=2`），连续 miss 判死预算 **4s**（`ws_ping_timeout=4`，
  ≈ 上游 `MAX_MISSED_HEARTBEATS=2 × interval=2s`）——僵死连接被回收，与上游
  gateway heartbeat 约定一致（`websocketHeartbeatIntervalMs` @default 2000，见 §4.4）。

## 2. 信封层（`web/envelope.py`）

判别联合只有两型，判别字段 = `type`（`server-request`/`client-response` 四象限在 alpha.1 中不存在）：

- **`client-request`** `{type, rpcId, method, payload}` —— wire 载体 = `POST /api/<endpoint>` 的 body。
- **`server-response`** `{type, rpcId, result}` —— wire 载体 = 该 POST 的响应体；`rpcId` 必回显请求方，从不重铸。

**RpcResult**：

```json
{"ok": true, "value": "<任意业务值，可省>"}
{"ok": false, "error": {"code": "...", "message": "...", "details": {...}}}
```

- 成功分支 `value` 可选（业务无值时整体省略该字段）。
- 业务方法绝不抛业务错误——一律经 `result.ok` 表达；`details` 恒为对象。

**RpcError 的 `code` 是命名空间闭集**（统一为 `<namespace>/<name>` 形式，键集与 typert
`RemoteErrorDetailsMap` 一致：基础设施 `gateway/*` 加各域可扩展注册；`web/envelope.py`
`RPC_ERROR_CODES` 收 47 码，下块列本 web 载体直接签发的 33 个，其余由各自域 handler 签发；
路由层边界校验收 `gateway/input-invalid`，未知端点折算为 `gateway/invocation-unavailable`）：

```
gateway/bad-request       gateway/cancelled         gateway/internal
gateway/arguments-invalid gateway/input-invalid     gateway/invocation-unavailable
gateway/protocol          gateway/uplink-overflow
session/not-found
session/model-unavailable session/conflict          session/invalid-time-zone
session/workspace-attach-failed workspace/not-found agent-preset/conflict
agent-preset/not-found    agent-preset/invalid      agent-preset/locked
session/agent-busy
session/attachment-invalid session/queue-item-not-found session/steer-unavailable
session/title-invalid     session/fork-unavailable  subagent/not-found
subagent/catalog-diagnostic subagent/unauthorized
workspace-file/not-found   workspace-file/not-directory workspace-file/not-regular-file
workspace-file/not-text    workspace-file/outside-workspace workspace-file/too-large
workspace-file/watch-unsupported
workspace/session-active
job/not-found
```

寄送方签发 `rpcId`（实践中 UUID 即可，递增非零即可）；`transport_error` 把载体层异常折进
`result.ok=false` 分支，兜底码恒 `gateway/internal`。

## 3. unary 会话服务端点（`web/api.py`，`session/*`）

端点命名与上游 typert 一致：`<namespace>/<method>`（gateway `endpointOf` = `${namespace}/${method}`，
client `connection/src/client/rpc.ts` 按 `/` 切两段）；`web/args.canonical_endpoint` 兼容历史
点式 `namespace.method` 折成斜杠式。路由表（`WebApi.ROUTES`），全部满足 §1 unary 载体约定：

| 端点 | 轮廓 | 典型业务错误码 |
|---|---|---|
| `session/list` | 会话清单（附 running 位） | —（gateway/bad-request 守卫） |
| `session/search` | 按查询过滤会话 | gateway/bad-request |
| `session/create` | 新建会话（`cwd` 或 `workspaceId` 二选一、`sessionId` 可注入幂等、`agentPreset` 可选） | gateway/bad-request / workspace/not-found / agent-preset/conflict / session/conflict / gateway/internal |
| `session/selectModel` | 设置模型 + `reasoningEffort` | gateway/bad-request / session/not-found / session/model-unavailable |
| `session/modelCatalog` | 模型目录 | — |
| `session/canOpenWorkspacePath` | 工作区路径可达性检查 | — |
| `session/openWorkspacePath` | 打开工作区 | gateway/bad-request |
| `session/rename` | 改标题 | gateway/bad-request / session/not-found |
| `session/fork` | 分支会话（无 fork 场景 → session/fork-unavailable） | gateway/bad-request / session/not-found / session/attachment-invalid / session/fork-unavailable |
| `session/prompt` | 投递 prompt（`mode` queue/steer、content 逐块 text/image、时间戳校验） | gateway/bad-request / session/not-found / session/invalid-time-zone / session/attachment-invalid / session/agent-busy |
| `session/attachment` | 附件受理（media + variantId） | session/not-found / session/attachment-invalid / gateway/internal |
| `session/updateQueue` | 队列编辑（edit/remove/steer 三类） | gateway/bad-request / session/attachment-invalid / session/queue-item-not-found / session/steer-unavailable |
| `session/cancel` | 取消当前回合 | session/not-found / session/agent-busy |
| `session/page` | 分页历史（throughSeq/beforeSeq/maxMessages） | gateway/bad-request / session/not-found / gateway/internal |

> 各端点 `args` 先在路由层经 `web/args.py` 做**统一 `{args}` 边界校验**（同 gateway
> `assertExactArguments`/`decode`）：字段集合精确匹配——缺 required / 多 unexpected →
> `gateway/arguments-invalid`（消息 `args fields do not match the descriptor: missing "x"; unexpected "y"`）；
> 顶层字段 JSON 类型错 → `gateway/input-invalid`（`wire field "x" failed boundary validation`，
> details 带 `field`）。枚举/范围/非空/跨字段语义仍留在 handler 以业务码表达，故上表
> `gateway/bad-request` 等仍涵盖业务语义错误。逐字段形状与错误 `details` 以 `api.py` 对应
> handler docstring 为准，教程见 07 章 §7.5.2；子代理所属会话被访问时统一折
> `session/agent-busy`（上游 `apiSessionSubagentOwnershipError`）。

### 3.1 结果里的二进制附件（`web/attachments.py`）

结果值里的 `bytes` 叶子无法进 JSON，按上游 gateway `encodeRpcResult`（投影）+ Connection
`fullResponse`（分帧）拆成**二进制附件**：该字节在 JSON 里写成 `null` 占位，附件在顶层
`attachments` 描述，载体改用 `multipart/form-data`。

- 触发面：结果含 `bytes`/`bytearray`/`memoryview` 的 unary 端点（`workspaceFiles/readBytes`
  的 `data`）。失败分支与纯 JSON 结果仍是 `200` + `application/json`。
- 分帧：`metadata` 文本 part 装 `{type, rpcId, result:{ok:true, value}, attachments}`，
  第 `i` 个二进制 part 名 `bytes-<i>`、`Content-Type: application/octet-stream`，出体流式。
- 附件描述符 `{"path": ["data"], "codec": "bytes", "part": "bytes-0"}`；`path` 段为对象键
  字符串或数组下标整数，按发现顺序编号。
- 客户端（`webui/src/wire/rpc.ts`）按 `path` 把 part 写回 `Uint8Array`：part 名重复、
  非 `bytes` codec、路径走不通、占位不是 `null`、有 part 未被声明都拒（`TypeError`）。
- 结果值有循环引用 → 200 + `gateway/internal`（`gateway: circular RPC result`）。

## 4. Remote 流（`WS /api/remote.mux`，`web/mux.py` + `web/stream_protocol.py` + `web/uplink.py`）

单一路径承载**全部** Remote 流；每条 open 后的 value 序列经 `item` 帧吐出。客户端 `streamId`
自编号（非空字符串即可）。

**浏览器 → 宿主**（文本帧四型；每型字段集合精确匹配，多一个键或缺一个键都拒并关 WS 1008）：

```json
{"type": "open",   "streamId": "<id>", "endpoint": "<endpoint>", "payload": {...}}
{"type": "item",   "streamId": "<id>", "value": "<任意值，可省>"}
{"type": "end",    "streamId": "<id>"}
{"type": "cancel", "streamId": "<id>"}
```

**宿主 → 浏览器**（文本帧三型，同样字段集合精确匹配）：

```json
{"type": "item",  "streamId": "<id>", "value": "<任意值，可省>"}
{"type": "end",   "streamId": "<id>"}
{"type": "error", "streamId": "<id>", "error": {"code": "...", "message": "...", "details": {...}}}
```

- 每条 open 立即按 `endpoint` 分发（§4.1/4.2/4.3）；`open` 内抛错 → 该流先发 `error` 帧再 `end`，
  **不关 WS**（单流失败与其它流隔离）；流中途失败同理；`error` 帧本身发送失败 → close 1011。
- 每条 open 同时建一条**有界上行 inbox**（`web/uplink.py`）：单消费者，缓冲按整帧 UTF-8 字节
  记账，上限 262144 字节（`create_app(stream_inbox_bytes=...)` 可调，同上游 `streamInboxBytes`）。
  `end` 是半关（已缓冲的 `item` 仍可读尽）；`end` 之后再收 `item` → 该流以 Remote failure 中止，
  终态 `error` 帧 code `gateway/protocol`；缓冲超限 → `gateway/uplink-overflow`（消息带字节上限，
  `details` 带 `{endpoint}`）。两者都只杀本流、不关 WS。
  紧跟 open 的 `item` 会排在该流 inbox 里等消费者，不因「先建流后派发」而丢。
  `$events` 由网关自有（不走上行），open 即释放；其余端点的上行活到该流结束/取消/断连时释放。
- 上行项由方法侧逐项解码（`web/uplink.py` 的 `UplinkItems`，上游 `GatewayInvocation.uplink()`
  的 `UplinkDecoder`）：过该 endpoint 声明的 codec，未声明则走无损 JSON 校验（`NaN`/Infinity、
  `-0`、循环引用、Date、函数、symbol、不可枚举属性等一律拒），解码结果本身也再校验一次。
  被拒项 → 该流以 `gateway/input-invalid` 中止，终态 `error` 帧消息
  `typert gateway: <endpoint>: wire field "uplink" failed boundary validation`，
  `details` 为 `{endpoint, field: "uplink"}`。每次调用 `uplink()` 只能取一次（第二次抛错）；
  rc.1 出厂的全部 Remote 方法都是 `In = never`，故 `GatewayStreams.uplink_codecs()` 出厂为空表
  ——声明点即 typert 生成的描述符 `uplink.codec`（`packages/typert/protocol/src/types.ts:355-357`）。
- `endpoint` 全集（`GatewayStreams.stream_kinds`，九条）：`$events`、`session/follow`、
  `session/control`、`terminal/retain`、`terminal/follow`、`workspace/follow`、
  `workspaceFiles/changes`、`job/list`、`job/follow`；未知 endpoint → `error` 帧
  `gateway/invocation-unavailable`
  （消息 `typert gateway: <endpoint>: no active Remote method exports this endpoint`）。
- 每条 open 的命名空间未挂载（`ctx` 无 `terminalController`/`workspaceController`/
  `workspaceFiles`）→ 该流 `error` 帧 `gateway/invocation-unavailable`；args 缺字段/类型不符
  → `gateway/arguments-invalid`。
- `workspaceFiles/changes`（`{args:{workspaceFileScopeId, path}}`）：先以 `ctx.fs.watch`
  建立目标监听（目录须在工作区内），成功给 `{kind:'ready'}`，随后命中目标的失效重 stat
  产出 `{kind:'change', change:{absolutePath, version}}`（目标已删除则 `{absolutePath, absent:true}`）；
  watch 不可用 → `workspace-file/watch-unsupported`，目录越界 → `workspace-file/outside-workspace`。
  同 ns 的 unary `readBytes` 取 `options:{range?:{offset,length}, baseFile?}`，服务值为原生字节、
  wire 层走 §3.1 的二进制附件。

### 4.1 `session/follow`（历史跟随流）

- open payload：`{args:{address:{kind:'session', sessionId}, maxMessages?}}`
- 首帧 `snapshot`：

```json
{"type": "snapshot",
 "header": {"version": 2, "id": "...", "createdAt": ..., "isSeeded": false,
            "cwd": "...?", "parentSession": "...?", "origin": "...?",
            "delegationDepth": ...?, "agentPreset": "...?"},
 "cursor": <seq>,
 "records": [{"type": "event", "event": <事件信封>}...],
 "hasMore": <bool>,
 "projections": {"asOfSeq": <seq>,
                 "values": {"sessionStats": {"turns","steps","llmMs","toolMs","ttftMs","ttftSteps","decodeMs","decodeTokens"},
                            "tokenUsage": {"uncachedInputTokens","outputTokens","cacheReadTokens","cacheWriteTokens"}}}}
```

  header 是平铺 `SessionWireHeader`（`api.py _wire_header`，上游 history.ts wireHeader 用
  `isSeeded` boolean）；records 严格 `{type:'event', event}` 包装。事件信封统一形态
  `{type, seq, time, data}`（`seq` 0 基严格递增：`seq == 追加前日志长度`，同上游 EventLog
  `seq: this.log.length` 后 push；首事件 seq=0）。`maxMessages` 溢出时只取尾段并置
  `hasMore=true`；`cursor` = 最后一条已提交事件 seq（0 基 inclusive，空日志 -1）。
  `projections.values` 是真实视图（sessionStats 八键加 tokenUsage 四键，等价于现场折叠，
  见 `telemetry.projection_values` 与 architecture 映射表；`contextPressure` 不承载）。
- 续帧：`{"type":"event", "event": <事件信封>}`，活体帧从 `snapshot.cursor + 1` 起、
  `event.seq` 严格递增；客户端按 seq 去重拼接（webui `TrajectoryBuffer`）。
- 错误：`gateway/arguments-invalid` / `session/not-found`（未知会话）。
- 上游 wire **无 `since` 字段**（见 §4.4）：`follow`/`control` 每次（重）连都重开流并重投完整
  `snapshot`/`baseline`，客户端按 seq 去重即无缺口；mini 相同。follow 活体帧的 mini 载体是
  ≤50ms 短轮询批量提取，进程内日志现成可读，帧形状与顺序同上游。

### 4.2 `session/control`（宿主级 live control）

- open payload：恰 `{args:{}}`。
- 首帧 `baseline`：

```json
{"type": "baseline",
 "value": {"queues": {"<sessionId>": [<queue item>...]},
           "jobs":   {"<sessionId>": [<job row>...]},
           "projections": {"<sessionId>": {"asOfSeq": <seq>,
                                           "values": {"sessionStats": {...8 键...},
                                                      "tokenUsage": {...4 键...}}}}}}
```

  control baseline 的 queues/jobs/projections 覆盖**全部 live 会话**（空会话也各放一条
  空块）；projections.values 与 follow 同源（`telemetry.projection_values` 现场折叠，
  真实 sessionStats/tokenUsage 视图，见 4.1）。

- 续帧（替换语义）：`{"type":"queue", "sessionId": "...", "items":[...]}`（inbox 拼接时）、
  `{"type":"jobs", "sessionId": "...", "jobs":[...]}`（作业变更时）、会话 dispose →
  `{"type":"queue", "sessionId": "...", "items":[]}`。
- queue item：`{id, placement: "queued"|"steering"|"context", message: {id, content:[...]}}`
  （user source 的 item 另带可选 `rpcId`，对应上游 promptRpcId）；
  job row 键：`id / kind / label / status / startedAt / detail / finishedAt`（存在才带）。

### 4.3 `$events`（远程事件流，`web/events.py`）

- open payload 必须**恰** `{args:{}}`（非空 args → 该流 `error` 帧 `gateway/arguments-invalid`）。
- 首帧 `ready`：`{"type":"ready", "clientId": "<uuid>", "host": {"home": "<宿主 home>"}}`
  （`host.home` 仅用于前端缩写本机路径显示）。
- 下游帧三种：

```json
{"type": "emit",      "event": "...", "args": [...]}
{"type": "waterfall", "event": "...", "eventId": "...", "agentId": "...", "request": {...}}
{"type": "cancel",    "eventId": "..."}
```

- emit 转发源（api-session/* 族）：`session/created → api-session/added`（初始 list row）、
  `session/disposed → api-session/removed`、`agent/status → api-session/status`（running 位）、
  `agent/error → api-session/error`、user `user/message → api-session/activity`。
- waterfall：审批问询 `event="approval/request"`、`agentId=<会话 id>`、`request={toolName}`；
  由首个客户端的 `$events/result` 结算（§5）。

### 4.4 重连语义与心跳（三流统一约定）

同上游 README：「reconnection reopens the `$events` stream；one-way notifications
are **not** replayed after reconnect」「always return a complete opening snapshot followed
by deltas」。

- **`session/follow` / `session/control`**：每次 open（含重连）都重投完整 `snapshot` / `baseline`
  快照帧，随后以续帧增量推进。客户端只需「重开即重启，按 seq 与替换帧语义收敛」，无需游标
  参数（wire 无 `since`）。
- **`$events`**：新代次先发 `ready`（新 `clientId`）；对旧代次已转发过的单向 emit **不重放**
  （无 since 恢复游标，不算缺口）；**挂起的 waterfall 保留 `eventId` 跨代次重投**（新客户端
  open 即收到，首个 `$events/result` 结算，幂等 no-op）。
- **心跳**：transport 级（不归 `web/mux.py`，前端无感）：每连接每 2s 一次协议 Ping，
  连续 2 周期无 Pong 判死（`ws_ping_timeout=4`，约等于上游 `MAX_MISSED_HEARTBEATS=2`，
  即 stream-server.ts 的 terminate）；由 uvicorn 的 websockets 实现透传
  （`web/launcher.py` 的 `uvicorn_options`，间隔取上游 `websocketHeartbeatIntervalMs`
  @default 2000）。

### 4.5 `terminal/retain` / `terminal/follow`（终端持有与恢复流）

`terminal/retain` —— 一条物理 Remote 流为一个终端身份**持窗**（不激活 Agent、不取输入控制）。

- open payload：`{args:{sessionId, id}}`（取 sessionId，不需要 `agentId`）。
- 唯一一帧确认 `{"type":"retained"}`，此后**不再出帧**，流保持到本流被 `cancel`/断连，
  或该身份被 `terminal/close`（含会话 owner 拆解、控制器 dispose）关闭。进程自己退出
  **不**结束持窗（上游 `retention.ts` 的 `lifetime` 闩只随身份关闭而落）。
- 载体差异（已登记）：上游 holders 集合的唯一作用是抑制「无人值守空闲回收」调度，
  mini 无该调度器，故持有不改变清理时机。
- 错误：身份缺失/已关闭/owner 正在清理 → `terminal/unavailable`；命名空间未挂载 →
  `gateway/invocation-unavailable`（同 §4 头）。

`terminal/follow` —— 附加到一个终端，**不把进程生命周期绑到传输**。

- open payload：`{args:{agentId, id, attachmentId}}`；`attachmentId` 须合 `^[\w-]{1,128}$`。
  附加即独占输入控制（较晚附加接管输入，旧的降为只读），分离不杀进程。
- 首帧 `snapshot`（完整有界屏幕，含滚出历史；行尾空白裁剪、尾随空行去除）：

```json
{"type": "snapshot", "sequence": <int>, "screen": "<整屏文本>", "info": {<WebTerminalInfo>}}
```

  `info` = `{id, title, shell:{path,args,name}, cwd, cols, rows, state, exitCode, error?, controllerId?}`，
  `state` ∈ `running|exited|failed`；`exited` 带 `exitCode`，`failed` 带 `error`；
  `controllerId` 只在当前有输入控制者时出现（附加期间必有）。`cwd` 是创建时工作目录，
  shell 内 `cd` 不回写该字段。
- 续帧（有序，三型与上游 `TerminalFrame` 一一对应）：

```json
{"type": "output", "sequence": <int>, "data": "<本段输出>"}
{"type": "state",  "info": {<WebTerminalInfo>}}
```

- 终态：进程退出/失败先投一帧 `state`（`state` 转 `exited`/`failed`），**队列里剩余帧投完后
  流才 `end`**（上游 `TerminalFollower.finish`）。`state` 帧也会在 `resize`（cols/rows）、
  `rename`（title）、输入控制权易手时出现。
- 慢消费者：单 follower 排队字节超 `maxBufferedBytes`（缺省 2 MiB）→ 该流以
  `gateway/internal` 中止（消息 `Terminal output consumer exceeded its buffer; reconnect to
  recover the current screen`）；重连开新代次从 `snapshot` 恢复当前屏幕。
- 错误：未知 `agentId` → `session/not-found`；身份缺失/已关闭 → `terminal/unavailable`；
  `attachmentId` 非法 → `gateway/internal`（上游同为普通 `Error`，折算同一码）。

### 4.6 `workspace/follow`（工作区投影流）

- open payload 恰 `{args:{}}`（上游 `follow(signal)` 无入参）。
- 首帧 `baseline`（重连必重发的完整基线）：

```json
{"type": "baseline",
 "value": {"items": [{<WorkspaceView>}, ...],
           "archivedSessionIds": ["<sessionId>", ...],
           "pinnedSessionIds": ["<sessionId>", ...]}}
```

  `WorkspaceView` = `{workspaceId, path, title, sessionIds, createdAt, updatedAt}`（`sessionIds`
  是该工作区名下按人工顺序记的会话）；`pinnedSessionIds` 是**注册表全局**钉选集（最近钉选在前）。
- 续帧五型（插入/替换语义，同上游 `WorkspaceFollowIncrement`）：

```json
{"type": "upsert",   "workspace": {<WorkspaceView>}}
{"type": "remove",   "workspaceId": "..."}
{"type": "order",    "workspaceIds": ["..."]}
{"type": "archived", "archivedSessionIds": ["..."]}
{"type": "pinned",   "pinnedSessionIds": ["..."]}
```

- 一次变更的出帧顺序固定：先按新顺序给**新身份**发 `upsert`（已知名不再 upsert），顺序真变才发
  `order`，再 `archived`，最后 `pinned`（上游 `feed.ts` 的 `changed`）。`archived`/`pinned`
  都是全量替换（不是增量 diff）。
- 断连期间的增量不重放：重连即新代次重发 `baseline`，消费者按「重开即全量」收敛（§4.4）。

### 4.7 `job/list` / `job/follow`（后台作业 roster 与输出流）

`job` namespace（上游 `packages/api/job-controller`，web-app 默认挂载）：

- `job/list`（`{args:{sessionId}}`）：一次会话可见作业集的**整集替换**帧流。首帧即刻，
  此后每次触及可见作业的 lifecycle 提交（registered/progress/stopping/settled/removed）
  合并 100ms 后发下一整集；纯 output 追加不刷新 roster：

```json
{"type": "rows", "jobs": [{<JobView>}, ...]}
```

  `JobView` = `{id, kind, label, owner?, outputLimitBytes?, status, progress?, detail?,
  startedAt, finishedAt?, output:{total, earliest, spillPaths?}}`（`status` ∈
  `running|stopping|completed|killed|failed`）。
- `job/follow`（`{args:{sessionId?, jobId, from?}}`）：一作业 retained 输出的观察流。
  `from` 缺省 = 最老保留字节；`from` 显式提供时须为非负整数（否则 `gateway/arguments-invalid`）。
  帧序：`opened{job, from}` → `output{chunks, next, lossy?}`* → `status{job}`（终态投影后流正常
  关闭）：

```json
{"type": "opened", "job": {<JobView>}, "from": 0}
{"type": "output", "chunks": [{at, text, channel?, gapBefore?}], "next": 1024, "lossy"?: true}
{"type": "status", "job": {<JobView>}}
```

  output 帧按 64 KiB 软字节预算切帧（单块超预算整块带走，lossy 只随首帧）；`next` 是下一
  帧的绝对字节偏移，重连时作为 `from` 传入恢复。已结算且排空（`cursor >= output.total`）发
  `status` 后正常关闭；owner teardown 移除作业则以移除时的终态投影发 `status` 并关闭。
- `job/kill`（unary `POST /api/job/kill`，`{args:{sessionId, jobId}}`）：请求取消一个作业，
  返回 `{"outcome": "requested" | "already-finished"}`；未知/外会话作业 →
  `job/not-found`（details `{sessionId, jobId}`）。
- `job` namespace 未挂载（`ctx` 无 `jobs`）→ `gateway/invocation-unavailable`。

### 4.8 `agentPresets`（声明式组合 roster 的 Remote 面）

`agentPresets` namespace（上游 `packages/preset/agent-preset-registry`，web-app 默认挂载）。
三个 unary `POST /api/<endpoint>`：

- `agentPresets.list`（`{args:{}}`）→ `AgentPresetRoster`：
  `{presets: [{id, isDefault, name?, description?, broken?}], modeSelectionEnabled}`；
  `modeSelectionEnabled` mini 恒 `true`（无 settings 配置面）。未知端点集合不固定——roster
  是当前部署的声明列表。
- `agentPresets.read`（`{args:{agentPreset}}`）→ `AgentPresetDocument`：
  `{agentPreset, content, name?, description?}`；`content` 是声明行/组合的 entry-list YAML
  （mini JSON 载体 → `rows: []`）。未知 id → `agent-preset/not-found`（details
  `{agentPreset, available}`）。
- `agentPresets.select`（`{args:{sessionId, agentPreset}}`）→ 提交的 preset id（字符串）。
  **首回合前锁**：`turnBoundary` 投影判定 `openTurnStartSeq !== null || lastTurn > 0` →
  `agent-preset/locked`（details `{sessionId, agentPreset}`，消息 "This session has already
  started"）；合法则解析（未知 → `agent-preset/not-found`）并 append durable
  `agent-preset/selected {agentPreset}` 落盘后返回 id。
- 错误码闭集新增 `agent-preset/locked`（`agent-preset/not-found`/`agent-preset/invalid` 既有）；
  未挂载 roster → `gateway/invocation-unavailable`。
- 会话投影：`agentPreset` 单元（stateVersion 1，init = `session.meta.agentPreset ?? null`，
  fold `agent-preset/selected` 整值替换，有 wire）——`session/projections` 与 control
  projection 帧暴露该单元。

## 5. `$events/result`（HTTP unary 特判端点，`web/server.py` + `web/events.py`）
`POST /api/$events/result`，body 为 `client-request` 全形，`payload` 恰：

```json
{"args": {"clientId": "...", "eventId": "...", "outcome": {"kind": "result" | "next" | "rejected", ...}}}
```

outcome 三型：

- `{"kind":"next"}` —— 该客户端向上游让位；
- `{"kind":"result", "value": <任意无损 JSON，可省>}` —— 投出结果；
- `{"kind":"rejected", "error": {"name", "message", "code"?, "details"?}}` —— 监听器抛错。

应答恒 200 `server-response`：合法且结算成功 → `{"ok":true}`；词法非法 / 未知 `clientId` →
`{ok:false, error:{code:"gateway/bad-request"|"gateway/internal", ...}}`。

结算语义（同 `receiveRemoteEventResult`）：`result` → 终局（首个投出者唯一放行）；`next` →
该客户端让位、全部客户端耗尽 → `'next'`；`rejected` → `'rejected'`；**已结算/被取代的 eventId
幂等 no-op**；注册表 `dispose()` 时全量 pending 折 `'cancelled'`。

## 6. 审批桥（`tools/ask` ↔ `approval/request` waterfall，`web/approvals.py`）

接线（教学简化）：工具管线 `tools/ask` 闸门 → 落审计事件 `approval/asked` → 以
`approval/request` waterfall 投递给所有 `$events` 客户端 → 浏览器经 `$events/result` 结算 →
落 `approval/decided` → 返回 bool（`allowed-once` 唯一放行）。

outcome 归一（`APPROVAL_OUTCOMES = {allowed-once, rejected, cancelled, unavailable}`）：

| 汇合 | 结果 |
|---|---|
| `result` 且 value ∈ APPROVAL_OUTCOMES | 原样 |
| `result` 但值非法 | `unavailable`（fail-closed，不放行） |
| `rejected` | `unavailable`（fail-closed） |
| `next` | 委托 `nxt()`（无其它 answerer 时终值） |
| `cancelled` | `cancelled` |

前端应答 value 取 `{"kind":"allowed-once"|"rejected", "sessionId"?, "approvalId"?}`。

## 7. SPA 静态承载（`web/frontend.py`）

- `DIST_ROOT` 默认 `web/static/`（教学参照），经 **`MINIHARNESS_WEBUI_DIST`** env 可指向产品化
  前端构建产物（`webui/dist/`）；`serve_static` 约定不变。
- 只服务 `GET`/`HEAD`，其它方法 405；非 `/api/` 前缀。
- 约定（同 `packages/host/frontend-static`）：目录遍历出根 → 403；未命中 → 回退 `index`
  200（SPA 客户端路由）；MIME 按扩展（未知 → `application/octet-stream`）；index taps 恒
  identity（不注入，mini 无 boot-manifest）。

## 8. 会话导出（`GET /api/session.export`，`web/downloads.py`）

- query：`sessionId`（必须）、`includeDescendants`（`true`/`false`/缺省，其余 400）。
- 状态码链：200 / 400 / 404（缺根）/ 501（后端不支持）/ 500；响应头
  `Content-Disposition: attachment; filename="dsh-session-<safe>.zip"`。
- zip 条目序：根制品以**逐字原始文件名**入档（`session.v4.jsonl[.zstd]`，generation 版本化
  文件名，压缩 0/none 时为 `session.jsonl`）→ 后代 `subagents/<safe-id>/<同名制品>`
  （parentSession BFS + seen-set 去重）→ 媒体 `media/<attachmentId>.<ext>`；压缩等级 0-9。
- 错误正文统一私有外壳（`session log export failed to prepare the stored artifact`），不泄路径细节。

## 9. 错误语义速查

1. 业务错误恒 200 + `server-response` + `result.ok=false`（unary 与 `$events/result` 同则）。
2. 载体层 404/415/400/500 只在信封/HTTP 层面，不代表业务状态。
3. 流内错误 = `error` 帧（单流隔离），不关 WS；`close` 码只留给协议/形状/危险级错误。
4. 审批 fail-closed：非 APPROVAL_OUTCOMES 合法值一律 `unavailable`，绝不误放行。
5. 事件 `seq` 严格递增、0 基（`seq == 追加前日志长度`）；未知事件类型持久化读路径 fail-closed（除非带 `ignorable: true` 豁免放行），不做静默吞掉。

## 10. 与前端实现的映射

`webui/src/wire/` 约定客户端层对应本章节：

| 文件 | 对应 |
|---|---|
| `rpc.ts` | §1 unary 载体 + §2 信封 + §3.1 multipart 重组（`parseBinaryResponse`）+ §5 `$events/result`（`RpcFailure` 折叠 transport_error） |
| `mux.ts` | §4 客户端 open/item/end/cancel 帧（`StreamHandle.send`/`endUplink`/`close`/`next`）+ item/end/error 消费（流队列 + waiter；断连即 `failAll` 终结本连接全部流） |
| `json-value.ts` | §4 上行项无损 JSON 校验（`isRemoteUplinkItem`，同上游 typert `isRemoteUplinkItem`） |
| `events.ts` | §4.3 `$events` ready/emit/waterfall/cancel + §5 结算（settled 集合 fail-closed） |
| `follow.ts` | §4.1 snapshot/event 帧 + seq 去重（`TrajectoryBuffer`） |
| `control.ts` | §4.2 的 baseline/projection 帧——**当前只建模了 rc.1 退役前的 `queue`/`jobs` 帧**（`applyControlFrame` 对 baseline 是 no-op 标记），宿主已不发这两型，队列/作业面板恒空（换源待立项） |
| `types.ts` | §2 信封 + §3.1 附件描述符 + 事件/消息/会话类型（镜像 core 模型） |

测试：`webui/tests/wire.test.ts` + `webui/tests/wire-binary.test.ts`（vitest，mock fetch/WS；
后者走 node 环境解析 multipart）+ `tests/test_web_*.py`（后端约定全组）；
后端静态承载新增 `tests/test_web_frontend.py` `test_webui_dist_build`（Vite 形态 dist）。