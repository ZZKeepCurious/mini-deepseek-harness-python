# ROADMAP：MiniHarness 的方向与规划

> 项目目标：用 Python（stdlib 优先，关键协议层精选第三方如 httpx）从 0 到 1 复现 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的核心契约，逐模块对照上游（`deepseek-harness/`）解读与重写，最终完整落地后超越。
> 原则：每个阶段可独立运行、有测试、可演示；优先"约定正确"而非"功能齐全"。

## 已完成

核心约定已全部落地，能力清单见 README"已实现能力"表：事件溯源会话、持久化与崩溃恢复、插件事件总线、工具管线、Agent Loop、LLM 扩展口与重试、boot 组合、headless 入口与启动器选项、能力扩展口三件套（沙箱/凭据/子 agent）、外部协议入口（ACP/SDK/hooks）、异步并行、preset/干预/轨迹/动态插件/审批、token 计量与上下文压缩（pre-step 压力检查 0.8/0.16、overflow 强制减容重试、surface replace 检查点事务）、后台作业（job_output/job_list/job_kill 三工具 + 完成 notice，进程内注册表、无会话事件）、plan（状态机 + 审查 UI：`/plan` 命令、`exit_plan_mode` 审查工具、userQuestions 通道、plan 投影单元）、命令表面（`command/run|done` 配对）、goal（`goal/change` 事件溯源 fold + GoalService + pull 式轮次驱动 + `get_goal`/`create_goal`/`update_goal` 三工具 + `/goal` 命令）、skills（分层注册表 + filesystem provider + skill 工具 + catalog-form 持久目录，报告 04 议题 10）、**可继续子代理**（`start_continuable`/`send_message` + durable 子会话 + 冷恢复 + 结算投递 + `send_message`/`interrupt_agent`/`list_agents` 控制工具；A7 同步阻塞子回合 → A8 升级为**异步事件驱动**：父有 driver 时投递即返回 message id、Activation 跨回合驻留、watchSettlement（when_idle_async + poke 竞速）、steer 批内合并、结算先于所有权释放、interrupt 授权矩阵（user/ancestor authority + 缺席 no-op）、disposal 竞速冷恢复重投不丢消息，无 driver 场景回退同步门面；生命周期事件 subagent/start|end（runId 配对 + epochStopReason/foldConsumedWork 终局折叠）、sendWaking/admitWaking 所有权记账（waiting/settled 判定）、初始 prompt 投递返回 {childId, messageId}、嵌套续跑（exec.agent 为授权与所有权主体，孙代结算通知投直属父）、finishDisposal 顺序含 flushFinalState best-effort）、**Agent 层补齐**（inbox 双队列 followup→next-turn / steer→next-step + `agent/inbox/spliced`、`agent/status`、`agent/error`、`agent/turn-stopping` serial/aserial、`request/header` canonical 形状 + `request/context`、`agent/request` waterfall、concludesTurn、fuseToolSignals 每工具独立熔合、system-prompt assemble/contexts/tools/variables 提供器 + `{{variable}}` 严格插值）、**会话管理服务**（`ctx.sessions`：create/prepare/enter/announce 生命周期、get/list、fork 五错误码、flush 并行检查点、`session/created|disposed|event|flush` 四事件，对齐上游 `packages/core/session` 的 manager 层；headless/demo/resume 已接入）、**官方 Python SDK 互操作**（用上游 `python/sdk` 的 `DeepSeekHarness` 经 `launch_args_override` 驱动 mini worker 子进程，验证 `Session.run` 全流程：inbox 回执 → assistant/message → turn/end → status idle → final_response/finish_reason；`tests/test_upstream_sdk_interop.py` 4 项，pydantic + 上游 SDK 源码可达时运行，缺则 skip）、**核心 asyncio 化**（LLM 流式 `stream(messages, tools, signal)` async 契约 + httpx 异步 SSE 传输（`_aiter_raced` 与 abort 事件竞速，真取消无遗留线程）+ `StreamAborted`；AgentLoop 单一 async 驱动 + `followup`/`steer` 同步门面（`asyncio.run` 瞬态事件循环）；协作式 `asyncio.Event` 取消（`_AbortProxy.event`）；retry/压缩链/agent-pre-step 监听器（plan/goal/skills）async 化——对齐上游"纯异步事件驱动"形态，工具执行体直接 await、同步工具函数经 `_maybe_await` 解包）、**web 传输层**（`miniharness/web/`：四象限 RPC 信封（39 码错误集）、WebApi unary 会话服务、mux/host SSE 事件流 + session/queue splice 重投影快照、FastAPI 载体对齐 `handler.ts` 状态码链、`--profile web` 启动器；浏览器前端留在观察清单）。发展史记录在工作区 `status/mini-harness/`。

**web 浏览器半**（2026-08-20，设计记录见 `status/mini-harness/design-web-browser-half.md`）：后端 wire 全对齐（上游 `host/apiproxy` 的 approval 通道 + `frontend-static`）——approval/requested|resolved mux 帧（每帧独立 rpcId）+ `POST /api/respond`（RpcReceipt 回执）+ 静态服务契约（遍历 403 / SPA 回退 200 / MIME / 405），上游客户端指向 mini 后端可工作；浏览器前端为 vanilla SPA（`web/static/`：Trajectory 折叠、审批面板 Allow once / Reject、命令/配置界面、队列/作业面板），React monorepo 复现标注教学简化。

## 规划中

下一主线候选：插件示例集（教程用插件 + 真实工具演示）；多 agent 编排（子 agent 递归任务分解）；遥测（事件订阅、用量统计，`usage` chunk 已就绪）。

## 上游包观察清单（暂不纳入复现范围）

这些 `packages/` 包确认存在，未来想扩充复现范围可从中挑选；多数属于"能力扩展口 + 消费工具"的延伸，核心约定不依赖它们。

- **能力类**：`fs`、`shell`、`terminal`、`subprocess`、`web`、`lsp`、`mcp`、`code-runtime`、`storage`、`spill`、`workspace`
- **编排类**：`workflow`、`schedule`、`todo`、`preset`
- **横切类**：`interaction`、`settings`、`identity`、`hooks`、`acp`、`session-query`、`attachment`、`feedback`、`guard`、`runtime-diagnostics`、`host`、`extensions`、`client`
- **平台类**：`api`、`typert`、`sdk`、`bundle`、`test-support`
- **官方 Python SDK**：`python/sdk`（`deepseek-harness-sdk`，stdio JSON-RPC 客户端）+ `python/sdk-runtime`（`deepseek-harness-runtime-bin`，打包默认 agent 的运行时）——SDK 互操作测试以它为目标