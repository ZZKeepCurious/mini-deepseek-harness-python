# 04 · 产品面全解读

<p class="lead">前三个子页回答"系统是什么、怎么运转"；本页回答"<b>用户/宿主看到的是什么、哪些设计塑造了使用体验</b>"。九个议题，每个都按固定四段式展开：<b>产品体验</b>（使用者看到什么）→ <b>机制</b>（怎么实现）→ <b>源码证据</b>（文件:行号）→ <b>mini 对照</b>（已复现 / 简化 / 规划）。</p>

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
| preset roster + per-agent mount | `boot.py`/`apply_patch` 已有组合与 patch 层叠基础；无 preset 概念 | 最小版 preset roster：标准/极简两个 preset 的组合差异 = 工具目录 + prompt 来源，与手册 08 章同步 |

## 议题 2：外部入口全景

### 2.1 产品体验

用户以四种方式接触系统：终端交互（web surface）、一次性任务（headless）、自动化和 IDE（三个协议入口）、以及通过官方 SDK 的完整会话操控。所有入口最终都是**同一个事件溯源会话内核**的不同宿主。

### 2.2 机制

```text
启动链路（每个入口相同）：
bin.ts → args 解析（--profile/--task/--session） → profile-boot 层
  → loadProfile：定位 profile.yml（内置 bundle 根 + home 用户根双锚点）
  → composeEntries：bundle 栈 + 用户 patch 层叠（header 行 + 增量行）
  → boot()：loader 结算组合树（插件加载、include 展开）
  → 应用提供方（startup）：挂 surface 服务（web 端口 / headless 单任务）
  → watchUserPatches：用户 patch 热更新（仅开发 profile）
```

| 入口 | 形态 | 协议/载体 | 说明 |
|---|---|---|---|
| web surface | 浏览器 GUI | HTTP + SSE（`host/apiproxy`）+ JSON-RPC 会话协议（`core/session-jsonrpc`） | 完整产品体验：Trajectory、审批、命令、配置等 |
| headless | CLI 一次性任务 | `bundle/headless` | stdout 最后一条非空 assistant 文本；退出码按 turn/end reason；**不支持会话恢复**（见议题 7） |
| ACP 协议 | 自动化客户端协议 | `packages/acp/acp`（stdio JSON-RPC） | 机器到 agent 的规范接口（会话/审批/工具调用） |
| SDK 协议 | 官方 SDK | `packages/sdk/protocol` | Python/TS 官方客户端，全会话操控 |
| hooks 桥接 | Claude Code / Codex hooks | `packages/hooks` | 把 harness 作为这些 IDE agent 的后端工具执行器 |

<p class="mermaid-note">注：三协议入口的会话协议信封与事件序列一致（envelope/编号/消息模型），这是 mini 复现"JSON-RPC 最小子集"（ROADMAP 阶段 12）的契约依据。</p>

### 2.3 源码证据

- `apps/cli/src/{bin.ts,args.ts,profile-boot/*,boot.ts}` —— 启动与 profile 加载链路
- `bundle/headless/src/{index,startup}.ts` —— headless 语义（mini 已对齐并复现）；startup.ts:31-56 只解析 task 位置参数与 --help，无 --session
- `docs/architecture.md` 与 `packages/sdk/protocol` —— 协议契约

### 2.4 mini 对照

headless 一次性任务入口（`miniharness/headless.py` + `cli.py`）：stdout 最后一条非空 assistant 文本、退出码按 turn/end reason、空任务拒绝、未知 profile fail loud、不开端口、`ctx.appExit` 宿主钩子。9 个测试，手册 07 章。

协议入口最小子集：JSON-RPC 信封（`miniharness/sdk_protocol.py`，21 测试，07 章 §7.6）、ACP（`miniharness/acp.py`，26 测试，07 章 §7.7）、hooks 桥（`miniharness/hooks.py`，40 测试，07 章 §7.8）；web 表面留在观察清单。

异步化与并行工具执行（`miniharness/scheduler.py` + bus async 变体 + `execution_mode` 分类器，36 测试，手册 12 章）——屏障/滚动池/模型序提交/取消排干与上游 `agent-loop/src/tool-calls.ts` 逐条对齐。

模型请求重试/退避（`miniharness/retry_policy.py` + `miniharness/llm_retry.py` + `loop.py` 接线，36 测试，手册 04 章 §4.10）——`agent/request-error` waterfall 扩展点、normal/always 策略、有界指数退避 + 对称抖动、`providerRetryAfterMs`（429 Retry-After）优先、durable `llm/retry` + `llm/retry-started` 审计对；上下文溢出/认证不在默认可重试白名单 → 终局降级，对齐上游 `llm-retry/src/index.ts` 与 `retry-policy.ts`。

YAML 配置 + `!!js` 插值子集（`miniharness/composition.py`，23 测试）——pyyaml 可选依赖双载体、`process.env.<NAME>` 子集（其它表达式 fail loud，简化为不求值 JS）、`.env` 加载（ENOENT 静默/其它 warn/已存在不覆盖）、组合 dump 渲染；启动器选项（`miniharness/cli.py`，24 测试）——`--patch` 可重复、`--dump-config`/`--dump-default-config` 互斥且 boot-free、dump 不接受任务参数、default 不接受 `--patch`，行级 `# ==` 来源注释 + `!!js` 原样未求值 + skipped patch warn 不失败 + 单文档可再加载；会话管理子命令（`miniharness/sessions.py`，8 测试，mini 教学扩展——上游会话管理在 web 表层）；CI（`.github/workflows/ci.yml`：unittest + Python 3.10~3.13 matrix × ubuntu/windows + demo 冒烟）+ integration 标签真实 API 测试（`tests/test_real_api.py`，`MINIHARNESS_INTEGRATION=1` + key 缺一即跳过）。当前 398 个测试全绿。

## 议题 3：Trajectory 轨迹台账

### 3.1 产品体验

Trajectory 是 **web 专属**的"Agent 的 DevTools"：一个按 turn 组织的可检查事件台账（`packages/client/ui-trajectory/README.md:5`）。它把原始事件流折叠成 User/Assistant/Tool/嵌套 Subtool 的记录，用户可：按时间线扫读（Overview 区域，四种投影模式、TTFT 两色标注）、展开任意记录看局部检查器（Summary/Payload/Timing/Input/Output）、全文搜索（浏览器内）、折叠展开。长会话呈虚拟化滚动：尾部打开、向上翻页加载更早记录。

!!! warning "澄清（重要）"
    Trajectory **不是独立数据系统**，也不是某种"session-timeline"包——全仓 grep 无该包。它就是同一份事件溯源日志在浏览器端的投影视图；`session-query`（议题 7）与它无数据关系。

### 3.2 机制

```text
数据流：
会话日志（唯一数据源）
  → session.history RPC（beforeSeq 向前分页，按 append-origin 消息边界切页）
  → ConversationNodeAssembler 折叠（conversation-assembler.ts:133-150）
      · 折叠窗口 = 当前滚动区间的节点
      · 每个 target（user/assistant/tool/steering…）用独立 definition 物化
  → TrajectorySnapshot = { eventNodes, eventLocations, requests, callSchemas, partial, runningCalls }
      （trajectory-contract.ts:60-68）
  → 渲染 + 虚拟化（只挂载可见行窗口 + overscan）
```

- **折叠引擎是纯函数**：每个折叠定义由 `match/update/finalNode` 构成（trajectory-*-definition.ts），无副作用、可重入——这是"日志投影"而非"状态机"的本质；
- **保留边界**：设计笔记明确拒绝"把日志拍平成裸记录流"——Turn/Step/Request 边界保留因果结构（.agents/notes/implemented/feature/2026-07-27-trajectory-inspection-ledger.md:44）；
- **Overview**：从同一折叠数据投影真实开始时间与耗时（TTFT 等），不是另一套采集；
- **搜索**：浏览器内增量索引（trajectory-search-index.ts），随事件到达增量更新，不走 session-query；
- **虚拟化**：只挂载可见行（阈值 100 行、overscan 12、DOM 上限 160，trajectory-virtualization.e2e.ts 钉住该契约）；
- **分页**：依赖 `session.history` 的消息边界切页语义（议题 7），保证折叠窗口内消息完整。

### 3.3 源码证据

- `packages/client/ui-trajectory/README.md:5` —— Trajectory 定义（turn 组织的可检查回放）
- `packages/client/ui-trajectory/src/client/conversation-assembler.ts:133-150` —— 折叠窗口与 per-target 物化
- `packages/client/ui-trajectory/src/shared/trajectory-contract.ts:60-68` —— TrajectorySnapshot 结构
- `packages/client/ui-trajectory/src/shared/trajectory-*-definition.ts` —— 纯函数折叠定义（match/update/finalNode）
- `packages/client/ui-trajectory/src/client/trajectory-search-index.ts` —— 浏览器内增量搜索索引
- `apps/web/tests/trajectory-virtualization.e2e.ts` —— 虚拟化行窗口契约
- `.agents/notes/implemented/feature/2026-07-27-trajectory-inspection-ledger.md:44` —— 不扁平化、保留边界的决策

### 3.4 mini 对照

| 上游 | mini 现状 | 规划 |
|---|---|---|
| 折叠引擎（纯函数） | `headless.py::summarize` 是最简投影（只拼 text 块） | 完整折叠：turn:step 聚合 chunk→message→timing、callId 树、steering 判定、compaction request、request header 继承 → TrajectorySnapshot 等价物（手册 10 章） |
| 虚拟化 / Overview / 搜索 | 无（终端输出） | 终端渲染或 JSON dump 简化版；不复制浏览器 UI |

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

mini loop 现有：`followup` + inbox + pre-step blocked + finally 闭合。**缺口**：steer/inject/cancel/whenIdle 语义。规划：手册 09 章 + loop 代码对齐（steer 改向、cancel 清 inbox+abort、quiescence 判定）。

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
- **四个 answerer 通道**：web 人工（api-proxy 把 approval/request 进 pending 注册表 → `approval/requested` mux 帧 → 浏览器 `POST /api/respond` → `approval/resolved` 广播，api-proxy.ts:1407-1488）；ACP 机器（只给 allow-once/reject-once，acp/src/index.ts:215-229）；hooks 桥（把 PreToolUse 的 ask 透传，hooks-claude-code/src/index.ts:238-244）；无内置 answerer（headless 组合 resolve unavailable 并 fail-closed）；
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

mini 无审批层。规划：pre-execute 瀑布已复现（tools.py），可加最小 `ask/never` 两档 + 审计事件 `approval/asked|decided`（日志语义对齐），与手册 09 章合并推进。

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
- `.agents/notes/implemented/2026-08-11-repository-naming-contract-and-rename-ledger.md:260` —— 改名记录

### 6.4 mini 对照

规划（手册 11 章）：在 Context/PluginManager 上做"动态插件"简化版——进程内存 define/run/stop，生命周期事件对齐（define 后 run、stop 回收、重启不恢复）。价值排序靠后。

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
  ui-workspace open → ctx.sessions.open → RPC session.history（尾页 PAGE_MESSAGES=50）
  → host：historySourceFor（attached 优先，否则持久化 inspect，api-proxy.ts:1533-1538）
  → paginate：消息边界切页 → historyPage；客户端与 mux 实时帧按 seq 缝合；subscribedLastSeq>tailSeq 补拉（gap repair）
  → loadOlder（beforeSeq=窗口首 seq）

真正恢复（resume，激活运行）：
  ensureSession：持久化记录存在 → ctx.agents.resume({resumeSessionId})（api-proxy.ts:1617-1662）
  → agent-loop：persistence.prepare → setupAndPublish('resume')（agent-loop/src/index.ts:655-710）
  声明式路径：config agents[].resumeSessionId（与 sessionId 互斥，index.ts:270-373）
```

- **分页语义**（api-proxy.ts:282-313）：`beforeSeq` 缺省=尾页；从尾部倒着数 maxMessages 个 **append-origin 消息**（user/assistant message 且 isAppendSurfaceEvent）；replacement 拷贝不占配额；同一消息的 chunk/tool 事件经 `sourceEventSeqs` 分组，**绝不在消息中段切页**；`compaction/summary` 与其 replacement 同页；
- **全文检索是 opt-in**：session-query 家族（服务定义 + SQLite FTS5 实现，`packages/session-query/`）索引六类事件（user/message、assistant/message、tool/call name+arguments、tool/result content+error、todo/write、turn/end reason）；结构性事件不产生文档（extraction.ts:13-42）；shipped bundle 默认 `openAt: never`（SQLite 永不打开，搜索抛 `SESSION_QUERY_SEARCH_DISABLED`），此时 sidebar 退化为本地子串匹配（bundle/base/cordis.patch.yml:109-121）；
- **读历史从不 resume 或发布 Agent**（api/sessions.ts:279-280）——两条路径刻意分离；
- **headless 无 --session**：startup.ts:31-56 只解析 task 位置参数；headless.spec.ts:90 的 resume 桩直接 reject 证明从不调用。

### 7.3 源码证据

- `packages/host/apiproxy/src/api-proxy.ts:282-313, 1533-1538, 1617-1662, 2036-2165` —— 分页、historySourceFor、ensureSession、session.search
- `packages/host/apiproxy/src/api/sessions.ts:236-283` —— search/history 契约（不 resume 的明示）
- `packages/session-query/session-query/src/extraction.ts:13-42` —— 索引文档投影六类事件
- `packages/session-query/session-query-sqlite/README.md:19,40-42,55` —— FTS5、openAt 三态、进程内同步执行
- `packages/core/agent-loop/src/index.ts:655-710` —— resume 的 prepare/setupAndPublish
- `packages/bundle/headless/src/startup.ts:31-56` 与 `tests/headless.spec.ts:90` —— headless 不支持恢复

### 7.4 mini 对照

mini 持久化层已复现（JSONL + fail-closed + interrupted 修复）。**缺口**：resume 会话选择、history 分页窗口。规划：分页语义（消息边界切页）与 headless `--session` 恢复的取舍——建议先复现"读历史窗口"语义，headless 保持不支持恢复（对齐上游）。

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

mini 无 plan/goal。规划：plan 先做"log-only 状态 + prompt section 注入"（最简，不引入审查 UI）；goal 后置。与手册 09/11 章协同。

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
- 用量来源：`assistant/message.usage` + `assistant/chunk` 中 `chunk.type==='usage'` 的早样本（同一步早样本+最终样本，后者替换前者不重复计数，usage-projection.ts:74-123）；
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
- `apps/cli/config/agent-presets/standard/agent.cordis.yml:64-74,128-155,186-198` —— jobs/compaction/subagent 组合位点

### 9.4 mini 对照

mini 无压缩/作业/子代理续跑。规划（按价值排序）：① token 计量 fold（可先做固定启发式估算版）；② 压缩最小版（pre-step 压力检查 + summary replace 检查点 + 前缀重放标注简化）；③ 后台作业语义（进程内注册表，无会话事件——与上游一致）后置。