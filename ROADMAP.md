# ROADMAP：从 0 到 1 复现 DeepSeek Harness（Python）

> 对照物：`deepseek-harness/` 真实源码 + `docs/report/` 报告的功能地图。
> 原则：每个阶段可独立运行、有测试、可演示；优先"约定正确"而非"功能齐全"。

图例：✅ 完成 · ◐ 部分完成 · ⏳ 待办

## 阶段 0：骨架与工程化 —— ◐

- [x] 仓库结构（包 / tests / docs / examples）
- [x] `pyproject.toml`（可 `pip install -e .`）
- [x] CLI 入口 `miniharness`（= `python -m miniharness.demo`）
- [x] README / ROADMAP / LICENSE / .gitignore
- [ ] GitHub Actions CI（`unittest` + Python 3.10~3.13 matrix）
- [ ] 真实 API 集成测试（打标签 `integration`，CI 可跳过）

## 阶段 1：会话地基（事件溯源）—— ✅

- [x] `Session` 追加重放日志：seq 单调、append 复制、unknown 拒绝
- [x] 回合事件携带 `turn` / `step` 编号（与上游 `SessionEvent` 字段一致，从 0 起）
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
- [ ] 重试/退避、上下文溢出降级（LlmFailure 编码已就绪）

对应 dsh：`core/agent-loop` + `llm/llm` + `llm/llm-deepseek`｜手册：04 章

## 阶段 5：组装（boot / 组合）—— ✅

- [x] `apply_patch` 补丁算法（replace / insert，纯函数）
- [x] `boot()`：配置加载 → 补丁层叠 → 插件激活 → 启动断言
- [ ] YAML 配置 + 插值（当前 JSON 简化版，见阶段 9）

对应 dsh：`packages/boot`｜手册：05 章

## 阶段 6：能力扩展口（进阶）—— ◐

- [x] 沙箱基础版：Passthrough / ReadOnly（deny-on-failure 约定）
- [x] 凭据基础版：EnvCredentialProvider（env-over-.env，按操作解析）
- [x] 子 agent 基础版：InProcessSubAgentProvider
- [ ] 真沙箱后端：Linux bwrap / Landlock、macOS Seatbelt、**Windows ACL runner**（上游 `sandbox/sandbox-local` 的四个后端；降级为"文档 + 约定测试"）
- [ ] 凭据多来源：`.env` 文件 / keyring / 提示注入（上游 local provider 的 `env` / `file` / `project-env` / `user-env` 四层）
- [ ] 子 agent 远程：fork / ACP / SDK 通道

对应 dsh：`docs/capability-seams.md` 各页｜手册：06 章

## 阶段 7：异步化（与 dsh 的最大差距）—— ⏳

- [ ] `asyncio` 化事件总线（emit/waterfall/parallel 的 async 版本）
- [ ] 真并行工具执行 + `ParallelBarrier`（并行失败即整体失败，与 dsh 语义一致）
- [ ] `is_concurrency_safe` 标记：安全工具并发，不安全工具串行
- [ ] 同步 API 保留（`run_sync` 包装），避免破坏现有测试

对应 dsh：`core/agent-loop` 的并行编排、`core/context` 的并发模型

## 阶段 8：CLI 与交互 —— ⏳

> 上游 `apps/dsh` 没有子命令式 CLI，而是 **profile 机制**：`dsh --profile headless "job"`（跑一个任务后退出）、`dsh web`（`--profile web` 的别名）、`dsh plugin --profile <name> <pnpm args>`。以下按同构对齐。

- [ ] `miniharness --profile headless "job"`：单任务模式（新会话 → 跑 → 打印最终答复退出）
- [ ] `miniharness web`：`--profile web` 别名（可先做终端 TUI 版）
- [ ] `miniharness sessions`：会话列表 / 恢复 / 删除
- [ ] `--config` / `--patch` 标志派发（复用 `apply_patch`）
- [ ] `--dump-config` / `--dump-default-config`（组合结果导出；上游两个标志都有）

对应 dsh：`apps/dsh`（profile 启动器 + `packages/boot/cmdline`）

## 阶段 9：配置与生态 —— ⏳

- [ ] YAML 配置（`pyyaml` 可选依赖）+ 插值
- [ ] 官方 SDK 互操作测试（`deepseek-harness-sdk` 驱动真实 harness 对照约定）
- [ ] 插件示例集（教程用插件 + 真实工具演示）

## 阶段 10：高级 —— ⏳

- [ ] 多 agent 编排（子 agent 递归任务分解）
- [ ] 会话管理服务（多会话并行、ACL）
- [ ] 遥测：事件订阅、用量统计（`usage` chunk 已就绪）

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
