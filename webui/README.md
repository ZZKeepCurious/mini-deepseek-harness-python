# webui — MiniHarness 产品化浏览器前端

> 仓库顶层独立 React + TypeScript + Vite 工程（独立于 Python 内核）。
> **只依赖后端发布的 wire 契约**：两信封 RPC（`/api/<endpoint>`）+ `/api/remote.mux`
> WebSocket 承载 Remote 流 + `$events`/`$events/result` + `session.follow`/`session.control`。
> 禁止 import / hack Python 内部。契约权威参考：`../docs/interface-wire.md`。

## 功能面

会话列表 / 新建、Trajectory（虚拟化窗口 + Overview 折叠跳转 + 全文搜索）、审批瀑布、队列/作业面板。

## 环境要求

- Node.js 18+（Vite 5 / TS 5.7；建议 20 / 22+）
- pnpm（`corepack enable` 或 `npm i -g pnpm`）

## 常用命令（package.json scripts）

| 命令 | 说明 |
|---|---|
| `pnpm install` | 首次安装依赖 |
| `pnpm dev` | 开发服务器，把 `/api` 与 `/api/remote.mux` 代理到本机 Python 后端 |
| `pnpm build` | `tsc --noEmit` + `vite build` → `dist/` |
| `pnpm preview` | 本地预览 `dist/` 产物（不代理 `/api`，完整运行请走后端承载） |
| `pnpm typecheck` | 仅类型检查 |
| `pnpm test` / `pnpm test:watch` | vitest 测试（wire 层 + trajectory 模型/搜索/组件，mock fetch/WS） |

## 启动方式

### 1. 先起 Python web 后端

```sh
# 仓库根目录；后端缺省端口为 0（OS 分配），dev 代理默认指向 8899，请显式对齐：
MINIHARNESS_WEB_PORT=8899 python -m miniharness.cli --profile web
```

代理目标可用 `MINIHARNESS_WEBUI_PROXY` 覆盖（默认 `http://127.0.0.1:8899`，见 `vite.config.ts`）。

### 2a. 开发态（热更新）

```sh
pnpm dev
# 打开 http://localhost:5173/
```

Vite dev server 把 `/api`（RPC）与 `/api/remote.mux`（WS mux）代理到后端；仅这两条路径被代理，其余由前端自身处理。

### 2b. 生产态（构建产物由后端承载）

```sh
pnpm build                  # 产出 webui/dist/
# 回仓库根，让后端承载产物（相对路径按服务器启动目录解析）：
MINIHARNESS_WEBUI_DIST=webui/dist python -m miniharness.cli --profile web
```

浏览器打开后端打印的地址，页面及 /api 同源，无需代理。

### 认证

后端配置了 `MINIHARNESS_WEB_TOKEN` 时，页面须以 `?token=` 挂到 URL 访问，如
`http://127.0.0.1:5173/index.html?token=<token>`。逻辑见 `src/wire/auth.ts`：unary
请求带 `Authorization: Bearer`，mux WebSocket 把 token 拼进 URL。

## 目录结构（三层边界）

```
webui/
├── src/wire/      # 契约客户端层（纯 TS，可单测）：rpc / mux / follow / control / events / auth
├── src/app/       # React 编排 hooks（useBackend）
├── src/ui/        # 无状态展示组件（SessionList / Trajectory / ApprovalPanel / ControlPanel）
├── tests/         # vitest 单测（jsdom，mock fetch/WS）
├── vite.config.ts # dev 代理 + 构建 + vitest 配置
└── index.html     # Vite 入口
```

## 测试与类型检查

```sh
pnpm typecheck
pnpm test
```