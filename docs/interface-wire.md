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
agent-preset/not-found    agent-preset/invalid      session/agent-busy
session/attachment-invalid session/queue-item-not-found session/steer-unavailable
session/title-invalid     session/fork-unavailable  subagent/not-found
subagent/catalog-diagnostic subagent/unauthorized
workspace-file/not-found   workspace-file/not-directory workspace-file/not-regular-file
workspace-file/not-text    workspace-file/outside-workspace workspace-file/too-large
workspace-file/watch-unsupported
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
  终态 `error` 帧 code `gateway/protocol`；缓冲超限 → `gateway/uplink-overflow`（消息带
  `limit` 与 `buffered` 字节数，`details` 带 `{endpoint}`）。两者都只杀本流、不关 WS。
  紧跟 open 的 `item` 会排在该流 inbox 里等消费者，不因「先建流后派发」而丢。
  `$events` 由网关自有（不走上行），open 即释放；`session/follow`、`session/control`、
  `workspaceFiles/changes` 在流结束/取消/断连时释放。
- 关键 `endpoint`（`GatewayStreams.stream_kinds`）：`$events`、`session/follow`、`session/control`；
   未知 endpoint → `error` 帧 `gateway/internal`。
- `workspaceFiles/changes`（`{args:{workspaceFileScopeId, path}}`）：先以 `ctx.fs.watch`
  建立目标监听（目录须在工作区内），成功给 `{kind:'ready'}`，随后命中目标的失效重 stat
  产出 `{kind:'change', change:{absolutePath, version}}`（目标已删除则 `{absolutePath, absent:true}`）；
  watch 不可用 → `workspace-file/watch-unsupported`，目录越界 → `workspace-file/outside-workspace`。
  同 ns 的 unary `readBytes` 取 `options:{range?:{offset,length}, baseFile?}`，服务值为原生字节、
  wire 层折 base64。

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
| `rpc.ts` | §1 unary 载体 + §2 信封 + §5 `$events/result`（`RpcFailure` 折叠 transport_error） |
| `mux.ts` | §4 客户端 open/cancel 帧 + item/end/error 消费（流队列 + waiter） |
| `events.ts` | §4.3 `$events` ready/emit/waterfall/cancel + §5 结算（settled 集合 fail-closed） |
| `follow.ts` | §4.1 snapshot/event 帧 + seq 去重（`TrajectoryBuffer`） |
| `control.ts` | §4.2 baseline/queue/jobs 替换帧（`applyControlFrame`） |
| `types.ts` | §2 信封 + 事件/消息/会话类型（镜像 core 模型） |

测试：`webui/tests/wire.test.ts`（vitest，mock fetch/WS）+ `tests/test_web_*.py`（后端约定全组）；
后端静态承载新增 `tests/test_web_frontend.py` `test_webui_dist_build`（Vite 形态 dist）。