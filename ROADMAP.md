# ROADMAP：MiniHarness 的方向与规划

> 项目目标：用纯 Python（stdlib only）从 0 到 1 复现 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的核心契约，逐模块对照上游（`deepseek-harness/`）解读与重写，最终完整落地后超越。
> 原则：每个阶段可独立运行、有测试、可演示；优先"约定正确"而非"功能齐全"。

## 已完成

核心约定已全部落地，能力清单见 README"已实现能力"表：事件溯源会话、持久化与崩溃恢复、插件事件总线、工具管线、Agent Loop、LLM 扩展口与重试、boot 组合、headless 入口与启动器选项、能力扩展口三件套（沙箱/凭据/子 agent）、外部协议入口（ACP/SDK/hooks）、异步并行、preset/干预/轨迹/动态插件/审批、token 计量与上下文压缩（pre-step 压力检查 0.8/0.16、overflow 强制减容重试、surface replace 检查点事务）、后台作业（job_output/job_list/job_kill 三工具 + 完成 notice，进程内注册表、无会话事件）、plan 最小版（log-only `plan/mode` 状态 + plan:policy 分节注入 + system prompt 分节服务）。发展史记录在工作区 `status/mini-harness/`。

## 规划中

当前主线：**Agent 层补齐**（依报告 04 议题 8/9 缺口，按依赖序推进）：

- **plan 审查 UI**（`/plan` 命令、`exit_plan_mode` 审查工具、session-projection 的 plan 投影单元、userQuestions）——上游 `packages/plan`（状态机本体已落地，审查 UI 后置）
- **goal**：goal round 驱动与快照校验——上游 `packages/goal`
- **skills**：先补报告专题解读，复现视解读结果定——上游 `packages/skill`
- **可继续子代理**（durable 子会话 + coldResume + followup 路由，后置）——上游 `packages/subagent`（continuation）

后置：

- **官方 Python SDK 互操作测试**：用 `deepseek-harness-sdk`（PyPI，stdio JSON-RPC 客户端）驱动真实 harness 子进程，对照协议约定做互操作验证（上游 `python/sdk` + `python/sdk-runtime`）。
- **web 表面**：`dsh web` 别名 + 浏览器半（上游 `packages/bundle/web-app`）；前端工程量最大，观察清单。

远期展望：插件示例集（教程用插件 + 真实工具演示）；多 agent 编排（子 agent 递归任务分解）；会话管理服务（多会话并行、ACL）；遥测（事件订阅、用量统计，`usage` chunk 已就绪）。

## 上游包观察清单（暂不纳入复现范围）

这些 `packages/` 包确认存在，未来想扩充复现范围可从中挑选；多数属于"能力扩展口 + 消费工具"的延伸，核心约定不依赖它们。

- **能力类**：`fs`、`shell`、`terminal`、`subprocess`、`web`、`lsp`、`mcp`、`code-runtime`、`storage`、`spill`、`workspace`
- **编排类**：`workflow`、`schedule`、`todo`、`preset`
- **横切类**：`interaction`、`settings`、`identity`、`hooks`、`acp`、`session-query`、`attachment`、`feedback`、`guard`、`runtime-diagnostics`、`host`、`extensions`、`client`
- **平台类**：`api`、`typert`、`sdk`、`bundle`、`test-support`
- **官方 Python SDK**：`python/sdk`（`deepseek-harness-sdk`，stdio JSON-RPC 客户端）+ `python/sdk-runtime`（`deepseek-harness-runtime-bin`，打包默认 agent 的运行时）——SDK 互操作测试以它为目标