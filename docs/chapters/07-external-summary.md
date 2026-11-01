[← 第 7 章导读](07-external-entry-points.md)

# 第 7 章 · 总表与检查点

## 7.9 一张表看懂全部入口 {#sec-7-9}

| 入口 | 对谁说话 | 载体 | mini 对应 |
|---|---|---|---|
| `dsh --profile web` | 人（浏览器） | HTTP + 浏览器客户端 | `miniharness/web/`（两信封 unary + WS mux + `webui/` React 前端，§7.5） |
| `dsh --profile headless "task"` | 人（shell 一次性任务） | 进程（stdout/退出码） | `miniharness/cli/headless.py` |
| `dsh --profile <自定义>` | 组合层自定义 | 任意 | （可经 boot/patch 扩展） |
| ACP 服务器 | 自动化程序 | stdio JSON-RPC | `miniharness/protocol/acp.py`（握手/会话/prompt/取消/审批桥） |
| JSON-RPC SDK | 编程客户端（官方 Python SDK 的线） | stdio JSON-RPC | `miniharness/protocol/sdk.py`（信封子集 + 三方法） |
| hooks 桥 | 用户既有 CC/Codex 钩子 | 子进程 | `miniharness/protocol/hooks.py`（CC 配置 → 四类拦截决策 + 审计配对） |

自定义 profile 是组合层的事情，不需要新的协议，只需要 `boot()` 已经支持的 patch 层叠——第 5 章的 `apply_patch` 承担这件事。

## 7.10 检查点

1. `python -m unittest tests.test_web_stream_protocol tests.test_web_server -v` 通过。
2. 能口述 unary / mux / `$events` 三泳道各解决什么问题（链 [wire-tour](../guides/wire-tour.md)）。
3. 任选 [T1 headless](../guides/traces/headless-one-task.md) 或 [T3 web unary](../guides/traces/web-unary-session.md) 走查一遍。
4. 产品 UI：`cd webui && pnpm test`（wire 层）；静态 dist 见 `tests/test_web_frontend.py` 中的 webui dist 构建测试。

## 7.11 回到 dsh：真实源码对照

对照 `packages/api/gateway`、`packages/client/connection` 与 `apps/cli` 的 profile/headless 语义。mini 入口组合见 [Fig S5](../system-map.md#fig-s5)；wire 走查 [T3](../guides/traces/web-unary-session.md)、[T4](../guides/traces/web-follow-approval.md)。
