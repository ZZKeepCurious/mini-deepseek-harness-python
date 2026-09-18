# ROADMAP：MiniHarness 的方向与规划

> 项目目标：用 Python（成熟开源库优先，无语义等价库时才用标准库手写）从 0 到 1 复现 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的核心约定，逐模块对照上游（`deepseek-harness/`）解读与重写；目标是**面向产品化的生产就绪**——凡真实部署中构成可靠性/安全/互操作/体验实质风险的项，不因「教学/demo 可接受」豁免，按生产就绪标准补齐或立项。
> 原则：每个阶段可独立运行、有测试、可演示；优先"约定正确"而非"功能齐全"。

## 覆盖范围

核心约定已全部实现，能力清单见 README「已实现能力」表；每个模块与上游包的权威归属见 [docs/architecture.md](docs/architecture.md) 映射表。

web 半（传输层 + 浏览器前端）的 wire 面与上游一致：两信封 RPC、Remote 流（单条 `/api/remote.mux` WebSocket 承载 `open/cancel/item/end/error` 帧 + `$events/result` unary 结算）、`$events` 注册表（api-session/* 转发 + `approval/request` waterfall 审批桥）、`session.follow`/`session.control` 流、静态服务约定、会话日志导出 `GET /api/session.export`（zip 打包 root + 子代理后代 + 被引用媒体）。上游 React 客户端指向 mini 后端可工作。浏览器前端有两种形态：产品化 `webui/`（仓库顶层独立 React+TS+Vite 工程，只依赖 wire 约定；`vite build` 产物经 `MINIHARNESS_WEBUI_DIST` 由后端静态承载）+ `web/static/` vanilla SPA 教学参照（旧 SSE wire，不对新后端工作）。设计与决策记录在 `status/mini-harness/`。

## 规划

下一主线候选：插件示例集（教程用插件 + 真实工具演示）；更多 agent 编排（agent-team 已实现）；遥测上报面（telemetry-capture / OpenTelemetry，需远端数据面）。

## 扩展能力

- **MCP 客户端 / 资源**：`miniharness/mcp/`（L3）——`apply(ctx, config)` 代连接 supervisor（stdio / streamable-http 统一，`tools/list_changed` 重同步、server instructions 字节上限 fail-loud、`failOnStartupError`）+ 指数退避重连（预算耗尽即停）+ 工具归属注册 `${server}.${name}#${hash}` + darkfrozen render / 图片受理投影 + mcp-resources 三个共享资源工具。**载体注记（SDK 2.2）**：SDK stdio 传输不投递子进程 EOF/退出（`_drain_stdout` 永久阻塞、read_stream 不关闭、在途 RPC 永挂）——mini 用有界 RPC 竞速（5s，对 `generation.lost` 中止）加常驻生命线 ping watchdog（15s 心跳 / 3s 超时），取代上游 within 取消令牌，避免子进程退出后握手流程阻塞；关闭屏障 5s 超时 fail-closed 防止重叠子进程。

- **storage 存储中心**：`miniharness/storage/`（L1）——hub + domain 数据形态 + JSON backend（`ctx.storage`）：hub 不碰 IO（backend 注册表 + `storage.backend.<name>` 生命周期服务键）；domain 带 schema 校验、单写链、持久后 `domain/changed` 事件发射、backup-and-skip 坏记录政策；JSON medium 支持 single 整文档与 per-record 一记录一文档（原子重写、legacy bootstrap、foreign 文档读缺位）。对应 `packages/storage`（storage hub + storage-domain + storage-json；storage-sqlite 不承载）。

- **遥测 / 用量统计**：`miniharness/telemetry/`（L2）——sessionStats + tokenUsage 真实投影 + per-turn 用量推导（`fold_session_stats` / `fold_token_usage` / `derive_turn_token_usage`，与上游 session-stats / token-meter 一致）+ opt-in `UsageStatsService`；wire `projections.values` 由空基线改为真实视图；CLI `sessions stats [id]` 教学扩展。

- **Agent Teams**：`seams/agent_team/` 隐式 root roster + durable peer mailbox + 共享任务 DAG（四类 `team/*` 事件全 log-only，Team Lead 会话为权威 journal）+ 模型侧 9 工具与 `team:policy` 提示节（同步门面 + 事件循环内 async 双投递载体）。

- **图片输入请求（DeepSeek Files API 执行簇）**：`miniharness/llm/deepseek_files/`（L1）——file-id/defaults/models/types/model-info（catalog 能力解析）+ image-tokens（provider vision-token 计算器逐字移植）+ request-pricing（`ImageRequestTarget` 路由目标 + offloaded/retained 定价）+ files-api（Chat Completions 与 Messages 双协议 httpx 传输）+ upload-index（`files-v3.json` + filelock 跨进程锁 + `os.replace` 原子发布）+ file-store（单飞共享上传 + 索引复用 + 配额恢复）+ request-files（stale-id 恰一次重试 + 规范化图片诊断）；`DeepSeekAdapter` 按目录宣称 image 输入并走 image-capable 请求路径（Files file-id 优先 → 解析失败整请求回退 inline base64）。载体差异：httpx 异步替代 fetch/FormData、filelock + `os.replace` 替代 dsh-atomic-write（见 verified-diffs §2.36）。

- **图像卸载决策（message 投影）**：`core/session/projections.py`（L0）+ `compaction/image_offload.py`（L2）——`image/offload` durable 事件记录要永久卸载的输入图片 occurrence（当前 surface 的 user/message 或 tool/result 节点、深度优先序号、严格递增 + 拆分校验 fail-closed）；`derive_messages` 经 `fold_projections` 把选中 occurrence 投影为不可变 offloaded 副本（身份保留），模型侧投影为占位文本；`offload_oldest_images` + `agent/request-error` 上的 `IMAGE_OFFLOAD_REQUIRED` surface 修复（不消耗重试预算、不落 retry 事件）对应上游 `compaction-image-offload`。会话格式目录新增 `read_released_header` / `encode_current_header` / `encode_current_event`（对应 `session-format-catalog`）。

## 上游包观察清单（未复现，暂不纳入范围）

以下上游 `packages/` 包尚未复现，未来想扩充复现范围可从中挑选；多数属于"能力扩展口 + 消费工具"的延伸，核心约定不依赖它们。已实现的家族中也有只做了一部分切片的（如 subprocess 仅环境清洗、client 仅 ui-trajectory、host 为 apiproxy 子集），权威归属以 docs/architecture.md 映射表为准。

- **能力类**：`fs`、`terminal`、`e2b`、`lsp`、`code-runtime`、`spill`、`workspace`、`ptc-runtime`
- **编排类**：`workflow`、`schedule`、`todo`
- **横切类**：`settings`、`session-query`、`feedback`、`guard`、`runtime-diagnostics`、`api`、`context`、`util`、`web`
- **平台类**：`typert`、`test-support`

官方 Python SDK（`python/sdk` 的 stdio JSON-RPC 客户端 + `python/sdk-runtime` 运行时）协议面已实现（`protocol/sdk.py`），互操作测试以官方 SDK 为目标（`tests/test_upstream_sdk_interop.py`，缺 pydantic/上游源码自动 skip），不再列观察清单。
