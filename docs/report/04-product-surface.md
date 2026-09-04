# 04 · 产品面全解读

<p class="lead">前三个子页回答"系统是什么、怎么运转"；本页回答"<b>用户/宿主看到的是什么、哪些设计塑造了使用体验</b>"。十个议题，每个都按固定四段式展开：<b>产品体验</b>（使用者看到什么）→ <b>机制</b>（怎么实现）→ <b>源码证据</b>（文件:行号）→ <b>mini 对照</b>（已复现 / 简化 / 规划）。</p>

!!! note "定位说明"
    本页内容全部可定位到上游源码或 docs；产品体验的归纳（如四种模式中文名）来自 `apps/cli/config/agent-presets/*/preset.yml` 的 description 字段直译，属上游真实存在。上游存在多处*命名换代*（如审批词汇、包路径），相关澄清见各议题。

## 议题 1：模式设计——四种 agent preset

### 1.1 产品体验

`dsh --profile web` 启动后，用户在入口处可切换四种"模式"：**标准模式**、**PTC 模式**、**极简模式**、**创造模式**。它们不是同一 agent 的四种开关，而是**四个不同组成的 agent**：每个模式 = 一组工具目录 + 一套系统提示词 + 若干能力开关，会话内生效、互不影响。

| 模式 | preset 目录（roster） | 一句话差别 |
|---|---|---|
| 标准模式 | `agent-presets/standard/` | 完整编码 agent：bash/pwsh、文件系统、skills、goals、plan、上下文压缩、子代理/工作流、web 搜索 |
| PTC 模式 | `agent-presets/code/` | 标准全部能力 + 经 Code Mode SDK 呈现的编程工具：模型写一个 TypeScript 程序，组合多步文件操作 |
| 极简模式 | `agent-presets/minimal/` | 仅两个工具（持久 bash + str_replace_editor）+ 固定完整提示词，无运行时上下文快照、无压缩 |
| 创造模式 | `agent-presets/cordis/` | 标准能力 + 运行时检查与插件实验（inspect/define/run）、preset 创作指导 |

### 1.2 机制：preset = 会话级 agent 组合

每个 preset 目录含 `preset.yml`（名字、描述、排序）与 `agent.cordis.yml`（组合本体）。关键机制（`packages/preset/README.md:5,14`）：

- **挂载点**：组合挂在 agent 作用域（scope）下，`ctx.agentPresets` 提供词汇表与文件系统发现（可信根 + 用户自建根）；
- **roster 即目录列表**：预置名单就是 `apps/cli/config/agent-presets/` 的目录列举，不维护第二份清单（`preset/README.md:12`）；
- **进程级设施留在 host plane**：注册表、跨会话服务是进程单例，属于 host 组合；preset 只携带"这一个 agent 贡献给它们的东西"。命名了进程级全局服务的行会在挂载时被拒绝，而不是与下一个会话冲突；
- **realm 隔离**：service 行必须挂在带 `isolate` realm 的 group 内（`standard/agent.cordis.yml:11-18`），否则与根 realm 冲突——这就是"一个进程同时跑多个不同 preset 的 agent"的底层保障。

#### 标准模式组合要点（standard/agent.cordis.yml）

- agent-plane 组成：`service agent-mt`（多回合 agent）+ `service agent`（普通）、goal、plan、compaction、subagent、skills 等；
- **每进程只挂一次**（service 行在 scope 树上的位置决定），会话经 scope parentage 加入 agent 作用域；
- plan 状态只写日志（不设服务），`exit_plan_mode` 是唯一退出路径；工具目录跨模式切换保持不变——为了**请求缓存稳定**（system prompt 不变，KV 缓存复用）——这是"plan 工具也要给"的深层原因（`standard/agent.cordis.yml:118 注释`）。

#### 极简模式组合要点（minimal/agent.cordis.yml:1-6）

- persona 块 `complete: true`：系统提示即全部上下文（不注入运行时快照）；
- `includeRuntimeContext: false`；无 compaction；
- 工具目录：持久 `bash` + `str_replace_editor` 两个工具——演示"preset 可以只换工具集就得到全新产品形态"。

### 1.3 源码证据

- `apps/cli/config/agent-presets/{standard,code,minimal,cordis}/preset.yml` —— 四个模式的姓名/描述/排序（order 1-4）
- `apps/cli/config/agent-presets/{standard,minimal}/agent.cordis.yml` —— 组合差异（agent-plane 组合 vs fixed-prompt 双工具）
- `packages/preset/README.md` —— per-session agent composition 机制说明
- `.agents/notes/implemented/architecture/2026-08-03-per-session-agent-presets.md` —— 设计笔记

### 1.4 mini 对照

| 上游 | mini 现状 | 规划 |
|---|---|---|
| preset roster + per-agent mount | `miniharness/preset/{standard,minimal}/preset.json` + `preset/presets.py`：目录列表即名单、per-agent 挂载视图、host 缺工具 fail loud、进程级冲突拒绝挂载（手册 08 章） | — |

## 议题 2：外部入口全景

### 2.1 产品体验

用户以四种方式接触系统：终端交互（web surface）、一次性任务（headless）、自动化和 IDE（三个协议入口）、以及通过官方 SDK 的完整会话操控。所有入口最终都是**同一个事件溯源会话内核**的不同宿主。

### 2.2 机制

```text
启动链路（每个入口相同）：
bin.ts → args 解析（--profile/--patch/--dump-config，task 为位置参数透传） → profile-boot 层
  → loadProfile：定位 profile.yml（内置 bundle 根 + home 用户根双锚点）
  → composeEntries：bundle 栈 + 用户 patch 层叠（header 行 + 增量行）
  → boot()：loader 结算组合树（插件加载、include 展开）
  → 应用提供方（startup）：挂 surface 服务（web 端口 / headless 单任务）
  → watchUserPatches：用户 patch 热更新（仅开发 profile）
```

**启动链路逐节点走读**（对应上图每行）：

1. **bin.ts**：进程入口，只负责把命令行交给下一位，不掺业务。
2. **args 解析**：`--profile`（选 preset）、`--patch`（追加补丁）、`--dump-config`（只读导出组合，boot-free）；`task` 位置参数透传（headless 的明文任务）。
3. **profile-boot 层**：承接已解析参数，进入 profile 装配域。
4. **loadProfile**：定位 `profile.yml`——内置 bundle 根（`packages/bundle/...`）与 home 用户根（`~/.config/...`）**双锚点**，决定用哪个预设组合。
5. **composeEntries**：把 bundle 栈（基础 preset）与用户 patch 层**层叠**成"header 行 + 增量行"的组合条目，未真正实例化插件。
6. **boot()**：loader 结算组合树——实际加载插件、`include` 展开、依赖驱动激活；到这里组合才变成活的运行时。
7. **应用提供方（startup）**：把 surface 服务挂上——web 开端口 / headless 跑单任务，开始对外服务。
8. **watchUserPatches**：仅开发 profile 开启，用户 patch 变更经 HMR 热更新重挂，不重启进程。

四种入口共用同一条启动链路，区别只在第 7 步挂的 surface 服务不同——这是"同一事件溯源内核的多个宿主"在启动层的落地。

| 入口 | 形态 | 协议/载体 | 说明 |
|---|---|---|---|
| web surface | 浏览器 GUI | HTTP + SSE（`host/apiproxy`）+ JSON-RPC 会话协议（`core/session-jsonrpc`） | 完整产品体验：Trajectory、审批、命令、配置等 |
| headless | CLI 一次性任务 | `packages/bundle/headless` | stdout 最后一条非空 assistant 文本；退出码按 turn/end reason；**不支持会话恢复**（见议题 7） |
| ACP 协议 | 自动化客户端协议 | `packages/acp/acp`（stdio JSON-RPC） | 机器到 agent 的规范接口（会话/审批/工具调用） |
| SDK 协议 | 官方 SDK | `packages/sdk/protocol` | Python/TS 官方客户端，全会话操控 |
| hooks 桥接 | Claude Code / Codex hooks | `packages/hooks` | 把 harness 作为这些 IDE agent 的后端工具执行器 |

<p class="mermaid-note">注：三协议入口的会话协议信封与事件序列一致（envelope/编号/消息模型），这是 mini 复现"JSON-RPC 最小子集"（见手册 07 章 §7.6「JSON-RPC 信封子集」）的契约依据。</p>

### 2.3 源码证据

- `apps/cli/src/{bin.ts,args.ts,profile-boot/*,boot.ts}` —— 启动与 profile 加载链路
- `packages/bundle/headless/src/{index,startup}.ts` —— headless 语义（mini 已对齐并复现）；startup.ts:31-56 只解析 task 位置参数与 --help，无 --session
- `docs/architecture.md` 与 `packages/sdk/protocol` —— 协议契约

### 2.4 mini 对照

headless 一次性任务入口（`miniharness/cli/headless.py` + `cli/main.py`）：stdout 最后一条非空 assistant 文本、退出码按 turn/end reason、空任务拒绝、未知 profile fail loud、不开端口、`ctx.appExit` 宿主钩子（手册 07 章）。

协议入口最小子集：JSON-RPC 信封（`miniharness/protocol/sdk.py`，07 章 §7.6）、ACP（`miniharness/protocol/acp.py`，07 章 §7.7）、hooks 桥（`miniharness/protocol/hooks.py`，07 章 §7.8）。

web 传输层（`miniharness/web/`，07 章 §7.5）：两信封 RPC（`client-request`/`server-response`，`server.py` 严格 `{args}` 解包）、WebApi unary 会话服务（`api.py`：list/search/create/selectModel/modelCatalog/…/page unary）、Remote 流 wire（`stream_protocol.py`，单条 `/api/remote.mux` WebSocket 承载 open/cancel/item/end/error 帧 + `$events/result` unary 结算）、`$events` 注册表（`events.py`：ready 首帧 + api-session/* 转发 + waterfall）、`session.follow`/`session.control` 流（`streams.py`，follow=snapshot{header,cursor,records,hasMore,projections}+逐帧、control=baseline{queues,jobs,projections}+替换帧）、审批桥（`approvals.py`：async tools/ask 闸门 ↔ `$events` waterfall 经 result 结算 + `approval/asked|decided` 审计）、会话导出下载（`downloads.py`：`GET /api/session.export`，root + 子代理后代 + 被引用媒体 zip，200/400/404/501/500 状态码链，错误走私有信封外壳）、FastAPI 载体（`server.py`，对齐 gateway `stream-server.ts`/`handler.ts` 状态码链 + frontend-static 契约）、`--profile web` 启动器（`launcher.py` + `cli/main.py`）。契约层已全量对齐 alpha.1 真实契约（上游 React 客户端指向 mini 后端可工作）。**浏览器前端二形态**：产品化 `webui/`（仓库顶层独立 React+TS+Vite 工程，只依赖 wire 契约：会话列表/新建、Trajectory（虚拟化窗口 + Overview 折叠跳转 + 全文 search，2026-09-02 R5 闭合）、审批瀑布、队列/作业；`vite build` 产物经 `MINIHARNESS_WEBUI_DIST` 由后端静态承载）+ `web/static/` vanilla SPA（旧 SSE wire 教学参照，不对新后端工作）。

异步化与并行工具执行（`miniharness/core/agent_loop/tool_calls.py` + core/scope async 变体 + `execution_mode` 分类器，手册 12 章）——屏障/滚动池/模型序提交/取消排干与上游 `agent-loop/src/tool-calls.ts` 逐条对齐。

模型请求重试/退避（`miniharness/llm/retry_policy.py` + `miniharness/llm/retry.py` + `core/agent_loop/agent.py` 接线，手册 04 章 §4.9）——`agent/request-error` waterfall 扩展点、normal/always 策略、有界指数退避 + 对称抖动、`providerRetryAfterMs`（429 Retry-After）优先、durable `llm/retry` + `llm/retry-started` 审计对；派发前熔合信号检查（请求 signal + 重试插件 lifetime，等价 `AbortSignal.any`）、事件驱动多信号竞速等待、插件 effect teardown（注销监听器 + lifetime.abort + 排干在途恢复）与陈旧回调守卫；上下文溢出/认证不在默认可重试白名单 → 终局降级，对齐上游 `llm-retry/src/index.ts` 与 `retry-policy.ts`。

YAML 配置 + `!!js` 插值子集（`miniharness/boot/composition.py`）——pyyaml 硬依赖承载 YAML、`process.env.<NAME>` 子集（其它表达式 fail loud，全量 JS eval 有意不复制、仅放开 env 子集为对齐目标）、`.env` 加载（ENOENT 静默/其它 warn/已存在不覆盖）、组合 dump 渲染；启动器选项（`miniharness/cli/main.py`）——`--patch` 可重复、`--dump-config`/`--dump-default-config` 互斥且 boot-free、dump 不接受任务参数、default 不接受 `--patch`，行级 `# ==` 来源注释 + `!!js` 原样未求值 + skipped patch warn 不失败 + 单文档可再加载；会话管理子命令（`miniharness/cli/session_cmds.py`，mini 教学扩展——上游会话管理在 web 表层；以上测试位于 `tests/test_cli.py`（启动器/会话子命令）与 `tests/test_composition.py`（YAML 组合））；CI（`.github/workflows/ci.yml`：unittest + Python 3.10~3.13 matrix × ubuntu/windows + windows-acl 门控 e2e + demo 冒烟）+ integration 标签真实 API 测试（`tests/test_real_api.py`，`MINIHARNESS_INTEGRATION=1` + key 缺一即跳过）。

## 议题 3：Trajectory 轨迹台账

### 3.1 产品体验

Trajectory 是 **web 专属**的"Agent 的 DevTools"：一个按 turn 组织的可检查事件台账（`packages/client/ui-trajectory/README.md:5`）。它把原始事件流折叠成 User/Assistant/Tool/嵌套 Subtool 的记录，用户可：按时间线扫读（Overview 区域，四种投影模式、TTFT 两色标注）、展开任意记录看局部检查器（Summary/Payload/Timing/Input/Output）、全文搜索（浏览器内）、折叠展开。长会话呈虚拟化滚动：尾部打开、向上翻页加载更早记录。

!!! warning "澄清（重要）"
    Trajectory **不是独立数据系统**，也不是某种"session-timeline"包——全仓 grep 无该包。它就是同一份事件溯源日志在浏览器端的投影视图；`session-query`（议题 7）与它无数据关系。

### 3.2 机制

```text
数据流：
会话日志（唯一数据源）
  → session.page RPC（throughSeq/beforeSeq/maxMessages 游标向前分页，按 append-origin 消息边界切页）
  → ConversationNodeAssembler 折叠（conversation-assembler.ts:133-150）
      · 折叠窗口 = 当前滚动区间的节点
      · 每个 target（user/assistant/tool/steering…）用独立 definition 物化
  → TrajectorySnapshot = { eventNodes, eventLocations, requests, callSchemas, partial, runningCalls }
      （trajectory-contract.ts:60-68）
  → 渲染 + 虚拟化（只挂载可见行窗口 + overscan）
```

**数据流逐节点走读**（对应上图每行）：

1. **会话日志**：唯一数据源，Trajectory 只读它，不维护独立数据。
2. **session.page RPC**：按 `throughSeq`/`beforeSeq` 游标向前分页拉取日志切片，页边界取 **append-origin 消息边界**（保证折叠窗口内消息完整，不劈裂一条消息）。
3. **ConversationNodeAssembler 折叠**（conversation-assembler.ts:133-150）：把原始事件折叠成可检查记录。折叠窗口 = 当前滚动区间的节点；每个 target（user / assistant / tool / steering…）用**独立 definition**（`match/update/finalNode` 纯函数）物化成自己的记录形状。
4. **TrajectorySnapshot**（trajectory-contract.ts:60-68）：折叠的成品——由 `eventNodes`（节点表）、`eventLocations`（节点↔事件序号映射）、`requests`、`callSchemas`、`partial`（崩溃未闭合尾部标记）、`runningCalls`（在飞工具调用）六块组成。
5. **渲染 + 虚拟化**：只挂载可见行窗口（阈值 100 行）+ overscan 12，长会话翻页加载，避免一次性渲染整条日志。

关键点：第 3 步的折叠是**纯函数无副作用**，这决定了 Trajectory 本质是"日志投影"而非"状态机"。

- **折叠引擎是纯函数**：每个折叠定义由 `match/update/finalNode` 构成（trajectory-*-definition.ts），无副作用、可重入——这是"日志投影"而非"状态机"的本质；
- **保留边界**：设计笔记明确拒绝"把日志拍平成裸记录流"——Turn/Step/Request 边界保留因果结构（.agents/notes/implemented/feature/2026-07-27-trajectory-inspection-ledger.md:44）；
- **Overview**：从同一折叠数据投影真实开始时间与耗时（TTFT 等），不是另一套采集；
- **搜索**：浏览器内增量索引（trajectory-search-index.ts），随事件到达增量更新，不走 session-query；
- **虚拟化**：只挂载可见行（阈值 100 行、overscan 12、DOM 上限 160，trajectory-virtualization.e2e.ts 钉住该契约）；
- **分页**：依赖 `session.page` 的消息边界切页语义（议题 7），保证折叠窗口内消息完整。

### 3.3 源码证据

- `packages/client/ui-trajectory/README.md:5` —— Trajectory 定义（turn 组织的可检查回放）
- `packages/client/runtime/src/client/sessions/conversation-assembler.ts:133-150` —— 折叠窗口与 per-target 物化
- `packages/client/ui-trajectory/src/client/trajectory-contract.ts:60-68` —— TrajectorySnapshot 结构
- `packages/client/ui-trajectory/src/client/trajectory-*-definition.ts` —— 纯函数折叠定义（match/update/finalNode）
- `packages/client/ui-trajectory/src/client/trajectory-search-index.ts` —— 浏览器内增量搜索索引
- `apps/web/tests/trajectory-virtualization.e2e.ts` —— 虚拟化行窗口契约
- `.agents/notes/implemented/feature/2026-07-27-trajectory-inspection-ledger.md:44` —— 不扁平化、保留边界的决策

### 3.4 mini 对照

| 上游 | mini 现状 | 规划 |
|---|---|---|
| 折叠引擎（纯函数） | `client/trajectory.py`：turn/step 聚合 chunk→message→timing、callId 树、tool-call 节点只来自 `tool/call` 事件、崩溃尾部 partial 标记、纯函数（手册 10 章） | turn 摘要为教学扩展（上游 trajectory-contract 无 turns 摘要） |
| 虚拟化 / Overview / 搜索 | `client/trajectory.py` 为终端纯函数引擎（R5 在浏览器前端落地） | ✅ 浏览器前端 `webui/` 已实现（2026-09-02 R5）：Trajectory 虚拟化窗口 + Overview 折叠跳转 + 全文 search（`webui/src/trajectory/{model,search}.ts`，纯前端零契约改动）；`client/trajectory.py` 保持终端渲染不变 |

## 议题 4：Agent 干预面

### 4.1 产品体验

宿主（web UI、hooks 桥、ACP 客户端、编排程序）通过 `Agent` handle 干预运行中的 agent：**唤醒**（下一条消息）、**转向**（打断当前思路改道）、**注入**（静默加料）、**取消**（清空排队 + 中止）、**等待静默**（整机 quiescence）、**维护任务**（非回合的后台活）。干预全部经会话日志可查——inbox 每次变更先落 `agent/inbox/spliced`，取消留下 `turn/end {kind:'aborted'}`。

### 4.2 机制：Agent 接口与干预通道

Agent 接口定义于 `packages/core/agent/src/runtime-types.ts:64-144`：

| 方法 | 语义 | 日志体现 |
|---|---|---|
| `followup(messages)` | 下一 turn 唤醒：消息入 inbox，agent idle 时开 turn | inbox 事件 + turn/start |
| `steer(steer)` | 下一 step 唤醒：idle 时同步开 turn；running 时下个 step 边界消费（可转向） | inbox 事件 + 下个 step |
| `inject(messages)` | 非唤醒注入：running 中入 inbox，不触发新 turn | inbox 事件 |
| `send(msg, target, wakeup)` | 统一原语：wakeup 决定 followup 还是 inject | inbox 事件 |
| `cancel(cause, opts)` | 清 inbox（除非 keepInbox）+ abort 活跃活动；idle no-op | inbox/spliced + turn/end aborted |
| `whenIdle()` | 整机 quiescence 信号：无活跃 driver 或 maintenance 才 resolve | — |
| `runMaintenance(task)` | true idle 下执行非回合维护（如 compactNow、goal 驱动的维护路径） | — |

#### 干预通道（waterfall 系列）

- **pre-step**：`agent/pre-step` 瀑布，决策 `{kind:'reject'}|{kind:'enter', messages}`（调用点 `core/agent-loop/src/agent.ts:225-243`）；reject → `turn/end {kind:'blocked'}`（agent.ts:267-269）。真实拒绝者：hooks-codex、hooks-claude-code（把 pre-tool/step 决策映射进来）、goal-round-driver（校验 goal 消息 reservation，见议题 8）；
- **request**：`agent/request` 瀑布可在请求派生前改配置（含 compaction 的 request-error 恢复，见议题 9）；
- **approval**：`tools/pre-execute` 瀑布返回 `{kind:'ask'}` 走审批（见议题 5）；
- **inbox 日志语义**：被拒绝的 claimed 消息"既不丢弃也不重发"；取消时未派发的 tool call 补 `ABORTED_BEFORE_DISPATCH` 错误结果对。

#### 生命周期

对外暴露 `idle | running` 两态（内部另有 maintenance）；`running` 是"驱动级排干区间"而非 turn 是否开着；quiescence = 无活跃 driver 且无 maintenance。`ctx.agents` 提供注册面：register/get/isOwnedBy/list/roots/create/resume/setFactory。

### 4.3 源码证据

- `packages/core/agent/src/runtime-types.ts:64-144` —— Agent 接口全表
- `packages/core/agent-loop/src/agent.ts:225-243, 267-269` —— pre-step 瀑布与 blocked 落日志
- `packages/hooks/hooks-codex/src/index.ts:211` 与 `packages/hooks/hooks-claude-code/src/index.ts:224` —— 真实 pre-step 拒绝者
- `packages/goal/goal-round-driver/src/index.ts:334-414` —— goal reservation 校验（enter 非 reject）

### 4.4 mini 对照

mini loop（`core/agent_loop/agent.py`）已复现完整干预面：`followup` + inbox + pre-step blocked + finally 闭合，以及 `steer`（下一 step 唤醒）/ `inject`（非唤醒入 inbox）/ `cancel`（清 inbox + aborted 闭合边界生效）/ `when_idle`（quiescence）/ `run_maintenance`（仅 true idle）（手册 09 章）。

## 议题 5：一次性审批体验

### 5.1 产品体验

当模型调用需要人类点头的操作（危险命令、写文件、删除等）时，web 面板弹出 **Reject / Allow once** 两个按钮（`packages/client/ui-conversation/src/client/skeleton/ApprovalPanel.tsx:57-83`）。**没有"永远允许"**：每次授权只作用于被询问的那一个动作，下一次调用必须重新 ask。无人值守时可用 `never` 策略确定性拒绝，或由 ACP 机器应答（只给一次性 allow-once/reject-once 两个选项）。

!!! warning "词汇澄清"
    现仓库词汇是 `ApprovalOutcome` / `ApprovalPolicy` / `PreToolDecision`，不存在 `PermissionStatus`、`approvalContext` 等旧符号；且"allowed-once 豁免下一次"不存在——README 已知限制清单明示无 allow-always、无记忆规则、无撤销、无授权存储（`packages/interaction/user-approval/README.md:60`）。

### 5.2 机制

```text
一次审批的数据流：
tools/pre-execute 瀑布（core/tools）→ 返回 {kind:'ask'}（PreToolDecision，tools/src/index.ts:588-591）
  → serviceAsk（tools/src/index.ts:1475-1481, 1689-1729）
      · ctx.get('approval') 缺省/无 agent → 降级 deny
      · 必须处于 open turn（否则 throw，不落任何日志，index.ts:257-265）
  → approval.request({agent, toolName, callId, reason, signal})
      · append 'approval/asked'（审计，log-only）
      · decide()：aborted→cancelled；never→rejected；否则 waterfall('approval/request', …)
        监听器异常/非词汇值 → 'unavailable'（fail-closed）
      · append 'approval/decided'（同一 ApprovalRequestId 关联）
  → 结果映射回工具流水线：allowed-once→{kind:'allow'}；其余→deny+reason
```

- **闭合四值**：`'allowed-once' | 'rejected' | 'cancelled' | 'unavailable'`（`packages/interaction/user-approval/src/types.ts:29`），唯一放行是 allowed-once；
- **两档策略**：`ApprovalPolicy = 'ask' | 'never'`（index.ts:94）；`never` 在进入 waterfall 分发**之前**确定性返回 rejected（index.ts:304-312）——因为 `prepend:true` 的监听器无法绕过 service 自身先裁决；有效策略 = 日志中最后一个 `approval/policy` 事件的纯 fold（index.ts:112-118），恢复无需 catch-up，`setApprovalPolicy()` 是唯一写路径；
- **审计不进模型转录**：三个事件（asked/decided/policy）都 log-only、无 surfaceOp、无 role；模型只看到提问方消费者最终的工具结果。策略另经两条模型可见路径：system-prompt runtime-context 快照（`approval:policy`，order 115，index.ts:204-216）+ 策略切换时注入带 `source:{kind:'plugin'}` 的 user/message（index.ts:226-237）；
- **四个 answerer 通道**：web 人工（`web/approvals.py` 挂 async `tools/ask` 闸门 → `approval/request` 经 `$events` 远程事件瀑布投递 → 浏览器 `$events/result` 回投结算，wire 契约对齐 `packages/api/remotes`，api-proxy.ts:1407-1488 旧路径已随 apiproxy 删除）；ACP 机器（只给 allow-once/reject-once，acp/src/index.ts:215-229）；hooks 桥（把 PreToolUse 的 ask 透传，hooks-claude-code/src/index.ts:238-244）；无内置 answerer（headless 组合 resolve unavailable 并 fail-closed）；
- **权限旋钮在部署层**：`packages/bundle/base/cordis.patch.yml:188-205` 声明 approval 行（policy 由 `DSH_PERMISSION_MODE` 推导）+ `permission-presets` 三档表（read-only / workspace-write / danger-full-access，各自捆绑 sandbox 模式 + 审批策略，schema 在 `packages/interaction/permission-presets/src/index.ts:161-178`）。

### 5.3 源码证据

- `packages/interaction/user-approval/README.md:5,59-62` —— 一次性 seam 总述；仅 open turn 有效；无内置 answerer
- `packages/interaction/user-approval/src/index.ts:34-72,94,112-118,257-344` —— 事件声明、策略、fold、request/decide 全流程
- `packages/core/tools/src/index.ts:588-591,1475-1481,1689-1729` —— PreToolDecision 与 serviceAsk 映射
- `packages/host/apiproxy/src/api-proxy.ts:1407-1488` 与 `packages/host/apiproxy/src/api/approvals.ts:17-21` —— web 通道与应答 payload 约束
- `packages/acp/acp/src/index.ts:215-229` —— ACP 一次性机器选项
- `packages/bundle/base/cordis.patch.yml:188-205` —— 部署级 permission 配置
- `docs/subsystems/approval.md:21,33,86-88` —— fail-closed 语义与审计 log-only

### 5.4 mini 对照

mini 已复现审批层：`ask/never` 两档、'never' 在派发前由服务自身确定性拒绝、审计事件 `approval/asked|decided` turn-enclosed 且 log-only 非 surface、`approval/policy` 最后一条胜出纯 fold、无 answerer/抛错/非词汇表返回值归一化 'unavailable' fail closed、'allowed-once' 无跨调用豁免（手册 09 章）。**web 人工通道已复现**（`web/approvals.py`）：接在工具管线闸门 `tools/ask` 而非上游的 `approval/request`（教学简化），wire 契约一致——`approval/request` 经 `$events` 远程事件瀑布投递、浏览器 `$events/result` 回投结算（result∈APPROVAL_OUTCOMES，非法值 fail-closed 'unavailable'）、网关 dispose 全 pending 'cancelled'（手册 07 章 §7.5.4）。

## 议题 6：运行时自我修改

### 6.1 产品体验

在创造模式下，agent 可以**运行时给自己加新插件**：先 inspect 运行时（列表/查询/自身），define 一个新包（kind new），run 激活，stop/undefine 回收。示例 demo（`scripts/demo-cordis.mjs` + `examples/web-cordis/cordis.yml`，端口 3081）走完全程：define `pkg-1` → run → `invoke('double')` 返回 42。

!!! warning "关键事实（修正预期）"
    原 `packages/self-modification/` 已改名为 `packages/extensions/`。临时插件**只存进程内存，明确不写 cordis.yml、不落盘、重启不恢复**（`tool-cordis/README.md:19`）——"agent 直接改配置"这条路径是设计上排除的。

### 6.2 机制

- **七工具两族**（tool-cordis）：`inspect_list` / `inspect_query` / `inspect_self`（检查）+ `define` / `run` / `stop` / `undefine`（修改）；
- **完整流程**（`cordis-host-runner/tests/runner.spec.ts:150-176`）：define（kind new，mint `dyn-1`/`pkg-1`）→ run（立即 `status:'running'`、`run-1`、`ctx.provide('dynDoubler')` 生效）→ invoke 返回 42；
- **带浏览器半的包**：走 `cordis/request-run` 审批往返（拒绝则不启动，失败回滚）；
- **失败诊断闭环**：run 失败 → `inspect_self` 读诊断 → define 新包 → `run mode:"update"` 热更新；
- **异步结果回喂**：经 `agent.steer` 送回 agent（cordis-host-runner/src/index.ts:1019-1117）。

### 6.3 源码证据

- `packages/extensions/tool-cordis/README.md:19` —— 进程内存、不写 cordis.yml 的明示
- `packages/extensions/cordis-host-runner/tests/runner.spec.ts:150-176` —— 端到端示例全流程
- `packages/extensions/cordis-host-runner/src/index.ts:1019-1117` —— inspect_self 诊断与 steer 回喂
- `.agents/notes/implemented/architecture/2026-08-11-repository-naming-contract-and-rename-ledger.md:260` —— 改名记录

### 6.4 mini 对照

mini 已复现（`extensions/dynamic.py`）：进程内存 define/run/stop/undefine 生命周期，define 仅登记、run 才生效、运行中 run = retract 旧 run 后 replace、undefine 运行中自动 retract、进程级冲突 fail loud、重启不恢复（手册 11 章）。

## 议题 7：会话恢复 / resume

### 7.1 产品体验

web 中打开历史会话有两种不同深度：**读历史窗口**（滚动回看，不激活 agent）与**真正恢复运行**（resume，让 agent 继续干活）。侧边栏搜索会话按标题/工作区名 +（可选）全文内容。headless **不支持** `--session` 恢复——每次都是全新随机会话的一次性任务。

### 7.2 机制

```text
会话检索与恢复：
sidebar 搜索 → ctx.sessions.search（runtime manager.ts:518-527）→ RPC session.search
  · listVisibleSessionSummaries() 划定授权可见集
  · sessionQuery.searchSessions：事件过滤 [user/message, assistant/message] × surface current
  · 分页游标消费（≤ SESSION_SEARCH_PROVIDER_CALL_LIMIT 次），逐 hit 校验可见性
  · snippet 240 codepoints，≤20 条 + hasMore（api/session-search.ts:2,5）

打开窗口（读历史，不激活）：
  ui-workspace open → ctx.sessions.open → RPC session.page（尾页 PAGE_MESSAGES=50）
  → host：historySourceFor（attached 优先，否则持久化 inspect，session-controller/src/index.ts，aliased host/history-source）
  → paginate：消息边界切页 → session.page（throughSeq/beforeSeq/maxMessages，gateway 映射）；客户端与 mux 实时帧按 seq 缝合；subscribedLastSeq>tailSeq 补拉（gap repair）
  → loadOlder（beforeSeq=窗口首 seq）

真正恢复（resume，激活运行）：
  ensureSession：持久化记录存在 → ctx.agents.resume({resumeSessionId})（session-controller/src/index.ts ensureSession 等效）
  → agent-loop：persistence.prepare → setupAndPublish('resume')（agent-loop/src/index.ts:655-710）
  声明式路径：config agents[].resumeSessionId（与 sessionId 互斥，index.ts:270-373）
```

- **分页语义**（session-controller/src/index.ts session.page，原 api-proxy.ts:282-313 已随 apiproxy 删除）：`beforeSeq` 缺省=尾页；从尾部倒着数 maxMessages 个 **append-origin 消息**（user/assistant message 且 isAppendSurfaceEvent）；replacement 拷贝不占配额；同一消息的 chunk/tool 事件经 `sourceEventSeqs` 分组，**绝不在消息中段切页**；`compaction/summary` 与其 replacement 同页；
- **全文检索是 opt-in**：session-query 家族（服务定义 + SQLite FTS5 实现，`packages/session-query/`）索引六类事件（user/message、assistant/message、tool/call name+arguments、tool/result content+error、todo/write、turn/end reason）；结构性事件不产生文档（extraction.ts:13-42）；shipped bundle 默认 `openAt: never`（SQLite 永不打开，搜索抛 `SESSION_QUERY_SEARCH_DISABLED`），此时 sidebar 退化为本地子串匹配（bundle/base/cordis.patch.yml:109-121）；
- **读历史从不 resume 或发布 Agent**（packages/api/session-controller/src/index.ts）——两条路径刻意分离；
- **headless 无 --session**：startup.ts:31-56 只解析 task 位置参数；headless.spec.ts:90 的 resume 桩直接 reject 证明从不调用。

### 7.3 源码证据

- `packages/api/session-controller/src/index.ts`（session.page / session.search / session.fork / resume 契约；原 api-proxy.ts:282-313/1533-1538/1617-1662/2036-2165 已随 apiproxy 删除）—— 分页、historySourceFor、ensureSession、session.search
- `packages/api/session-controller/src/types.ts` —— page 游标（throughSeq/beforeSeq/maxMessages）+ SessionErrorDetailsMap 错误闭集
- `packages/session-query/session-query/src/extraction.ts:13-42` —— 索引文档投影六类事件
- `packages/session-query/session-query-sqlite/README.md:19,40-42,55` —— FTS5、openAt 三态、进程内同步执行
- `packages/core/agent-loop/src/index.ts:655-710` —— resume 的 prepare/setupAndPublish
- `packages/bundle/headless/src/startup.ts:31-56` 与 `tests/headless.spec.ts:90` —— headless 不支持恢复

### 7.4 mini 对照

mini 持久化层已复现（JSONL + fail-closed + interrupted 修复），且 `sessions` 子命令（`cli/session_cmds.py`）提供 CLI 版会话管理（list / resume / delete，教学扩展）。**缺口**：resume 会话选择与 history 分页窗口的 web 表层形态。headless 保持不支持恢复（对齐上游 startup.ts:31-56 只解析 task 位置参数）。

## 议题 8：plan mode 与 goal

### 8.1 产品体验

**plan mode**：`/plan` 进入"先规划后动手"的软引导模式——模型先产出 markdown 计划，经人确认（Approve / Keep planning）后 `exit_plan_mode` 退出。它不锁任何工具，只是把一段指导文本加进每轮请求的 system prompt。**goal**：给 agent 立一个带验收标准的多轮目标，系统自动把目标变成一轮一轮的"goal round"消息喂回 agent，直到完成/暂停/超轮次。

### 8.2 机制

#### plan mode（`packages/plan/plan-mode/`，ctx.planMode）

- **状态只写日志**：唯一持久事实是会话事件 `plan/mode {active:boolean}`（log-only、非 surface、整值替换，index.ts:46-55）；生效状态 = `foldPlanMode` 对日志前缀的纯折叠（index.ts:129-138），resume/fork/compaction 都能恢复，无 live mirror；
- **软指导**：激活时 `plan:policy` prompt section（order 50，index.ts:225-233）把部署配置的指导文本加入每个 model request；sandbox 模式与审批策略是独立强制限制，不读写 plan 状态；
- **工具目录跨模式不变**：`exit_plan_mode` 在非激活时也保持注册（index.ts:63-67），进出 plan 只改 prompt 不改工具目录——请求缓存稳定（standard/agent.cordis.yml:118 注释原文）；
- **写入路径**：`set()` 时若 agent 空闲（无 open turn）立即 append；turn 打开中只记录 pending intent，等下一次**被接受的 in-turn pre-step** 追加（index.ts:425-460）——与 step 边界严格对齐；
- **退出审查**：`exit_plan_mode` 要求完整 markdown 计划，经 user-questions 弹"Plan review"（Approve / Keep planning）；批准 → pending 退出在下一 accepted pre-step 落盘；Keep planning 是带用户反馈的失败调用（index.ts:305-393）。

#### goal（`packages/goal/`：goal + goal-round-driver + tool-goal + command-goal）

- **事件**：durable 事件只有 `goal/change`（全快照或 clear 墓碑，version 1，domain.ts:25-36）+ 进程内通知 `goal/changed`；**不存在** `goal/claim` / `goal/round` 事件——round 只是消息 source 字段与快照计数（澄清）；
- **goal round**：驱动器把"active 且 armed 且未超限"的 goal 变成顺序轮次——每条是 goal 来源的 user 消息（`source:{kind:'goal', goalId, revision, round}`），经 `agent.followup()` 进 inbox，成为普通 FIFO turn（goal-round-driver/src/index.ts:174-179）；
- **pre-step 消费**：goal 消息**不是被 reject 而是 enter**；驱动器在 proposed messages 中认出 goal 来源，下游之前/之后各校验一次完整 reservation（active/armed、revision、round==roundsStarted+1、phase==claimed），校验失败才 reject（或下游 reject 时标记 blocked，code `prompt-rejected`）（index.ts:334-414）；
- **容量与并发**：轮次上限 `maxGoalRounds` 超限自动 block（code `round-limit`，index.ts:166-172）；按 agent 串行化——同一时刻最多一个在飞 round（index.ts:208-241）；resume/fork 后需人授权重新 armed；预留前先 `ctx.sessions.flush()` 检查点保证 goal/changed 持久义务（index.ts:142-154）。

### 8.3 源码证据

- `packages/plan/plan-mode/src/index.ts:46-55,63-67,129-138,205-233,305-393,425-460` —— plan 全链路
- `apps/cli/config/agent-presets/standard/agent.cordis.yml:104-124` —— entry-local realm 与部署指导文本（含 118 行缓存稳定注释）
- `docs/subsystems/plan.md:5,9-11,33` —— 软指导与 log-only 语义
- `packages/goal/goal-round-driver/src/index.ts:142-205,208-241,334-414` —— round 驱动与瀑布消费
- `packages/goal/goal/src/domain.ts:25-36,66` 与 `fold.ts:321-331` —— goal/change 事件与严格重放校验
- `packages/goal/tool-goal/README.md:9-25,49` —— 模型侧工具与权威要求
- `docs/subsystems/goal.md:44-70,100-111` —— 快照字段与 GoalMessageSource

### 8.4 mini 对照

mini 已实现 plan 全链路（状态机 + 审查 UI，`miniharness/plan/`，见下方 §8.5）与 goal 域（事件溯源 + 自动续跑驱动 + 三工具 + `/goal` 命令，`miniharness/goal/`，见 §8.6）。与手册 09/11 章协同。

### 8.5 mini 实现：plan mode 状态机 + 审查 UI（`miniharness/plan/`，A5 + 议题 8 收官）

对齐上游 `packages/plan/plan-mode/` 的 wire/契约核心：

- **状态只写日志**：唯一事实来源是 `plan/mode {active:boolean}`（log-only、非 surface、整值替换），生效状态 = `fold_plan_mode` 沿日志前缀折叠、最后一条胜出；resume/fork 无需 live mirror。`plan/mode` 已入 KNOWN_TYPES，seed 回放 fail-closed。
- **set() 四态**（index.ts:425-445）：`committed`（idle 立即 append）/ `queued`（turn 运行中记 pending，在下一个被接受的 in-turn pre-step 提交）/ `cancelled`（反向 pending 选择被清除、生效状态已匹配目标）/ `noop`（选择与生效或已 pending 状态一致）；被拒绝（reject）或中止的 step 不提交。上游同语义：先记 pending 再判 open turn，commit 一律 append。
- **plan:policy 节**（order 50，index.ts:225-233）：plan mode 生效（含 pending 选择）期间向每次模型请求注入部署方指引；实现经 `core/system_prompt.py` 分节渲染（基底 + 有序非空节，`\n\n` 连接，对齐上游 renderPrompt）。
- **叙述**：仅当最近一次 request/header 描述另一模式时注入一句 user 消息（idle 经 `agent.inject` 入 inbox，queued 经 pre-step 决策 messages）；模型可见 ⟺ 已记录。
- **审查 UI**（review.py + projection.py）：`exit_plan_mode` 工具（跨模式保持注册、要求 `# ` 标题的非空 markdown、批准 → 排队 silent 退出在下一个被接受的 in-turn pre-step 提交、Keep planning 是带反馈的失败调用、取消则提示等待）；`/plan` 命令四态文案逐字对齐（index.ts:274-301）；userQuestions 审查通道（ctx 服务 `userQuestions`，`.ask(question, agent)` 回调）；plan 投影单元（session-projection 的 `plan` 键，纯双事件折叠：`command/run`(name='plan') 记录已落盘选择、`plan/mode` 清空 pending）。
- **命令契约**（`miniharness/commands/`）：`command/run` + `command/done` 按 commandId 配对（log-only 非 surface），命令进入 handler 前先落 run（durable before dispatch）；未命中已注册命令的斜杠行是普通文本；handler 抛错结算为 `kind:'error'`。`command/run|done` 已入 KNOWN_TYPES。

mini 简化（教学范围，须在文档中标注）：canonical value + `Tool.render` 已按上游契约分离（`plan/review.py:163-164` 返回 canonical `{"approved": true}`，`_render_approved` 经 `render=` 承担模型可见文案，`present_call`/`present_result` 提供 UI 卡片——`plan/review.py:188-200`，对应上游 index.ts:319/382-392）；审查为同步回调（上游 async interaction.ask + signal）；userQuestions 无完整 async 对象形状（mini 收敛为 `.ask` 回调契约）。`system-prompt` 已实现 assemble waterfall + contexts/tools/variables 提供器 + `{{variable}}` 严格插值；保留简化：scope 层叠未复现（单全局层）、assembly.tools 提供器结果不直接成为请求工具列表（请求工具来自 ToolRegistry）；运行时上下文快照经 loop 侧投影注入对话消息流（`core/agent_loop/runtime_context.py`，上游 RuntimeContextProjection 同款）。`install_plan_mode` 要求 ctx 已提供 systemPrompt 服务（缺失抛 KeyError，fail loud）；装配序：`install_system_prompt` → `install_plan_mode` →（可选）`install_plan_review`。

### 8.6 mini 实现：goal 域（`miniharness/goal/`，议题 8 收官）

对齐上游 `packages/goal/{goal, goal-round-driver, tool-goal, command-goal}` 的 wire/契约核心：

- **事件溯源**：唯一 durable 事实是 `goal/change`（全快照或 clear 墓碑，version 1）+ 进程内通知 `goal/changed`（`{change: notification}`）；`goal/change` 已入 KNOWN_TYPES，seed 回放 fail-closed。严格重放 fold：decode 校验 kind/version/操作/字段集，非 create 操作要求精确推进一个 revision 且保持计数器/时间戳（对齐 domain.ts / fold.ts）。
- **round 驱动**（GoalDriver）：每条 round 是 goal 来源 user 消息（`source:{kind:'goal', goalId, revision, round}`）经 `agent.followup()` 入 inbox；pre-step 做 fail-closed reservation 校验（active/armed、revision、round==roundsStarted+1、预算），校验失败 reject → turn 以 `{kind:'blocked'}` 闭合。两层驱动表面都已对齐上游：
  - **driver 模式（web/ACP/SDK 的 async 表面）**：GoalDriver 订阅 `agent/status`(idle) 与 `goal/changed`，在 idle 且目标 active+armed 时自动排恰好一个下一轮（对齐上游 goal-round-driver 的 turn/end → continue 事件驱动续跑）；reservation 持久到该 round 回合结束（idle 到达才清除），避免 driver 模式 `followup` 只入队、pre-step 仍需 reservation 校验的竞态。
  - **同步门面（demo/headless 的 run()）**：无嵌套 asyncio.run，仍由宿主显式 `continue_rounds(loop)` 驱动（保留 pull 式契约；其 reservation 在 `followup` 后 `finally` 清除，因同步模式回合在 followup 内跑完）。
  - round 预算用尽自动 block（code `round-limit`）、被拒 block `prompt-rejected`、max-tokens → disarm、aborted → pause，两条路径语义一致。
- **服务**（GoalService，ctx.goals）：compare-and-set 变更全族（create/edit/pause/resume/complete/block/clear）；错误码与上游一致（GOAL_ALREADY_EXISTS / GOAL_STALE_REVISION / GOAL_INVALID_TRANSITION / GOAL_INVALID_OBJECTIVE / GOAL_INVALID_MAX_ROUNDS / GOAL_INVALID_BLOCK_REASON / GOAL_INVALID_EDIT / GOAL_INVALID_STATE / GOAL_NOT_FOUND）；create 仅当前无目标或 complete 时允许；resume 校验 round 预算余量。
- **模型工具**（tool-goal）：`get_goal` / `create_goal` / `update_goal`（description、参数 schema、canonical 输出 JSON 逐字对齐）；update 是 compare-and-set（stale ref → GOAL_STALE_REVISION，非法引用 → GOAL_TOOL_INVALID_UPDATE）；blocked 需 `{code:'model-reported', message}` 且 goal 轮次内未达连续轮数阈值（默认 3）时拒绝（GOAL_TOOL_BLOCK_THRESHOLD）；`tool:goal` prompt section（order 114）。
- **人类命令**（command-goal）：`/goal [<objective>|clear|edit <objective>|pause|resume]`，文案逐字对齐（show / create / edit / pause / resume / clear / 错误提示）。
- **装配序**（示例 `examples/plan_goal_demo.py`）：`install_system_prompt` → `install_commands` → `install_plan_mode` → `install_plan_review` → `install_goals` → `register_goal_tools` → `install_goal_driver` → `install_goal_commands`。

mini 简化（教学范围，须在文档中标注）：**agent registry + assertLive 已闭合（2026-09-01 R4）**——`core/agents.py` AgentRegistry + install_agents，goal `_prepare_mutation`（全公共方法必经）顶部 `assert_live_agent(agent)`；**canonical value + `Tool.render` 已闭合（2026-09-01 R1）**——goal 三工具 execute 返回结构化 canonical 输出、`render=` 承担模型可见文案（不再直接返回模型可见文本）；无 Typert remote 边界（remoteExport* 未复现）；driver 模式已对齐上游事件驱动续跑（`agent/status` idle + `goal/changed` → 自动排下一轮），同步门面保留 `continue_rounds` pull 式契约；无 competingQueued 竞争护栏（armed 目标在任意 idle 都会续跑，不区分是否刚有人类提示）；无 reserved attempt 集与 deferred wrapup 摘要注入（同步模型下 reservation 只在排队→pre-step 间存活，driver 模式延长到回合结束 idle）；权威判定用"当前 step 在 goal 轮次中"近似（authority.kind==='goal-round'，completionAuthority 模块未复现）。

## 议题 9：上下文压缩与后台作业

### 9.1 产品体验

**长对话自动压缩**：上下文压力超过阈值时自动把旧消息折叠成摘要并替换（surface 上留一个检查点消息），前缀重放复用 provider 的 KV cache；provider 报上下文超限时强制减容后重试。**后台作业**：bash/subagent 可 `run_in_background: true`，返回 job id，模型用 `job_output/job_list/job_kill` 收集/管理。**可继续子代理**：子代理以 durable 子会话运行，父 agent 可随时继续喂消息，进程重启后冷恢复。

### 9.2 机制

#### compaction（`packages/compaction/`：seam + compaction-basic + pruner + command-compact）

- **可选能力**：不在 agent-loop 脊柱上；`ctx.tokenMeter` 独立服务（docs/subsystems/compaction.md:5,69）；
- **触发**：`thresholdRatio` 默认 **0.8**（`floor(contextWindow × ratio)` → thresholdTokens，config.ts:20-23,144-154），tokenMeter 请求+响应压力 ≥ 阈值触发；`retainRatio` 默认 0.16 为保留尾部预算；`context-overflow`（provider 报 `CONTEXT_WINDOW_EXCEEDED`）绕过阈值强制减容（index.ts:283-312）；
- **事件**：`compaction/start`（先落地为锁）、`compaction/summary`、`compaction/end`（最后释放，崩溃留下可检测孤儿锁）+ 可选 `compaction/prune`；**不存在** compaction-request / compaction/compress 名称（澄清）；三个事件 log-only；surface 实际变更是带 `surfaceOp:{op:'replace',start,end}` 的 user/message 检查点（事务 id 关联）；
- **KV 复用**：summarization 是一次直接 `ctx.llm.stream()`（purpose:'compaction'），**逐字重放**会话自己的 system prompt、工具、被遮蔽区消息，最后追加固定压缩指令——复用 provider warm prefix cache（summarizer.ts:111,161-176）；
- **衔接**：压力检查挂在串行 `agent/pre-step`（request 派生前）；overflow 恢复挂在 `agent/request-error`，仅当 `surface.replaceGeneration` 前进才返回 retry（index.ts:147-223）；手动路径 compactNow 闭合后 `ctx.sessions.flush()` 放行后续 prompt（region.ts:231-237）。

#### tokenMeter（`packages/llm/token-meter/`）

- 无配置、**自身不产生任何会话事件**：对既有事件流做增量 fold（logRevision = consumedEvents），提供请求压力与表面定价两个快照（index.ts:60-98,116-147）；
- 用量来源：`assistant/message.usage` + `assistant/message`/`assistant/attempt` 内嵌流展开出的 usage 样本（同一步早样本+最终样本，后者替换前者不重复计数，usage-projection.ts:82,116——每次 v2 Assistant 结算贡献其流内嵌的最后一个 usage 样本）；
- 锚定/估算双路径：最新成功请求 usage 在其 canonical 信封与当前 request/header 一致且总数 ≥ 启发式锚价时复用（baseline.kind='usage'），否则估算（4 字符/token 等固定启发式，estimate.ts:12-19）。

#### 后台作业（`packages/jobs/`：seam + jobs-local + tool-jobs）

- 工具：`job_output(job_id, wait?, timeout_ms?)`（默认非阻塞读流式增量，wait 有界）、`job_list()`、`job_kill(job_id, reason?)`；canonical 值 `{text,job}` / `PublicJobSnapshot[]` / `{outcome:'cancellation-requested'|'already-finished', job}`；
- **无 `job/*` 会话事件**（全仓 grep 未命中）：作业完成走进程内 onJobDone/onJobsChanged 监听器 + 模型可见的完成 notice（busy 注入 / idle 唤醒，maxConsecutiveWakes 默认 3，tool-jobs/README.md:19-25）；
- 并发：`maxConcurrentJobsPerOwner` 默认 **10**（按精确 Agent 实例计 running+stopping，jobs-local/src/index.ts:28,144-146）；触发入口是 bash/pwsh/subagent 的 `run_in_background: true`；
- 注册表在 host plane，standard preset 只挂模型侧 tool-jobs（standard/agent.cordis.yml:64-74）。

#### 可继续子代理（`packages/subagent/`）

- durable 子 Session + 至多一个进程内 Activation；Activation 不是 request/result：可执行多个 FIFO turn，后代仍运行期间保持驻留（subagent/continuation.ts:1-11,154-159）；
- durable 事件 `subagent/descriptor`（log-only、跨压缩保留）+ 进程内 observe-only `subagent/start`/`subagent/end`（每个 Activation 驻留期一对，cold resume 是新 epoch 新 runId）；inbox 生命周期沿用 agent/inbox 事件；
- followup 路由：running → 同 Activation 入队；waiting → 唤醒；无 Activation → `coldResume`（persistence.inspect → authorizeLineage → 折叠 descriptor → ctx.agents.resume，不经过 provider，continuation.ts:883-932）；
- standard preset 的 spawn/fork 工具 `backgroundMode: continuable`；结束经 `subagent-settled` settlement notice 送达（与子代理自发 subagent-report 区分）。

### 9.3 源码证据

- `packages/compaction/compaction-basic/src/config.ts:20-23,144-154` —— 0.8/0.16 阈值
- `packages/compaction/compaction-basic/src/region.ts:152-254` —— start→summary→replace→end 事务与 flush
- `packages/compaction/compaction-basic/src/summarizer.ts:111,161-176` —— 前缀重放与 KV 复用
- `packages/compaction/compaction-basic/src/index.ts:147-223,368-420` —— pre-step 压力检查 / request-error 恢复 / compactNow
- `packages/llm/token-meter/src/index.ts:60-98,116-147,221-261` —— 增量 fold 与 usage 折入锚
- `packages/jobs/tool-jobs/README.md:9-15,19-27` 与 `docs/subsystems/jobs.md:16-22,180-285` —— 工具语义与 ctx.jobs 契约
- `packages/jobs/jobs-local/src/index.ts:28,144-146` —— 并发上限 10
- `packages/subagent/subagent/src/continuation.ts:403-457,476-505,883-932,966-1076` —— startContinuable / followup 路由 / coldResume / 物化
- `packages/core/agent/src/consumed-work.ts` + `packages/subagent/subagent/src/lifecycle.ts` —— foldConsumedWork 记账与 epochStopReason 终局折叠
- `apps/cli/config/agent-presets/standard/agent.cordis.yml:64-74,128-155,186-198` —— jobs/compaction/subagent 组合位点

### 9.4 mini 对照

mini 已实现 token 计量与压缩最小版（`llm/token_meter.py` + `compaction/`，装配在 demo/headless/ACP/SDK 入口）：TokenMeter 增量 fold + usage 折入锚（estimateHeader 按 system/tools 启发式定价且 config 不计价）；BasicCompactionEngine 的 pre-step 压力检查（阈值取 adapter.contextWindow，缺省返回 None 而非抛 TargetPressureConfigError）与 request-error overflow 减容（仅 surface.replaceGeneration 前进才 retry，上限 maxOverflowRetries，成功/回合结束边界惰性复位）；事务 compaction/start→前缀重放摘要→user/message 检查点（surfaceOp replace + sourceEventSeqs）→compaction/end，任何失败恰好补一次带 error 的 compaction/end。简化标注：摘要前缀重放无 KV cache 语义、崩溃孤儿锁检出即 busy 拒绝（对齐上游 assertCompactionInactive，上游同样无自动恢复）。toolResultPruner 可选阶段（`compaction/tool_result_pruner.py`）与 `_log_result` 经 `ctx.logger` 路由已对齐上游（见 P0-2 / P0-3）。

mini 已实现后台作业（`jobs/`，进程内注册表 + 三工具 + 完成 notice，装配在 demo/headless/session_cmds/ACP/SDK 入口）：`LocalJobRegistry` 提供 `ctx.jobs` 服务（start/list/get/read/kill/wait、onJobDone/onJobsChanged、attachController），id 为 `<kind>-N`，owned 作业按会话 id 栅栏、unowned 对任何调用方开放，结算 first-wins 且 waiters/kill/终态 read 置 reported 抑制 notice，`maxConcurrentJobsPerOwner` 默认 10（按精确 owner / unowned 桶计 running+stopping），owner 销毁时 cancel 在飞作业 + 限时排干 + 删除（teardown cancel 抛错 force-fail 只改记录）。模型侧 `job_output`（默认非阻塞读流式增量 / wait 有界、响应以 `[status: ...]` 结尾）、`job_list`（`<id> [<kind>] <status> — <label>`）、`job_kill`（requested / already-finished）；完成 notice 按 `completionDelivery` wakeup（idle owner 开 turn，预算 maxConsecutiveWakes=3，user 输入认领恢复）或 quiet（一律 inject），输出与 notice 按 `outputLimitBytes` 做 UTF-8 字节封顶，模型可见输出再经工具层 `finalizeContent` 兜底二次封顶（job_output/job_kill 保 `[status: ...]` 行，`[output truncated]` / `[result truncated]` 截断标记对齐上游 finalizeTaskContent）。与上游一致：**无 `job/*` 会话事件**、不新增事件类型。简化标注：**agent registry + assertLive 已闭合（2026-09-01 R4）**——`core/agents.py` AgentRegistry + install_agents + assert_live_agent，注册点 `AgentLoop.publish()`，jobs `_assert_access`/goal `_prepare_mutation` 接线（controller/监听器另按 scope 分层：`registry.py` `_layers = ScopedLayers(...)` + `chain_layers(scope_of(owner))`，对齐上游 P1-4a 作用域化分层）；**wake 预算载体已闭合（2026-09-02 R2）**——user 输入认领恢复改经安装 ctx 订阅 `agent/inbox/claimed`（仅 `source.kind=='user'` 恢复，对齐 tool-jobs `spendWakes.delete(agent)`）+ `agent/disposed` 防泄漏，AgentLoop 删 `on_inbox_claimed` 零参钩子；**teardown 排干为事件驱动**——逐任务等待 `settled` 事件（`registry.py:431-436`，对齐上游 `await Promise.all(settled)`，非限时轮询）；**canonical value + native renderer 分离已闭合**（`jobs/tools.py` execute 返回结构化结果、`render=` 承担模型可见文本，对齐上游 output.render）；**finalizeContent 载体**——上游经 `outputLimits` WeakMap（pre-execute prepend 捕获）取上限，mini 每次现查（等价回退路径，无 policy 时行为一致）；`run_in_background` 触发入口已经模型侧 `subagent` 工具复现（见下）。

mini 已实现可继续子代理全链路（`seams/subagent/`：descriptor + continuation + 委托工具 `subagent` + 控制工具三件套 + report 工具，装配在 demo/headless/session_cmds 入口）：durable 子会话（header meta + 描述符事件先落盘）+ 冷恢复（inspect → authorizeLineage → fold descriptor → 重建组合）+ 双路径执行（父有 driver → 投递即返回 message id、Activation 跨回合驻留、watchSettlement 结算；无 driver → 同步 pump 门面）；生命周期事件 `subagent/start`/`subagent/end`（runId 配对、SubagentRunInfo/RunEndInfo payload、逐监听器收容派发、经委托父 scope 载体的 scoped dispatch——打标监听器按载波键或其祖先接纳、未打标全局接纳，无标号父退化祖先链派发）；命名 provider 注册表（`register_provider` 登记按模型重建适配器的入口，disposer 注销即发布 `subagent/provider-removed` 边且幂等；解析时注册表优先、回落缺省工厂）；DRAINING 准入截止（manager 级 `drain()` 与 scoped `drain_descendants(parents)`：精确在世根过滤、closingScopes 成员=根+后代世系、child-first 强制结算、聚合错误;`assert_admitting` 接入 start/send 准入边界，拒绝措辞逐字对齐）；终局折叠 `foldConsumedWork` + `epochStopReason`（stepped/claimed 记账、droppedUnrun→aborted）；sendWaking/admitWaking 所有权记账（accepted 窗口、waiting/settled 判定顺序）；interrupt 授权矩阵（user/ancestor authority、stale 调用方防探针、缺席目标接受性 no-op）；嵌套续跑（exec.agent 为授权与所有权主体，孙代结算通知投 durable 直属父，`list_descendants` 沿 parentSession 链 BFS）；finishDisposal 顺序（cancel top-down → flushFinalState best-effort → capture → 拆除 → notifySettlement → releaseOwnership → end 边）。模型侧 `subagent` 工具对齐 tool-subagent 契约（文案/canonical+render/路由逐字，`run_in_background` 触发入口接入 jobs producer）。简化标注：invariant 运行时校验不适用（上游 per-provider invariant 分包架构）、同步模式结算投递走非唤醒 next-step。

## 议题 10：skills 能力家族——按需加载的指令

### 10.1 产品体验

**模型侧**：会话打开时，若存在模型可调用的 skill 且 `skill` 工具可见，agent 会先收到一条 durable 的目录消息——`<system-reminder>` 里的 `<available_skills>` 块，只列出每个 skill 的名字与一句简介（description 最长 500 字符，超长截断）。模型按名调用 `skill` 工具，拿到该 skill 的全文 `<skill_content>`（正文 + 资源指引），再照做。

**用户侧**：用户可以直接在输入里写 `/名字`（词边界匹配）。命中可用户调用的 skill 时，系统把它的全文渲染成一条 user 消息注入对话，模型直接跟随，无需再走工具。

**渐进披露**：目录只放摘要（省钱），正文按需加载（省事），资源（脚本/素材/URL）由指令内的相对引用按需解析。模型不会在一开始就看到所有 skill 的正文。这与 Anthropic 生态的 Claude Code Agent Skills 同构——`~/.claude/skills/<name>/SKILL.md` + frontmatter + 渐进披露；dsh 的差异在于：多了一层**分层注册表**（host + per-scope）、目录消息是**事件溯源出的 durable 会话消息**（resume/fork/compaction 后仍在），以及**双调用表面**（模型工具 vs 用户手势，可分别开关）。

### 10.2 机制：四包四角色

skills 家族由四个包组成，职责分得很清楚：

- **`dsh-skill`**（Service Definition）：只提供 `ctx.skills` 注册表与渲染原语，不落地任何具体 skill；
- **`dsh-skill-filesystem`**（Provider）：把本地目录变成 skill 源，注册进 `ctx.skills`；
- **`dsh-tool-skill`**（Consumer）：目录消息 + `skill` 工具 + `/名字` 手势，模型/用户面对的消费端；
- **`dsh-skill-badge`**（Provider）：一个内置的"dsh 徽章"skill，示范 source `'bundled'`（rank 600）。

#### 分层注册表（`packages/skill/skill/src/index.ts`）

`ctx.skills` 与 `ctx.tools` 同构：一个 global 层 + 每条 scope 链一层，按"离调用会话最近优先"合并，同名冲突由最近层胜出（index.ts:507-512）。provider 注册进调用方所在的层（host 组合挂的插件进 global，preset 挂的进 preset 层）。`SkillSource` 七桶：`project-dsh` / `project-agents` / `runtime` / `user-dsh` / `user-agents` / `custom` / `bundled`（+ 字符串扩张），`skill` 名字必须是 kebab-case 小写（`^[a-z0-9]+(?:-[a-z0-9]+)*$`，index.ts:20）。

三个观察接口（这是 A6 的核心契约）：

1. `list()`：每个 provider 的候选摘要，**期望是一个"完整发现"的数组**，而不是增量补丁；
2. `snapshot()`：`{skills, complete}`，complete 为 true 才缓存复用；collect 期间 revision 变动且重试一次（MAX_COLLECT_ATTEMPTS=2）仍不稳定 → 返回 incomplete 且不缓存（index.ts:525-546）；
3. `get(name)`：全文，不缓存正文（每次重新从 provider 取），所以**正文变更无需版本号/hash/失效通知**。

provider 返回的候选必须通过严格校验（name 合法、description 是字符串等），**非法候选直接抛错 fail fast**（index.ts:642-646），而不是静默过滤。`get()` 若返回的定义名与候选名不符，会精确失效该 provider 并拒绝该条（invalidateEntry，index.ts:629-639）。

#### invocation 策略（双布尔）

每个 skill 携带 `invocation = {modelInvocable, userInvocable}`（index.ts:48），四态齐全：两 false = 禁用；只 model = 模型工具可调、用户手势不可；只 user = 手势可调、目录与工具隐去（`disable-model-invocation` 的 skill 唯一入口）；两 true = 双表面。`get()` 是策略中立的加载原语，由消费方自行判谓词——所以工具与手势对同一个 skill 的可见性可以不同。

#### 渲染（renderSkillContent）

```markdown
<skill_content name="<escaped-name>">
<skill_resources>
<resource-guidance>
</skill_resources>

<skill_instructions>
<provider-owned-instruction-body>
</skill_instructions>
</skill_content>
```

XML 转义（escapeText/escapeAttr，index.ts:217-234），资源指引按 provider 类型四种形态：managed（"Resources ... managed by provider"）、directory（"Base directory for this skill: <path>"）、URL、opaque。

#### 文件系统 provider（`packages/skill/skill-filesystem/src/index.ts`）

- **六个 rank 源**（README 表格）：project-dsh `.dsh/skills`（rank 100）、project-agents `.agents/skills`（200）、custom 配置目录（300）、user-dsh `$DSH_HOME/skills`（400）、user-agents `$DSH_AGENTS_HOME/skills`（500）、bundled `$DSH_BUNDLED_SKILL_DIR`（600）；
- **两种载体**：目录 bundle `<name>/SKILL.md`，或扁平 `<name>.md`；刻意排除嵌套 `**/SKILL.md`（只挖一层）；
- **frontmatter**（YAML）：必填 `name` / `description`，可选 `whenToUse` / `metadata` / `disable-model-invocation` / `user-invocable`；布尔字段接受 `true/false/yes/no/on/off/1/0`；**camelCase 旧键或非法布尔 → 整条 skill 丢弃（fail-closed）+ warn**（README:57），而不是丢弃该字段退回宽松默认；
- **主体与目录分离生命周期**：目录由发现时解析 frontmatter 得到；`get()` 每次重读文件取正文，正文改动无需 hash/缓存失效；
- **watch**：chokidar 观察直接成员增减与 `SKILL.md` 变化，`references/scripts/assets` 等资源目录变化不触发失效；缺失根目录用 `fs.watchFile` 逐段探测；首访工具 `write`/`edit` 会通过 `fs/observed` 同步失效 provider（index.ts:228-234）；
- **ctx.fs 优先**：有 fs 服务时 listDir/readText/`.git` 探测全走 `ctx.fs`（沙箱/远程落地），无则回退 Node fs（README:43）。

#### 消费端（`packages/skill/tool-skill/src/index.ts`）

- **pre-step 目录**：每个 eligible pre-step 调 `ctx.skills.snapshot()`（index.ts:231-250）；首次非空且 `skill` 工具可见 → 向 enter 决策注入 durable user 消息（`<system-reminder>` 目录模板，index.ts:254-277）；
- **digest 判变**：目录消息带 `skill-catalog` source（catalog form，`{entries, update?}`），entries 的 sha256 作为 digest 基准（index.ts:328-337）；成员/描述/可见性变化 → append 一条完整替换目录；删光 → 空目录显式退役旧名字（tombstone）；
- **incomplete 不发**：snapshot 不完整 → 本 pre-step 不发，保留 last-good 目录（index.ts:233-236）；
- **工具可见性参与 digest**：`skill` 工具被限制/被同名 scoped 影子覆盖时目录整体省略——身份比较用"本插件注册的定义"而不是按名查找（README:15），保证全局挂载与 per-agent 挂载行为一致；
- **`/名字` 手势**：SKILL_GESTURE 正则只扫 claimed user 消息（index.ts:409, 177-204），命中可 user 调用的 skill 则 `get()` 全文渲染为 user instructions 注入（`skill-invocation` source），同一步内重复手势只注入一次；未知或 user 禁用的名字保持普通文本；
- **`skill` 工具**：`name` 参数 kebab 校验；错误三态严格区分：`Error: invalid skill name "<name>"` / `Error: skill "<name>" is unknown or no longer available` / `Error: skill "<name>" is not available for model invocation`（index.ts:130-156）。

#### 装配点

- base 组合（host plane）：`skill`（dsh-skill）+ `skill-filesystem` + `skill-badge`（**disabled:true**，packages/bundle/base/cordis.patch.yml:243-245）+ `tool-skill`（237-248）；
- standard preset 层：`skill-filesystem` + `tool-skill`（standard/agent.cordis.yml:83-87）——provider 与消费端成对进 preset 层；
- web-app 组合：host 的 `skill-filesystem` 禁用，由 preset 自己拥有本地发现（packages/bundle/web-app/cordis.patch.yml:323-333）。

### 10.3 源码证据

- `packages/skill/skill/src/index.ts:20-34,39,48,147-234,232-271,279-330,391-454,471-546,622-646` —— 注册表、三接口、渲染、校验、失效
- `packages/skill/skill-filesystem/src/index.ts:36-40,49-89,130,182-234,241` 与 `README.md:15-27,33-59,69-75` —— rank 表、frontmatter、watch、已知局限
- `packages/skill/tool-skill/src/index.ts:34,61-69,81-160,177-251,254-277,328-337,409` 与 `README.md:11-31,39-63,148-168` —— 目录生命周期、工具错误三态、手势、模板
- `packages/skill/skill-badge/src/index.ts:17,25,58` 与 `assets/dsh-badge.md` —— bundled 示例 provider
- `packages/bundle/base/cordis.patch.yml:237-248`、`packages/bundle/web-app/cordis.patch.yml:323-333`、`apps/cli/config/agent-presets/standard/agent.cordis.yml:83-87` —— 装配
- `docs/subsystems/skills.md` 与 `packages/skill/skill/README.md` —— 设计意图（社区实现按 progressive disclosure，同 Anthropic Agent Skills 理念，见 deepseekdocs.com/en/docs/features/skills）

### 10.4 mini 对照

**已复现**（`miniharness/skills/`，L2 编排层；装配在 demo/headless/ACP/SDK 入口）。对上四包的落地：

- `registry.py` —— `SkillRegistry`（`ctx.skills` 服务）：global + scope 链分层注册、`list`/`snapshot`/`get` 三接口、候选校验 fail fast、incomplete 不缓存 + revision 抖动重试一次、collect 缓存按 (cwd, scope 链, revision) 键控 LRU、`invalidate_cache`（revision++ + 清缓存 + `skills/change` 事件，监听器异常容错）、`get()` 定义名不符 → 精确失效该 provider 条目；`renderSkillContent`/escapeText/escapeAttr、digest 均逐字对齐。register_provider 同时接受"create 工厂返回 dict 或对象"两种形态。
- `filesystem.py` —— `FileSystemSkillProvider`：六类根（project-dsh 100 / project-agents 200 / custom 300 / user-dsh 400 / user-agents 500 / bundled 600，同根 rank 一致靠 providerOrder+localOrder 决胜）、目录 bundle `<name>/SKILL.md` + 扁平 `<name>.md`、`find_project_root`（向上找 `.git`）、user-dsh 根跳过 `.system`；frontmatter 经 pyyaml（硬依赖 `safe_load`）；camelCase 旧键或非法布尔 → fail-closed 丢弃 + warn；`get()` 每次重读文件取正文。
- `tool_skill.py` —— 消费端：pre-step 目录注入（首次非空且 `skill` 工具可见 → durable user 消息）、digest 判变（成员/描述/可见性变化 → 完整替换目录）、空目录退役 tombstone、incomplete 不发、工具可见性参与 digest（身份比较用本插件注册的定义）、`skill` 工具（错误三态逐字）、`/名字` 手势（skill-invocation source、去重保序、未知/user 禁用名保持普通文本）；手势 listener 先注册、catalog 后注册（waterfall 顺序保证目录在前、手势离答案最近）。
- 装配：`install_skills(ctx)`（幂等；创建注册表 + 挂 filesystem provider + 两个 pre-step listener），`register_skill_tools(reg, registry)` 把 `skill` 工具注册进现有 ToolRegistry；demo/headless/ACP/SDK 均已接线，standard preset 的 tools 列表保留 `"skills"` 与实现对齐。

**简化标注**：**watch 已闭合**——`FileSystemSkillProvider` 默认开启 watchdog 文件监听（`skills/watcher.py` `SkillWatchManager`，事件过滤/去抖 → 失效回调 + 失效条目精确失效，`filesystem.py:373-459`，`watch=False` 可关）；**badge 为 opt-in bundled provider**——`skills/badge.py` 实现内置 `dsh-badge`，需经 `install_badge_skill(ctx)` 显式装配，`install_skills(ctx)` 不自动装（`skills/__init__.py:62-87`）；无 `ctx.fs` 服务适配（直接 os 读取）；lookup cwd 读 `session.meta['cwd']`（对齐上游 `session.header.cwd`）；execute 直接返回渲染文本（无 canonical value + native renderer 分离，`skills/tool_skill.py:23`）。工具错误前缀已统一 `Error: `（对齐上游 toolErrorResult）。目录注入为每个会话增加 durable 消息——与 `skill` 工具的可见性联动（digest 含工具可见性）已对齐，不存在"目录与工具不一致"的偏差。