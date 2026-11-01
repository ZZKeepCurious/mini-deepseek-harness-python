[← 第 7 章导读](07-external-entry-points.md)

入口组合：[Fig S5](../system-map.md#fig-s5)。

# 第 7 章 · §7.4 三个协议入口

## 7.4 三个协议入口 {#sec-7-4}

除了两个产品表面，dsh 还有三个"对机器说话"的入口。ACP 与 SDK 各自是一个 profile（`acp` / `sdk`，§7.1 表），hooks 是挂在既有拦截点上的桥接层，不单独起进程。

### ACP：自动化专用协议

`packages/acp/acp/` 是 Agent Client Protocol 服务器（agentclientprotocol.com），跑在 **stdio JSON-RPC** 上，语义是"自动化专用"：程序化客户端创建全新 agent、发送文本提示、收集已提交的 assistant 文本、解析一次性权限请求、取消工作。它的读取模型是 ACP 的（`session/request_permission` 提供 allow/reject 二选一，客户端可以自动回答），所以它服务的是**另一个程序**，不是人。

仓库内最主要的客户端是 `subagent-acp`（`packages/subagent/subagent-acp`）：当主 agent 派生子 agent 时，如果 provider 选的是 ACP，子 agent 就通过这个协议跑在独立的 harness 进程里。这解释了第 6 章那六个子 agent provider 里 ACP 的位置——协议入口同时是子 agent 出口。

### JSON-RPC SDK：官方 SDK 的线

`packages/sdk/` 定义 JSON-RPC 协议与服务端；`python/sdk`（`deepseek-harness-sdk`）是 stdio JSON-RPC **客户端**，`python/sdk-runtime`（`deepseek-harness-runtime-bin`）是打包了默认组合的运行时二进制。`packages/examples/jsonrpc-demo` 演示同一个协议如何跑在部署方自己的插件树上（`cordis=` 指向自己的 cordis.yml 即可换组合，但要保留 jsonrpc-server 条目）。

它和 ACP 的区别：ACP 是单向自动化约定，SDK 是通用的消息信封协议（rpcId 签发、信封解包、SSE 帧解码等），上层可以再搭任何语义。上一节说的"headless 不开端口"，SDK 恰恰相反——它把 harness 暴露成一条可以编程驱动的线。

### hooks：把你已有的 Claude Code / Codex 钩子带进来

`packages/hooks/` 是两条桥：`hooks-claude-code` 和 `hooks-codex`。它们读取用户**既有**的 Claude Code 式 hook 配置（`hooks.json` 或 settings 的 `hooks` 键），把 `PreToolUse`、`UserPromptSubmit` 这类事件翻译成 harness 的类型化 Decision。公共的匹配器、退出码编解码、`ctx.shell` 执行、最严格合并都放在 `hook-protocol`，两条桥各只实现自己方言的 stdin 载荷与事件映射。

hooks 的价值在于迁移成本：已经写好 Claude Code 钩子（安全策略、工作流检查）的用户，把这些钩子原样带进 dsh，而不是在 harness 里重写一遍。注意它只能挂在 harness 的**既有拦截点**上，不是新的独立入口。

