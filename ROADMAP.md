# ROADMAP：MiniHarness 的方向与规划

> 项目目标：用 Python（成熟开源库优先，无语义等价库时才用标准库手写）从 0 到 1 复现 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的核心约定，逐模块对照上游（`deepseek-harness/`）解读与重写；目标是**面向产品化的生产就绪**——凡真实部署中构成可靠性/安全/互操作/体验实质风险的项，不因「教学/demo 可接受」豁免，按生产就绪标准补齐或立项。
> 原则：每个阶段可独立运行、有测试、可演示；优先"约定正确"而非"功能齐全"。

## 覆盖范围

核心约定已全部实现，能力清单见 README「已实现能力」表；每个模块与上游包的权威归属见 [docs/architecture.md](docs/architecture.md) 映射表。

web 半（传输层 + 浏览器前端）的 wire 面与上游一致：两信封 RPC、Remote 流（单条 `/api/remote.mux` WebSocket 承载 `open/item/end/cancel/error` 帧，帧字段集合精确匹配，每流一条 256 KiB 有界上行 inbox + `$events/result` unary 结算）、`$events` 注册表（api-session/* 转发 + `approval/request` waterfall 审批桥）、`session.follow`/`session.control` 流、静态服务约定、会话日志导出 `GET /api/session.export`（zip 打包 root + 子代理后代 + 被引用媒体）。上游 React 客户端指向 mini 后端可工作。浏览器前端有两种形态：产品化 `webui/`（仓库顶层独立 React+TS+Vite 工程，只依赖 wire 约定；`vite build` 产物经 `MINIHARNESS_WEBUI_DIST` 由后端静态承载）+ `web/static/` vanilla SPA 教学参照（旧 SSE wire，不对新后端工作）。设计与决策记录在 `status/mini-harness/`。

## 规划

下一主线候选：插件示例集（教程用插件 + 真实工具演示）；更多 agent 编排（agent-team 已实现）；遥测上报面（telemetry-capture / OpenTelemetry，需远端数据面）。

## 扩展能力

- **MCP 客户端 / 资源**：`miniharness/mcp/`（L3）——`apply(ctx, config)` 代连接 supervisor（stdio / streamable-http 统一，`tools/list_changed` 重同步、server instructions 字节上限 fail-loud、`failOnStartupError`）+ 指数退避重连（预算耗尽即停）+ 工具归属注册 `${server}.${name}#${hash}` + darkfrozen render / 图片受理投影 + mcp-resources 三个共享资源工具。**载体注记（SDK 2.2）**：SDK stdio 传输不投递子进程 EOF/退出（`_drain_stdout` 永久阻塞、read_stream 不关闭、在途 RPC 永挂）——mini 用有界 RPC 竞速（5s，对 `generation.lost` 中止）加常驻生命线 ping watchdog（15s 心跳 / 3s 超时），取代上游 within 取消令牌，避免子进程退出后握手流程阻塞；关闭屏障 5s 超时 fail-closed 防止重叠子进程。

- **storage 存储中心**：`miniharness/storage/`（L1）——hub + domain 数据形态 + JSON backend（`ctx.storage`）：hub 不碰 IO（backend 注册表 + `storage.backend.<name>` 生命周期服务键）；domain 带 schema 校验、单写链、持久后 `domain/changed` 事件发射、backup-and-skip 坏记录政策；JSON medium 支持 single 整文档与 per-record 一记录一文档（原子重写、legacy bootstrap、foreign 文档读缺位）。对应 `packages/storage`（storage hub + storage-domain + storage-json；storage-sqlite 不承载）。

- **遥测 / 用量统计**：`miniharness/telemetry/`（L2）——sessionStats + tokenUsage 真实投影 + per-turn 用量推导（`fold_session_stats` / `fold_token_usage` / `derive_turn_token_usage`，与上游 session-stats / token-meter 一致）+ opt-in `UsageStatsService`；wire `projections.values` 由空基线改为真实视图；CLI `sessions stats [id]` 教学扩展。

- **后台作业（jobs 域 rc.1 重写）**：`miniharness/jobs/`（L2）对齐 `packages/jobs`（seam + jobs-local + tool-jobs）——`JobSpec.run(JobHandle)`（`append`/`updateProgress`）与 pull 源（`JobOutputSource`）由注册表按 cadence 泵入每作业一个有界 `OutputRing`（绝对字节偏移、头部驱逐、UTF-8 安全超块尾部、lossy 读）；消费式 `read`（模型游标）与 `readAt`（绝对偏移）读同一批字节且互不干扰，`JobOutcome.result` 交出值型结果；`JobEvents.subscribe`（`{owner}`/`{owners:'all'|'scope'}`）经 scope 分层路由，`settled{cause,awaited}` 描述结算；模型侧三工具 `job_output`/`job_list`/`job_kill` + 完成 notice（缺省无界唤醒），`job` 家族并入 workspace 归档准入；消费方 = `seams/subagent` 后台委托、`tool_terminal` pty-send 后台。见 verified-diffs §2.67/§3.44。

- **shell 域（执行器句柄 + 模型工具 + shell-env）**：`miniharness/shell/`（L3）对齐 `packages/shell/{shell,bash-local,bash-sandbox,shell-env}`——`execute(spec) -> ShellExecution`（句柄含 status/exitCode/signal/done/非消耗 `observed` 偏移读/消费式 `read_output()`/`kill()`/memoized `result()`），`resolve` 补齐 workdir/timeoutMs/`onExpiry`(kill\|none)/stdoutMaxBytes，已 aborted signal 视为已触发；`env.py` 的 `ctx.shellEnv` 收集内置 `DSH_*` 并在 profile 上下文在场时填充保留键 `DSH_PROFILE`/`DSH_PROFILE_DIR`。`miniharness/tool_bash/`（L3）对齐 `packages/shell/tool-bash`——模型面 bash 工具 Config `{enableRunInBackground?, promoteOnTimeout?}`（缺省 true）：前台超时不再杀而提升为后台作业（`promoted` kind）、`run_in_background` 返回 `background` kind、前台投影可带 `stopped`；`process_outcome`/`process_sources`/`ring_delta` + render 逐字对齐。载体差异：无 `ctx.subprocess` 托管范围/spill（Popen + 读/监视线程 + 内存增长缓冲）、无 schemastery/沙箱升级审批面、jobs 一次装配。见 verified-diffs §2.68/§3.45。

- **Agent Teams**：`seams/agent_team/` 隐式 root roster + durable peer mailbox + 共享任务 DAG（四类 `team/*` 事件全 log-only，Team Lead 会话为权威 journal）+ 模型侧 9 工具与 `team:policy` 提示节（同步门面 + 事件循环内 async 双投递载体）。

- **图片输入请求（DeepSeek Files API 执行簇）**：`miniharness/llm/deepseek_files/`（L1）——file-id/defaults/models/types/model-info（catalog 能力解析）+ image-tokens（provider vision-token 计算器逐字移植）+ request-pricing（`ImageRequestTarget` 路由目标 + offloaded/retained 定价）+ files-api（Messages 协议 `/v1/files` httpx 传输）+ upload-index（`files-v3.json` + filelock 跨进程锁 + `os.replace` 原子发布）+ file-store（单飞共享上传 + 索引复用 + 配额恢复）+ request-files（stale-id 恰一次重试 + 规范化图片诊断）；`DeepSeekAdapter` 按目录宣称 image 输入并走 image-capable 请求路径（Files file-id 优先 → 解析失败整请求回退 inline base64）。载体差异：httpx 异步替代 fetch/FormData、filelock + `os.replace` 替代 dsh-atomic-write（见 verified-diffs §2.36）。

- **图像卸载决策（message 投影）**：`core/session/projections.py`（L0）+ `compaction/image_offload.py`（L2）——`image/offload` durable 事件记录要永久卸载的输入图片 occurrence（当前 surface 的 user/message 或 tool/result 节点、深度优先序号、严格递增 + 拆分校验 fail-closed）；`derive_messages` 经 `fold_projections` 把选中 occurrence 投影为不可变 offloaded 副本（身份保留），模型侧投影为占位文本；`offload_oldest_images` + `agent/request-error` 上的 `IMAGE_OFFLOAD_REQUIRED` surface 修复（不消耗重试预算、不落 retry 事件）对应上游 `compaction-image-offload`。会话格式目录新增 `read_released_header` / `encode_current_header` / `encode_current_event`（对应 `session-format-catalog`）。

- **PTC 运行时（ptc-runtime）**：PTC = programmatic tool calls（程序化工具调用）——模型写一段程序、在程序内通过宿主异步绑定调用工具的模式（上游更名记录：`packages/.agents/notes/archived/architecture/2026-08-25-rename-code-mode-to-ptc.md`，旧称 "Code Mode"）。`miniharness/ptc_runtime/`（L1）——`PtcRuntime` Service Definition（`ctx.ptcRuntime`）+ 保留名常量 + 绑定校验；`PythonPtcRuntime` 每请求在全新 CPython 子进程跑模型 Python（顶层 await/return），绑定经 stdin/stdout 行 JSON 协议桥接，墙钟预算/中止/输出上限 + 正交失败分类（exception/timeout/abort/worker-exit/invalid-output/output-limit/protocol）；`install_ptc_runtime(ctx)`。载体差异：上游 Node 后端 worker/subprocess + fd-3 wire，mini CPython 子进程 + 行 JSON；子进程非安全边界（上游同款声明）。**PTC 模式 `run_code` 工具已落地**：`miniharness/ptc/run_code.py`（L2，tools-presentation seam）——程序经 `tools.<name>` 调用 agent 可见工具，子派发落 `tool/ptc-dispatch-start`/`tool/ptc-dispatch`（`subCallId`=`<parent>:ptc:<n>`），只有外层精心挑选结果进模型历史；`describe`/`parameters` 按 runtime 语言取 flavor。载体差异：上游用 registry 分阶段调度接口 + 并发池，mini 经 `run_pipeline` 顺序执行（并发上限简化登记）。

- **会话检查点策略（session-checkpoint-policy）**：`seams/session_checkpoint.py`（L3）+ `SessionStore.checkpoint`（fail-closed）——三种语义持久化屏障（模型请求前 / 顶层工具体前 / 每步边界），经 `agent/checkpoint` / `tools/pre-execute` / `agent/pre-step` 三个挂点；取消落在检查点窗口内折叠为 canonical `ABORTED_BEFORE_DISPATCH`，检查点失败 fail-closed 不进入下游副作用；嵌套工具派发复用外层检查点。`install_checkpoint_policy(ctx)` opt-in 装配。载体差异：上游经 `llm/stream` 服务 waterfall 延迟适配器构造，mini 单一 adapter 直接调用故改用 `agent/checkpoint` waterfall。

- **浏览器终端域（terminal-controller）**：`miniharness/terminal_controller/`（L3）——`ctx.terminalController` 的 Remote namespace `terminal/*`（`environment`/`shells`/`list`/`create`/`follow`/`write`/`resize`/`rename`/`close`）；`BrowserTerminal` 以 pyte `HistoryScreen` 保存有界恢复屏、follow 独占输入并把旧附加降为只读、close 先 terminate→drain→finish；`TerminalFollower` 按 `JSON.stringify(frame)` UTF-8 字节预算并显式失败；`shells.py` 的 `resolve_executable` 对齐 subprocess-local（空拒/相对路径拒/绝对 stat+X_OK/PATH×PATHEXT）；`sandboxPolicy.add_mode_fence` 阻断保留终端时的会话模式切换。与 `terminal`/`terminal_bash`/`tool_terminal` 合成 terminal 域全量对齐，web 侧经 `terminal/*` unary + `terminal/follow` 流暴露（P4，见 verified-diffs §2.55/§3.32）。

- **api 残余三控制器（workspace / workspace-files / settings+credentials）**：`miniharness/{workspace_controller,workspace_files,settings_controller}/`（L3）——`workspace` namespace（十命令 + `follow` 投影流，注册表顺序/归档集/置顶集/首用默认登记扩展，归档活跃折 `workspace/session-active`）、`workspaceFiles` namespace（有界行 `read`/原生字节 `readBytes`（rc.1 折入 `readAll`/`readRelated`：`options.range`/`options.baseFile`）/`stat`/工作区限定 `list` + 目标级 `changes` 观察流经 `fs.watch`+`fs/observed`）、`settings`/`credentials` namespace（脱敏 describe + 三写 + 文档/预设目录打开 + 引用读写）。api 组主体全部复现（`remotes` 为组装胶水）；workspace rc.1 增量（`initializeDefault`/`pinSession`/`unpinSession` + `workspace/session-active`）见 verified-diffs §2.63/§3.40。

- **请求上下文插件（context 组）**：`miniharness/context/`（L2）——`time_context`（每步时钟读数 + 浏览器时区策略 + `timeContext` 投影）、`tmux_context`（tmux 方位查询/变化抑制 + `tmuxContext` 投影）、`file_reference`+`file_reference_local`（`@file` 词法 + `WorkspaceFileSearch` 模糊索引 + `ctx.fileReferences`）、`session_reference`（`dsh-session:` URI/提及 + 跨会话有界快照 + `sessionReferenceResolver/candidates` Remote）、`agent_instructions`（AGENTS.md 发现/去重/预算渲染/基线/reconcile + pre-step 注入与 `tools/post-execute` touch）。见 verified-diffs §2.57/§3.34。

- **子代理归档准入与激活容量（subagent rc.1 增量）**：`miniharness/seams/subagent/archive_admission.py`（L3）+ `continuation.py`——running subagent 后代按 durable lineage（`parentSession` + `origin:'subagent'`，任意深度，fork 不在列）回答 `workspace/session-activity` 的 `{kind:'subagent'}`，`workspace/session-stop` 以 parent 身份逐个 `cancel('parent')`；进程内 `ActivationPool`（经不间断 continuable 父链共享、按根 AgentLoop 记在 `WeakKeyDictionary`）在物化前预留槽位，满额抛 `ACTIVATION_LIMIT_REACHED`，结算/回滚释放；runtime Config `maxDepth` 默认 1、`maxActiveSubagents` 默认 8、`resolve_max_depth()`；`list_agents` status 枚举 `running`/`inactive`（省略非 continuable）并支持 `children`/`descendants` scope。载体说明：浏览器 `subagent.list` Remote、`subagent/catalog` 事件与投影、`sessionQuery.observeSession`、model-selection settings 链 mini 未复现（架构不适用）；`listChildren` 保留持久化 header 枚举。见 verified-diffs §2.69/§3.46。

- **模型澄清（user-questions）**：`miniharness/interaction/{user_questions,tool_ask_user}.py`（L3）+ `web/questions.py` 桥 + CLI 四入口 seam——`UserQuestionError` 稳定码集（ASK_ABORTED/EMPTY_QUESTIONS/CALLER_NOT_LIVE/DELEGATED_CALLER/BAD_INTENT/NO_PROVIDER/ASK_CANCELLED）、`user-questions/request` waterfall + 无应答者 fail-loud（`core/scope.py` `awaterfall` base 扩展形参）、`restore_user_question_error` 传输恢复、取消归一（在航 signal aborted → ASK_ABORTED）；`ask_user_question` 工具 schema/execute 逐字 + canonical `{"answers":[...]}`（render 返回紧凑 JSON 字符串，mini 全局工具 content 载体等价，见 verified-diffs §3.36）；plan 审查（plan/review.py）与 web 桥（`$events/result` 结算）消费同一 seam；工具只装配了服务处注册（web 组合与 demo），headless 仅装 seam。**测试**：test_user_questions 20 + test_ask_user_question 10 + test_web_user_questions 4 + test_plan_review 20。详见 verified-diffs §2.59/§3.36。

## 上游包观察清单（未复现，暂不纳入范围）

以下上游 `packages/` 包尚未复现，未来想扩充复现范围可从中挑选；多数属于"能力扩展口 + 消费工具"的延伸，核心约定不依赖它们。已实现的家族中也有只做了一部分切片的（如 subprocess 仅环境清洗、client 仅 ui-trajectory、host 为 apiproxy 子集），权威归属以 docs/architecture.md 映射表为准。

- **能力类**：`fs`、`e2b`、`lsp`、`code-runtime`、`spill`、`workspace`、`ssh`
- **编排类**：`workflow`、`schedule`、`todo`
- **横切类**：`settings`、`session-query`、`feedback`、`guard`、`runtime-diagnostics`、`util`、`web`（`api` 组已全部复现；`context` 组六件已复现）
- **平台类**：`typert`、`test-support`

官方 Python SDK（`python/sdk` 的 stdio JSON-RPC 客户端 + `python/sdk-runtime` 运行时）协议面已实现（`protocol/sdk.py`），互操作测试以官方 SDK 为目标（`tests/test_upstream_sdk_interop.py`，缺 pydantic/上游源码自动 skip），不再列观察清单。
