# ROADMAP：MiniHarness 的方向与规划

> 项目目标：用 Python（成熟开源库优先，无语义等价库时才用标准库手写）从 0 到 1 复现 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的核心约定，逐模块对照上游（`deepseek-harness/`）解读与重写；目标是**面向产品化的生产就绪**——凡真实部署中构成可靠性/安全/互操作/体验实质风险的项，不因「教学/demo 可接受」豁免，按生产就绪标准补齐或立项。
> 原则：每个阶段可独立运行、有测试、可演示；优先"约定正确"而非"功能齐全"。

## 当前覆盖面

核心约定已全部落地，能力清单见 README「已实现能力」表；每个模块与上游包的权威归属见 [docs/architecture.md](docs/architecture.md) 映射表。

web 半（传输层 + 浏览器前端）的 wire 面已全对齐：两信封 RPC、Remote 流（单条 `/api/remote.mux` WebSocket 承载 `open/cancel/item/end/error` 帧 + `$events/result` unary 结算）、`$events` 注册表（api-session/* 转发 + `approval/request` waterfall 审批桥）、`session.follow`/`session.control` 流、静态服务契约、会话日志导出 `GET /api/session.export`（zip 打包 root + 子代理后代 + 被引用媒体）。上游 React 客户端指向 mini 后端可工作。浏览器前端二形态：产品化 `webui/`（仓库顶层独立 React+TS+Vite 工程，只依赖 wire 契约；`vite build` 产物经 `MINIHARNESS_WEBUI_DIST` 由后端静态承载）+ `web/static/` vanilla SPA 教学参照（旧 SSE wire，不对新后端工作）。设计与决策记录在 `status/mini-harness/`。

## 规划中

下一主线候选：插件示例集（教程用插件 + 真实工具演示）；多 agent 编排（子 agent 递归任务分解）；遥测（事件订阅、用量统计，`usage` chunk 已就绪）。

## 上游包观察清单（未复现，暂不纳入范围）

以下上游 `packages/` 包尚未复现，未来想扩充复现范围可从中挑选；多数属于"能力扩展口 + 消费工具"的延伸，核心约定不依赖它们。已复现家族中也有只落了切片的（如 subprocess 仅环境清洗、client 仅 ui-trajectory、host 为 apiproxy 子集），权威归属以 docs/architecture.md 映射表为准。

- **能力类**：`fs`、`terminal`、`e2b`、`lsp`、`mcp`、`code-runtime`、`storage`、`spill`、`workspace`
- **编排类**：`workflow`、`schedule`、`todo`
- **横切类**：`settings`、`identity`、`session-query`、`feedback`、`guard`、`runtime-diagnostics`、`api`、`context`、`util`、`web`
- **平台类**：`typert`、`test-support`

官方 Python SDK（`python/sdk` 的 stdio JSON-RPC 客户端 + `python/sdk-runtime` 运行时）协议面已复现（`protocol/sdk.py`），互操作测试以官方 SDK 为目标（`tests/test_upstream_sdk_interop.py`，缺 pydantic/上游源码自动 skip），不再列观察清单。
