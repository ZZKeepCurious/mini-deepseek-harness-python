# web 接口契约参考（Wire Reference）

> 定位：这是 `mini-deepseek-harness` 前后端的**唯一耦合面**——`miniharness/web/` 传输层对
> 前端发布的所有接口契约（信封、帧、错误语义）。产品化前端（仓库顶层 `webui/`）与任何第三方
> 客户端只依赖本契约，禁止 import Python 内部。正文与当前实现逐条对应；契约本质上对齐上游
> `dsh-v0.1.2-alpha.1`（`packages/client/connection` + `packages/api/gateway` +
> `packages/api/session-controller` + `packages/api/remotes` + `host/frontend-static`），
> mini 侧保留简化的差异项在 `status/mini-harness/verified-diffs.md` §3.4 登记。
>
> 载体实现：`web/server.py`（FastAPI）；语法层：`web/envelope.py` + `web/stream_protocol.py`；
> 域实现：`web/api.py` + `web/streams.py` + `web/events.py` + `web/approvals.py` +
> `web/downloads.py` + `web/frontend.py`。教程对照：`docs/chapters/07-external-entry-points.md`
> §7.5（按实现顺序讲解）；本文件是**契约的权威速查**，不讲实现细节。

## 1. 通道总览与载体状态码

| 通道 | 载体 | 覆盖范围 | 载体状态码 |
|---|---|---|---|
| unary RPC | `POST /api/<endpoint>` | `session.*` 14 个 unary 端点 + `$events/result` 特判 | 404 / 415 / 400 / 500 / 200 |
| Remote 流 | `WS /api/remote.mux` | `$events` + `session.follow` + `session.control` 全部长期流 | WS 关闭码 1003 / 1008 / 1011 |
| 会话导出 | `GET /api/session.export` | 会话日志 zip 下载 | 200 / 400 / 404 / 501 / 500 |
| SPA 静态 | `GET`/`HEAD /{path}`（非 `/api/`） | `webui/dist/` 或 `web/static/` 产物承载 | 403 / 200 / 404 / 405 |

### unary 载体状态码语义（`web/server.py`）

- `404`：非 POST、路径不在 `/api/` 下、或 `method` 不在路由表（`session.*` 之外）。
- `415`：`content-type` 非 `application/json`（跨站写围栏，上游同款安全机制；无 CORS 头）。
- `400`：body 非 JSON（含空体）；`GET/HEAD /api/session.export` 参数缺失/非法同样归 400 文本。
- `500`：信封合法但实现崩溃（纯文本 `handler failure: <error>`）。
- `200`：**一切业务结果**——业务错误恒 200 + `server-response` 且 `result.ok=false`，不借 HTTP 状态码表达业务错误。

路径约束：endpoint 段匹配 `[A-Za-z0-9_$.-]+`（`$` 是真实契约：`$events`、`$events/result` 为网关
内端点）；body `client-request` 的 `payload` 必须**恰**为 `{args:{...}}` 单字段 plain object
（多余键 / 缺 args / 非对象一律 `bad-request`，对齐 gateway `remoteRequest`）。

### WS 关闭码（`web/mux.py`）

- `1003`：收到二进制帧（协议错误）。
- `1008`：文本非 JSON / 帧形状非法 / 重复 `open` 同一 `streamId`。
- `1011`：某流 `error` 帧自身发送失败。
- 心跳：每条连接每 30s 一次 transport 级协议 Ping（`web/launcher.py` uvicorn
  `ws_ping_interval=30`，`ws_ping_timeout=None` 不强制 Pong）——只保活中间代理、
  不杀僵死连接，对齐上游 `stream-server.ts` heartbeat（对齐交付，见 §4.4）。

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

**RpcError 的 `code` 是 23 码命名空间闭集**（alpha.2 起统一为 `<namespace>/<name>` 形式，
对齐 typert `RemoteErrorDetailsMap` 键集：基础设施 `gateway/*` + 各域 merge-extensible 注册，
`web/envelope.py` `RPC_ERROR_CODES`；R3 闭合路由层边界校验后新增 `gateway/input-invalid`）：

```
gateway/bad-request       gateway/cancelled         gateway/internal
gateway/arguments-invalid gateway/input-invalid     session/not-found
session/model-unavailable session/conflict          session/invalid-time-zone
session/workspace-attach-failed workspace/not-found agent-preset/conflict
agent-preset/not-found    agent-preset/invalid      session/agent-busy
session/attachment-invalid session/queue-item-not-found session/steer-unavailable
session/title-invalid     session/fork-unavailable  subagent/not-found
subagent/catalog-diagnostic subagent/unauthorized
```

寄送方签发 `rpcId`（实践中 UUID 即可，递增非零即可）；`transport_error` 把载体层异常折进
`result.ok=false` 分支，兜底码恒 `gateway/internal`。

## 3. unary 会话服务端点（`web/api.py`，`session.*`）

路由表（`WebApi.ROUTES`），全部满足 §1 unary 载体契约：

| 端点 | 轮廓 | 典型业务错误码 |
|---|---|---|
| `session.list` | 会话清单（附 running 位） | —（gateway/bad-request 守卫） |
| `session.search` | 按查询过滤会话 | gateway/bad-request |
| `session.create` | 新建会话（`cwd` 或 `workspaceId` 二选一、`sessionId` 可注入幂等、`agentPreset` 可选） | gateway/bad-request / workspace/not-found / agent-preset/conflict / session/conflict / gateway/internal |
| `session.selectModel` | 设置模型 + `reasoningEffort` | gateway/bad-request / session/not-found / session/model-unavailable |
| `session.modelCatalog` | 模型目录 | — |
| `session.canOpenWorkspacePath` | 工作区路径可达性检查 | — |
| `session.openWorkspacePath` | 打开工作区 | gateway/bad-request |
| `session.rename` | 改标题 | gateway/bad-request / session/not-found |
| `session.fork` | 分支会话（无 fork 场景 → session/fork-unavailable） | gateway/bad-request / session/not-found / session/attachment-invalid / session/fork-unavailable |
| `session.prompt` | 投递 prompt（`mode` queue/steer、content 逐块 text/image、时间戳校验） | gateway/bad-request / session/not-found / session/invalid-time-zone / session/attachment-invalid / session/agent-busy |
| `session.attachment` | 附件受理（media + variantId） | session/not-found / session/attachment-invalid / gateway/internal |
| `session.updateQueue` | 队列编辑（edit/remove/steer 三类） | gateway/bad-request / session/attachment-invalid / session/queue-item-not-found / session/steer-unavailable |
| `session.cancel` | 取消当前回合 | session/not-found / session/agent-busy |
| `session.page` | 分页历史（throughSeq/beforeSeq/maxMessages） | gateway/bad-request / session/not-found / gateway/internal |

> 各端点 `args` 先在路由层经 `web/args.py` 做**统一 `{args}` 边界校验**（对齐 gateway
> `assertExactArguments`/`decode`）：字段集合精确匹配——缺 required / 多 unexpected →
> `gateway/arguments-invalid`（消息 `args fields do not match the descriptor: missing "x"; unexpected "y"`）；
> 顶层字段 JSON 类型错 → `gateway/input-invalid`（`wire field "x" failed boundary validation`，
> details 带 `field`）。枚举/范围/非空/跨字段语义仍留在 handler 以业务码表达，故上表
> `gateway/bad-request` 等仍涵盖业务语义错误。逐字段形状与错误 `details` 以 `api.py` 对应
> handler docstring 为准，教程见 07 章 §7.5.2；子代理所属会话被访问时统一折
> `session/agent-busy`（上游 `apiSessionSubagentOwnershipError`）。

## 4. Remote 流（`WS /api/remote.mux`，`web/mux.py` + `web/stream_protocol.py`）

单一路径承载**全部** Remote 流；每条 open 后的 value 序列经 `item` 帧吐出。客户端 `streamId`
自编号（非空字符串即可）。

**浏览器 → 宿主**（文本帧两型；未知键被 schemastery 投影丢弃，判别字段缺失/类型错才拒）:

```json
{"type": "open",   "streamId": "<id>", "endpoint": "<endpoint>", "payload": {...}}
{"type": "cancel", "streamId": "<id>"}
```

**宿主 → 浏览器**（文本帧三型）:

```json
{"type": "item",  "streamId": "<id>", "value": "<任意值，可省>"}
{"type": "end",   "streamId": "<id>"}
{"type": "error", "streamId": "<id>", "error": {"code": "...", "message": "...", "details": {...}}}
```

- 每条 open 立即按 `endpoint` 分发（§4.1/4.2/4.3）；`open` 内抛错 → 该流先发 `error` 帧再 `end`，
  **不关 WS**（单流失败与其它流隔离）；流中途失败同理；`error` 帧本身发送失败 → close 1011。
- 关键 `endpoint`（`GatewayStreams.stream_kinds`）：`$events`、`session.follow`、`session.control`；
   未知 endpoint → `error` 帧 `gateway/internal`。

### 4.1 `session.follow`（历史跟随流）

- open payload：`{args:{address:{kind:'session', sessionId}, maxMessages?}}`
- 首帧 `snapshot`：

```json
{"type": "snapshot",
 "header": {"sessionId": "...", "cwd": "...?", "parentSessionId": "...?", "origin": "...?"},
 "cursor": <seq>,
 "records": [<事件信封>...],
 "hasMore": <bool>,
 "projections": {}}
```

  事件信封统一形态 `{type, seq, time, data}`（`seq` 0 基严格递增：`seq == 追加前
  日志长度`，对齐上游 EventLog `seq: this.log.length` 后 push；首事件 seq=0）。
  `maxMessages` 溢出时只取尾段并置 `hasMore=true`；`cursor` = 日志条数 = 下一条
  活体帧应达 `seq`。
- 续帧：`{"type":"event", "event": <事件信封>}`，首条活体帧 `event.seq == snapshot.cursor`，
  其后 `event.seq` 严格递增；客户端按 seq 去重拼接（webui `TrajectoryBuffer`）。
- 错误：`gateway/arguments-invalid` / `session/not-found`（未知会话）。
- 已核实（对齐交付，见 §4.4）：上游 wire **无 `since` 字段**——`follow`/`control` 每次
  (重)连 = 重开流重投完整 `snapshot`/`baseline`，客户端按 seq 去重即 gap-free；mini 同款，
  重连健壮性不欠账。follow 活体帧的 mini 载体 = ≤50ms 短轮询批量提取（进程内日志现成可读，
  帧形状/顺序与上游一致）。

### 4.2 `session.control`（宿主级 live control）

- open payload：恰 `{args:{}}`。
- 首帧 `baseline`：

```json
{"type": "baseline",
 "value": {"queues": {"<sessionId>": [<queue item>...]},
           "jobs":   {"<sessionId>": [<job row>...]},
           "projections": {}}}
```

- 续帧（替换语义）：`{"type":"queue", "sessionId": "...", "items":[...]}`（inbox 拼接时）、
  `{"type":"jobs", "sessionId": "...", "jobs":[...]}`（作业变更时）、会话 dispose →
  `{"type":"queue", "sessionId": "...", "items":[]}`。
- queue item：`{id, placement: "queued"|"steering"|"context", message: {id, content:[...]}}`；
  job row 键：`id / kind / label / status / startedAt / detail / finishedAt`（存在才带）。

### 4.3 `$events`（远程事件流，`web/events.py`）

- open payload 必须**恰** `{args:{}}`（非空 args → 该流 `error` 帧 `gateway/arguments-invalid`）。
- 首帧 `ready`：`{"type":"ready", "clientId": "<uuid>", "host": {"home": "<宿主 home>"}}`
  （`host.home` 仅用于前端缩写本机路径显示；不依赖已删除的 `host.describe`）。
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

结算语义（对齐 `receiveRemoteEventResult`）：`result` → 终局（首个投出者唯一放行）；`next` →
该客户端让位、全部客户端耗尽 → `'next'`；`rejected` → `'rejected'`；**已结算/被取代的 eventId
幂等 no-op**；注册表 `dispose()` 时全量 pending 折 `'cancelled'`。

### 4.4 重连语义与心跳（三流统一约定）

对齐上游 README：「reconnection reopens the `$events` stream；one-way notifications
are **not** replayed after reconnect」「always return a complete opening snapshot followed
by deltas」。

- **`session.follow` / `session.control`**：每次 open（含重连）重投完整 `snapshot` / `baseline`
  快照帧，随后续帧增量推进——客户端要做的只是「重开即重启，按 seq / 替换帧语义收敛」，无需
  游标参数（wire 无 `since`）。
- **`$events`**：新代次先 `ready`（新 `clientId`）；对旧代次已转发过的单向 emit **不重放**
  （无 since 恢复游标，非缺口）；**挂起的 waterfall 保留 `eventId` 跨代次重投**（新客户端
  open 即收到，首个 `$events/result` 结算，幂等 no-op）。
- **心跳**：transport 级（不归 `web/mux.py`，前端无感）：每连接每 30s 一次协议 Ping，
  `ws_ping_timeout=None` 不强制 Pong、不杀僵死连接（对齐上游 `stream-server.ts`——只保活、
  不要求 timely Pong）；由 uvicorn `websockets` 实现透传（`web/launcher.py` `uvicorn_options`）。

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
  前端构建产物（`webui/dist/`）；`serve_static` 契约不变。
- 只服务 `GET`/`HEAD`，其它方法 405；非 `/api/` 前缀。
- 契约（对齐 `packages/host/frontend-static`）：目录遍历出根 → 403；未命中 → 回退 `index`
  200（SPA 客户端路由）；MIME 按扩展（未知 → `application/octet-stream`）；index taps 恒
  identity（不注入，mini 无 boot-manifest）。

## 8. 会话导出（`GET /api/session.export`，`web/downloads.py`）

- query：`sessionId`（必须）、`includeDescendants`（`true`/`false`/缺省，其余 400）。
- 状态码链：200 / 400 / 404（缺根）/ 501（后端不支持）/ 500；响应头
  `Content-Disposition: attachment; filename="dsh-session-<safe>.zip"`。
- zip 条目序：根 `session.jsonl` → 后代 `subagents/<safe-id>/session.jsonl`（parentSession BFS
  + seen-set 去重）→ 媒体 `media/<attachmentId>.<ext>`；压缩等级 0-9。
- 错误正文统一私有外壳（`session log export failed to prepare the stored artifact`），不泄路径细节。

## 9. 错误语义速查

1. 业务错误恒 200 + `server-response` + `result.ok=false`（unary 与 `$events/result` 同则）。
2. 载体层 404/415/400/500 只在信封/HTTP 层面，不代表业务状态。
3. 流内错误 = `error` 帧（单流隔离），不关 WS；`close` 码只留给协议/形状/危险级错误。
4. 审批 fail-closed：非 APPROVAL_OUTCOMES 合法值一律 `unavailable`，绝不误放行。
5. 事件 `seq` 严格递增、0 基（`seq == 追加前日志长度`）；未知事件类型持久化读路径 fail-closed（除非带 `ignorable: true` 豁免放行），不做静默吞图。

## 10. 与前端实现的映射

`webui/src/wire/` 契约客户端层对应本章节：

| 文件 | 对应 |
|---|---|
| `rpc.ts` | §1 unary 载体 + §2 信封 + §5 `$events/result`（`RpcFailure` 折叠 transport_error） |
| `mux.ts` | §4 客户端 open/cancel 帧 + item/end/error 消费（流队列 + waiter） |
| `events.ts` | §4.3 `$events` ready/emit/waterfall/cancel + §5 结算（settled 集合 fail-closed） |
| `follow.ts` | §4.1 snapshot/event 帧 + seq 去重（`TrajectoryBuffer`） |
| `control.ts` | §4.2 baseline/queue/jobs 替换帧（`applyControlFrame`） |
| `types.ts` | §2 信封 + 事件/消息/会话类型（镜像 core 模型） |

测试：`webui/tests/wire.test.ts`（vitest，mock fetch/WS）+ `tests/test_web_*.py`（后端契约全组）；
后端静态承载新增 `tests/test_web_frontend.py` `test_webui_dist_build`（Vite 形态 dist）。