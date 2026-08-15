# ROADMAP：从 0 到 1 复现 DeepSeek Harness（Python）

> 对照物：`deepseek-harness/` 真实源码 + `docs/report/` 报告的功能地图。
> 原则：每个阶段可独立运行、有测试、可演示；优先"约定正确"而非"功能齐全"。

图例：✅ 完成 · ◐ 部分完成 · ⏳ 待办

## 阶段 0：骨架与工程化 —— ✅

- [x] 仓库结构（包 / tests / docs / examples）
- [x] `pyproject.toml`（可 `pip install -e .`）
- [x] CLI 入口 `miniharness`（= `python -m miniharness.demo`）
- [x] README / ROADMAP / LICENSE / .gitignore
- [x] GitHub Actions CI（`unittest` + Python 3.10~3.13 matrix × ubuntu/windows）—— `.github/workflows/ci.yml`
- [x] 真实 API 集成测试（打标签 `integration`，CI 可跳过）—— `tests/test_real_api.py`（`MINIHARNESS_INTEGRATION=1` + `DEEPSEEK_API_KEY` 缺一即跳过）

## 阶段 1：会话地基（事件溯源）—— ✅

- [x] `Session` 追加重放日志：seq 单调、append 复制、unknown 拒绝
- [x] 回合事件携带 `turn` / `step` 编号（与上游一致：**从 1 起**，`session/invariant.ts` `nextTurn: 1, nextStep: 1`）
- [x] deep-freeze、is_json_safe、`derive_messages` 投影、`turn_balance` 硬性规定
- [x] `repair_interrupted_turn`（崩溃只补括号，不截断）
- [x] 持久化扩展口：JSONL / SQLite 双后端、flush 栅栏、fail-closed 加载、版本拒绝
- [x] 端到端演示（demo.py：回合 → 崩溃 → 修复 → 回放续聊）

对应 dsh：`packages/core/session`、`packages/session/session-persistence`｜手册：01、05 章

## 阶段 2：组合层（Context / 插件）—— ✅

- [x] `Context` 注册库：provide / inject / 作用域链
- [x] 事件总线：emit / waterfall（next 短路）/ parallel / serial
- [x] `PluginManager`：依赖驱动激活、失败即回滚（可逆副作用）
- [x] 作用域可见性解析（子 ctx 继承父注入，覆写隔离）

对应 dsh：`vendor/cordis` + `core/scope`｜手册：02 章

## 阶段 3：工具 —— ✅

- [x] 工具注册表（name / description / parameters schema）
- [x] `run_pipeline`：pre / execute / post 三段 waterfall + timeout 规范
- [x] `tool/call` 先记录后执行、`tool/result` 唯一模型面向
- [ ] 并发安全工具标记 + 真并行（挪到阶段 7）

对应 dsh：`packages/core/tools`｜手册：03 章

## 阶段 4：智能（Agent Loop + LLM）—— ✅

- [x] `AgentLoop`：turn/step 状态机、inbox、pre-step 拒绝（零 step turn）、工具回灌续跑、max_steps 守卫
- [x] `StreamChunk` 统一流协议（block-start / text-delta / tool-call-delta / usage / finish），字段与上游对齐（`blockType` / `text` / `argumentsDelta` 增量 / `finish.reason`）
- [x] `FakeLlmAdapter`（无 key 测试）、`DeepSeekAdapter`（官方 SSE，urllib 实现）
- [x] 重试/退避、上下文溢出降级：`retry_policy.py`（normal/always 解析 + 默认值：maxRetries 2、initial 500、max 10000、jitter 0.1、可重试码 [EMPTY_RESPONSE, RATE_LIMIT, SERVER, TIMEOUT, TRANSPORT]，严格校验 + 冻结）+ `llm_retry.py`（agent/request-error 恢复：previousRetry 计数 / retryId 复用 / providerRetryAfterMs（429 Retry-After 秒或 HTTP-date）优先 / 有界指数退避 + 对称抖动 / 可取消等待（同步轮询 signal）/ durable `llm/retry` + `llm/retry-started`）+ loop 接线（失败 attempt 重发同一请求，request/header 只落一次；CONTEXT_WINDOW_EXCEEDED / AUTH 不在默认白名单 → 终局不重试）；`LlmFailure` 扩展 status / providerRetryAfterMs / requestId（x-request-id / x-deepseek-request-id），socket 超时映射 TIMEOUT

对应 dsh：`core/agent-loop` + `llm/llm` + `llm/llm-deepseek`｜手册：04 章

## 阶段 5：组装（boot / 组合）—— ✅

- [x] `apply_patch` 补丁算法（replace / insert，纯函数）
- [x] `boot()`：配置加载 → 补丁层叠 → 插件激活 → 启动断言
- [x] YAML 配置 + 插值（`composition.py`：pyyaml 可选依赖、`!!js` 表达式子集、`.env` 加载、组合 dump 渲染 —— 阶段 9 已完成）

对应 dsh：`packages/boot`｜手册：05 章

## 阶段 6：能力扩展口（进阶）—— ✅

- [x] 沙箱基础版：Passthrough / ReadOnly（deny-on-failure 约定）
- [x] 凭据基础版：EnvCredentialProvider（env-over-.env，按操作解析）
- [x] 子 agent 基础版：InProcessSubAgentProvider
- [x] 真沙箱后端：`LocalSandboxProvider`——四后端 profile 生成器（bwrap / landlock / seatbelt / windows-acl）+ 平台链选择 + 功能探测仲裁 + fail-closed（`SandboxUnavailableError`/`SANDBOX_UNAVAILABLE`）+ `ConfinedArgv`（denial 方言 / runner 失败规则 / enforcement）+ runnerCommand 覆盖—— `miniharness/sandbox_local.py`（约定测试；真实二进制后端按 ROADMAP 降级）
- [x] 凭据多来源：`LocalCredentialProvider`——`env > file > project-env > user-env` 四层（env 只读胜出、file 可写管理、project 优先于 user）、严格文档解析（坏条目整体拒绝）、`describe`/`set`/`unset`、env 遮蔽写拒绝、POSIX owner-only 检查 —— `miniharness/credentials_local.py`（文档载体为 JSON，YAML 简化标注）
- [x] 子 agent 远程：fork（父日志 completed-turn 前缀 seed 继承上下文）/ ACP（真子进程 stdio 协议，permission 自动应答）/ SDK（真子进程 stdio JSON-RPC，session.event 通知收集输出）三通道 —— `miniharness/subagent_providers.py` + `subagent_worker.py`

对应 dsh：`docs/capability-seams.md` 各页｜手册：06 章

## 阶段 7：异步化（与 dsh 的最大差距）—— ✅

- [x] `asyncio` 化事件总线：`aemit` / `awaterfall` / `aparallel`（监听器同步/async 混用，`_maybe_await` 循环解包 `return nxt()` 链）—— `miniharness/bus.py`
- [x] 真并行工具执行 + `ParallelBarrier`：`schedule_tool_calls` 调度器（exclusive 单元素屏障 / parallel 有界滚动池 `max_parallel` 上限、政策段按模型序有序 await、execute 体线程池重叠、结果按模型序提交、池内重分类成屏障、abort 排干已启动 + 未启动补 `TOOL_ABORTED_BEFORE_DISPATCH` 合成错误、调度器失败排干并抛第一个错误不编造结果）—— `miniharness/scheduler.py`
- [x] `is_concurrency_safe` 标记：`Tool.is_concurrency_safe` 支持 bool 或 `Callable[[args], bool]`，`execution_mode` 仅精确 True 放行 parallel（未声明/False/抛错/非布尔 fail 到 exclusive），且不进模型 schema
- [x] 同步 API 保留：`AgentLoop.run` / `followup` 同步路径原样（228 测试不动），新增 `run_async` / `_pump_async` / `_run_step_async`；`run_pipeline` 同步管线重构为 `pipeline_policy` + `pipeline_body` + 规范化，`run_pipeline_async` 新管线（wait_for 超时 + 置位 signal + shield 排干）；`AgentLoop(max_parallel_tool_calls=10)` 对齐 `DEFAULT_MAX_PARALLEL_TOOL_CALLS`

对应 dsh：`core/agent-loop` 的并行编排（`tool-calls.ts`）+ `core/tools` 的 `executionMode` + `core/context` 的并发模型｜手册：12 章

## 阶段 8：CLI 与交互 —— ✅

> 上游 `apps/dsh` 没有子命令式 CLI，而是 **profile 机制**：`dsh --profile headless "job"`（跑一个任务后退出）、`dsh web`（`--profile web` 的别名）、`dsh plugin --profile <name> <pnpm args>`。以下按同构对齐。

- [x] `miniharness --profile headless "job"`：单任务模式（上游 `dsh-headless` bundle 语义：新会话 → 提交任务 → 停稳 flush → stdout 最后一条非空 assistant 文本 → completed 退出 0，否则 1；空任务 usage error）—— 见 `miniharness/headless.py` 与手册 07 章
- [x] `--profile` 未知名字 fail loud（上游只有 web/headless 两个模板自动初始化）
- [ ] `miniharness web`：`--profile web` 别名（web 表面未复现，观察清单）
- [x] `miniharness sessions`：会话列表 / 恢复（继续对话）/ 删除 —— `miniharness/sessions.py`（fail-closed 加载 + 崩溃修复 + 重放；上游无此 CLI，会话管理在 web 表层，标注教学扩展）
- [x] `--config` / `--patch` 标志派发（复用 `apply_patch`；`--patch` 可重复，对齐 args.ts；`--config` 为 mini 教学扩展，上游用 profile 目录机制）
- [x] `--dump-config` / `--dump-default-config`（组合结果导出；两者互斥、boot-free、dump 不接受任务参数、default 不接受 `--patch`/`--config`；行级 `# == <label>` 来源注释、`!!js` 原样未求值、skipped patch warn 不失败、输出单文档可再加载）—— `miniharness/composition.py`

对应 dsh：`apps/dsh`（profile 启动器 + `packages/boot/cmdline`）＋ `packages/bundle/headless`

## 阶段 9：配置与生态 —— ◐

- [x] YAML 配置（`pyyaml` 可选依赖）+ 插值：`miniharness/composition.py`（23 测试）——`.yaml/.yml` 与 `.json` 双载体；`!!js` tag → `{__jsExpr}` 节点（上游 `loadOverlayPatches` 语义），求值仅支持 `process.env.<NAME>` 完整匹配、其它表达式 fail loud（上游是 JS eval 全量表达式，mini 不求值 JS —— 简化标注）；config 顶层对象（plugins）或条目数组（dump 输出形态）、patch 顶层数组 + 条目对象校验；`load_dotenv_file`（对齐 `loadEnv`：ENOENT 静默、其它 warn、已存在 key 不覆盖，复用凭据层 `parse_dotenv`）；`render_composition_dump`（层叠 + 行级来源追踪 + `!!js` 原样 + skipped patch warn + 单文档可再加载，无 pyyaml 时退化为 JSON）；`boot()` 与 `--dump-config` 共用同一补丁算法
- [ ] 官方 SDK 互操作测试（`deepseek-harness-sdk` 驱动真实 harness 对照约定）
- [ ] 插件示例集（教程用插件 + 真实工具演示）

## 阶段 10：高级 —— ⏳

- [ ] 多 agent 编排（子 agent 递归任务分解）
- [ ] 会话管理服务（多会话并行、ACL）
- [ ] 遥测：事件订阅、用量统计（`usage` chunk 已就绪）

## 阶段 11：上游契约深度对齐（2026-08 逐条核对）—— ✅

> 以 `deepseek-harness` 源码为唯一权威逐模块核对后的对齐批次；实现载体（zstd、async）受 stdlib 限制的简化须在文档标注，语义不造假。

- [x] 事件信封 `{type, seq, time, data}`（surface 事件带 `surfaceOp` + 可选 `sourceEventSeqs`）
- [x] `assistant/message` 携带 `sourceEventSeqs` 引用来源 chunk（`assistant/chunk` seq 列表）
- [x] 消息模型 `{id, role, content: ContentBlock[], source}`，块五类：text / reasoning / image / tool-call / tool-result
- [x] ToolResultMessage role=`'user'`，DeepSeek wire 序列化展开为 `role:'tool'` + `tool_call_id`（空输出 `'(no output)'`）
- [x] finish / turn 结束 reason 为对象 `{kind: 'stop'|'tool-calls'|'max-tokens'|'aborted'|'error'|'blocked'|'interrupted'}`（pre-step 拒绝 → `{kind:'blocked'}`）
- [x] `step/end` 与 `turn/end` 在 finally 必定落日志（错误 / 阻塞也闭合）
- [x] 崩溃恢复升级为工具级：`TOOL_NOT_STARTED` / `TOOL_OUTCOME_UNKNOWN` 合成 error 结果 → `step/end` → `turn/end {kind:'interrupted'}`，时间戳复用最后真实事件
- [x] JSONL：header 行 + `SESSION_FORMAT_VERSION=0` fail-closed 校验、torn 尾部截断修复、恢复后补 `session/end-seed` 标记
- [x] DeepSeek SSE：字面 `[DONE]` 必须出现（EOF 未到则 `STREAM_CLOSED`）；HTTP 错误映射 401/403→AUTH、429→RATE_LIMIT、400 上下文→CONTEXT_WINDOW_EXCEEDED、500+→SERVER
- [x] 未知工具也先落 `tool/call` 再产出 error 结果（上游 `appendToolCall` 先于派发）
- [x] 工具超时改为排干语义（取消后等待线程退出再返回）

## 阶段 12：外部入口（两个表面 + 三个协议）—— ◔

> 上游外部入口全景见手册 07 章：两个产品 profile（web/headless）+ 三个协议入口（ACP / JSON-RPC SDK / hooks 桥）。headless 已完成（阶段 8），其余按复现价值排序。

- [x] **headless 一次性任务**（`dsh --profile headless "task"` 语义：stdout 最后一条非空 assistant 文本、退出码按 turn/end reason、空任务拒绝、不开端口）—— `miniharness/headless.py`
- [x] **JSON-RPC 信封最小子集**：newline-delimited JSON-RPC 2.0 帧三态（请求/响应/通知）、`req_`+uuid id 签发、-32601/-32603 错误码、畸形行忽略、`JsonRpcResponseError(code, data)`；最小运行服务 initialize / session/prompt（懒创建会话）/ shutdown —— `miniharness/sdk_protocol.py`（21 测试）｜手册 07 章 §7.6
- [x] **ACP 最小子集**：initialize（不宣称富媒体能力）/ newSession（cwd 绝对路径、拒绝 additionalDirectories 与 mcpServers）/ prompt（text+resource_link 限定、inflight 拒绝、stopReason 映射、error turn 拒绝）/ cancel（未知 no-op）/ 审批桥（callId 存在时 allow-once/reject-once 二选一）—— `miniharness/acp.py`（26 测试）｜手册 07 章 §7.7
- [x] **hooks 桥**：Claude Code 式 hooks 配置子集 → 四类拦截决策（pre_step 拒绝 / pre_tool deny|ask / post_tool block / stop 强制继续），匹配器（字面量管道/正则/match-all 哨兵）、退出码编解码（exit 2 → block）、最严格合并（deny>ask>allow、stop 粘住）、`${CLAUDE_PLUGIN_ROOT}`/`${CLAUDE_PROJECT_DIR}` 替换、无效 matcher fail-closed、`hook/invoked`+`hook/result` 审计配对 —— `miniharness/hooks.py`（40 测试）｜手册 07 章 §7.8
- [ ] web 表面：`dsh web` 别名 + 浏览器半（前端工程量最大，观察清单）

对应 dsh：`apps/cli` + `packages/bundle/{headless,web-app}` + `packages/{acp,sdk,hooks}`｜手册：07 章

## 阶段 13：组合层与干预面（2026-08 新增）—— ✅

> 对应报告 04 页议题 1/4/5 与手册 08/09 章：preset roster（会话级组合）、Agent 干预面（宿主侧操控）与审批（能力 seam）。

- [x] **preset roster 最小版**：目录列表即名单（filesystem discovery）、per-agent 挂载视图（host 注册表不动）、host 缺工具 fail loud、进程级服务冲突拒绝挂载 —— `miniharness/presets.py`（内置 standard/minimal 两预设，9 测试）｜手册 08 章
- [x] **Agent 干预面**：`steer`（下一 step 唤醒）/ `inject`（非唤醒）/ `cancel`（清 inbox + aborted 闭合，边界生效）/ `when_idle`（quiescence）/ `run_maintenance`（true idle 维护）—— `miniharness/loop.py`（11 测试）｜手册 09 章
- [x] **轨迹投影折叠引擎**：`turn/start` 驱动的 turn 摘要 + TTFT + tool-call 父子树 + partial 崩溃尾部标记 —— `miniharness/trajectory.py`（9 测试）｜手册 10 章
- [x] **动态插件进程内存生命周期**：define/run/stop/undefine + 检查族、进程级冲突 fail loud、重启不恢复 —— `miniharness/dynamic.py`（10 测试）｜手册 11 章
- [x] **审批最小版**：`ask/never` 两档策略（'never' 派发前确定性拒绝）+ 审计事件对 `approval/asked|decided`（turn-enclosed、log-only）+ `approval/policy` 可重放覆盖（纯 fold）—— `miniharness/approval.py`（18 测试）｜手册 09 章 §9.5-9.6

对应 dsh：`packages/preset` + `apps/cli/config/agent-presets` + `packages/core/agent` + `packages/extensions` + `packages/interaction/user-approval`

对应 dsh：`packages/preset` + `apps/cli/config/agent-presets` + `packages/core/agent`

## 观察清单（上游已有、暂不纳入复现范围的包）

> 这些 `packages/` 包确认存在，若未来想扩充复现范围可从中挑选；多数属于"能力扩展口 + 消费工具"的延伸，核心约定不依赖它们。

- **能力类**：`fs`（文件系统+策略）、`shell`（bash/pwsh 能力）、`terminal`（持久会话终端）、`subprocess`（进程树）、`web`（搜索/抓取）、`lsp`、`skill`、`mcp`、`code-runtime`、`storage`、`spill`、`workspace`
- **编排类**：`workflow`（worker-thread provider）、`jobs`、`goal`、`schedule`、`compaction`（上下文压缩）、`plan`（plan 模式）、`todo`、`preset`（按会话组合）
- **横切类**：`interaction`（审批/权限/ask-user）、`settings`、`identity`、`hooks`（Claude Code/Codex 桥）、`acp`（Agent Client Protocol 服务端）、`session-query`、`attachment`、`feedback`、`guard`（loop 卫生/工具超时）、`runtime-diagnostics`、`host`、`extensions`、`client`
- **平台类**：`api`（远程 BFF + Typert RPC）、`typert`（类型图生成器/注册表）、`sdk`（JSON-RPC 协议与服务端）、`bundle`（可安装 profile 补丁层）、`test-support`
- **官方 Python SDK**：`python/sdk`（`deepseek-harness-sdk`，stdio JSON-RPC 客户端）+ `python/sdk-runtime`（`deepseek-harness-runtime-bin`，打包默认 agent 的运行时）——阶段 9 的互操作测试以它为目标

---

## 怎么选下一步

- 想让仓库"更像产品"：阶段 0 剩余（CI）→ 阶段 8（CLI）
- 想让实现"对齐 dsh 语义"：阶段 7（异步 + 并行屏障）优先
- 想让教程"闭环"：阶段 6 的检查点练习（06 章）
