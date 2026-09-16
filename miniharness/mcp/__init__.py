"""mcp 族：外部 MCP 工具桥（mcp-client）+ MCP 资源工具（mcp-resources）。

对应 dsh 真实源码：packages/mcp/mcp-client 与 packages/mcp/mcp-resources。

族内拆分：
- types（配置 + 重连策略解析）、transport（stdio/streamable-http 传输面）、
  tools（工具桥：publicToolName / syncTools / createMcpToolDefinition / 图像
  投影）、connection（连接 supervisor：专属事件循环 + 守护线程 + 重连预算）、
  server_context（mcpResources provider + systemPrompt 节接线）、client
  （apply 装载：serverName 占位 + ready + failOnStartupError）——
  对应 mcp-client/src/{index,connection,server-context,tools,transport}.ts；
- resources（McpResourceRuntime/ResourceLayer + 三共享工具 + render），
  对应 mcp-resources/src/{index,tools,render}.ts；
- fixture_server（测试与 demo 用 MCPServer 2.x fixture，教学扩展）。

载体差异与简化（详见 verified-diffs）：Python SDK 无 JS Client 的
listChanged autoRefresh/versionNegotiation 选项——tools/list_changed 与
传输故障经 ClientSession 的 message_handler 观察；每连接一个专属 asyncio
loop + 守护线程承载 SDK 的 async 生命周期（上游单线程事件循环）。
"""