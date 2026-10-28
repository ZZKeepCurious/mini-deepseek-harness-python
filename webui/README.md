# webui — MiniHarness 产品化浏览器前端

> 仓库顶层独立 React + TypeScript + Vite 工程（独立于 Python 内核）。
> **只依赖后端发布的 wire 约定**：两信封 RPC（`/api/<endpoint>`，含 `multipart/form-data`
> 的二进制结果附件）+ `/api/remote.mux`
> WebSocket 承载 Remote 流（含流的上行 `item`/`end`）+ `$events`/`$events/result` +
> `session.follow`/`session.control`。
> 禁止 import / hack Python 内部。约定权威参考：`../docs/interface-wire.md`。

## 功能面

会话列表 / 新建、Chat/Trajectory 双 tab、Trajectory（虚拟化窗口 + Overview 折叠跳转 + 全文搜索）、
MarkdownText（GFM 表格/代码、KaTeX 数学、安全外链、经 `/api/file` 的本地图片、搜索高亮）、
Composer（草稿持久化、14 行封顶、空白聚焦与选区恢复、`/` 命令、`@` 文件/会话引用、附件上传、
忙时 queue/steer）、工具卡（Bash/PowerShell 命令与输出、grep/glob 分组匹配与截断提示、
todo 计数/状态、read 行窗口、write/edit diff、web 搜索/抓取、图片元数据、未知工具通用回退）、
工作区文件树（`workspaceFiles/changes` 逐层实时刷新）与 text/markdown/code/image/PDF 预览、
xterm 终端（多 tab、shell 选择、重命名/关闭、写与尺寸 ack、断连重连）、
右侧栏 Console/Files/Terminal 三 tab 与 per-session 布局（宽度拖拽/键盘、收起、<768px 自动全屏、
中央「侧栏」展开按钮）、设置对话框（常规页权限缺省预设与完全权限确认门、浏览器外观/排版/发送偏好、
按值类型渲染的插件命名空间表单；写入带 revision 围栏、冲突后重读、`settings/document-updated` 自动刷新、
密钥只显存在性）、审批瀑布、队列编辑/插话/删除与作业面板
（rc.1 数据源：队列 = `session/control` 投影帧折叠出的 `inbox` 投影、作业 = `job/list` roster 流）。

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
| `pnpm test` / `pnpm test:watch` | vitest 测试（wire 层 + trajectory 模型/搜索/组件 + markdown 渲染 + composer/queue/tool-card/files/terminal/right-dock/settings 交互，mock fetch/WS） |

## 启动方式

### 1. 先起 Python web 后端

```sh
# 仓库根目录；后端缺省端口为 0（OS 分配），dev 代理默认指向 8899，这里显式设为 8899：
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
├── src/wire/      # 约定客户端层（纯 TS，可单测）：rpc / mux / json-value / follow / control / jobs / events / auth / composer / workspace / terminal / settings / credentials / permissions
├── src/app/       # React 编排 hooks（useBackend / useSettings / usePreferences）+ 浏览器偏好（uiPreferences）+ 右侧栏布局（rightDock：校验/夹取/持久化）
├── src/composer/  # Composer 与触发词/候选纯函数（triggers）
├── src/trajectory/ # 虚拟行模型、搜索索引、tool 结果解析（tool-views）
├── src/ui/        # 无状态展示组件（SessionList / Trajectory / ChatView / MarkdownText / ApprovalPanel / ControlPanel / FilesPanel / DocumentPreview / TerminalPanel / TerminalScreen / RightDock / SettingsDialog / SettingsGeneral / SettingsNamespaceForm）
├── src/ui/tools/   # ToolCallView：shell / search / todo / read / diff / web / image / 通用回退
├── tests/         # vitest 单测（jsdom，mock fetch/WS；wire-binary.test.ts 走 node 环境解析 multipart）
├── vite.config.ts # dev 代理 + 构建 + vitest 配置
└── index.html     # Vite 入口
```

### 流的上行（`src/wire/mux.ts`）

`openStream(endpoint, payload)` 返回 `StreamHandle`（同上游 `RemoteStreamHandle`）：
`send(item)` 发一条上行项（先过 `json-value.ts` 的无损 JSON 校验，失败即本地抛错）、
`endUplink()` 半关上行、`close()` 取消本流、`next()` 取下一帧下行。终态帧（`error`/`end`）
或连接断开后 `send()` 抛错——对齐上游「终态即停 pump / 断连 `failAll` 终结全部流」。

每次 `open` 的 `streamId` 由客户端铸**非空字符串**（`crypto.randomUUID()`，无 crypto 时退化为
时间戳+计数器）；上游 `stream-client.ts:120` 用 `randomUUID()`，数字 id 会被网关按协议违例关 WS 1008。

xterm 只在 `src/ui/TerminalScreen.tsx` 静态引入，`TerminalPanel` 动态加载它，因此构建会把 xterm
单独切块；引导态/错误态的组件测试不会拉入终端模拟器。

## 测试与类型检查

```sh
pnpm typecheck
pnpm test
```