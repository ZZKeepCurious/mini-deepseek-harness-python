"""MCP fixture server：供 mini mcp 集成测试驱动的真正 MCP server。

以官方 MCP SDK（mcp>=2,<3）的 MCPServer 实现真实子进程 / HTTP 端点：
- 工具：echo / add / do thing（含空格名 → 驱动 hash 命名）/ slow（可中断）/
  broken（恒抛错 → 服务端 isError 结果，驱动 isError 侧档）。
- 资源：test://greeting（dict + blob → 驱动渲染掩码）、test://greet/{name}
  （模板参数化 → 驱动 templates/list 与 read 路由）。
- 传输：--transport stdio（默认）| http（uvicorn + streamable_http_app，
  MCP SDK 2.x `MCPServer.streamable_http_app`）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys


def build_server() -> Any:
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name="fixture", instructions="fixture server instructions")

    @server.tool(name="echo")
    async def echo(message: str) -> str:
        return message

    @server.tool(name="add")
    async def add(a: int, b: int = 0) -> int:
        return a + b

    @server.tool(name="do thing")
    async def do_thing() -> str:
        return "done"

    @server.tool(name="slow")
    async def slow(ms: int) -> str:
        await asyncio.sleep(ms / 1000)
        return f"slept {ms}"

    @server.tool(name="broken")
    async def broken() -> str:
        raise RuntimeError("always broken")

    @server.resource("test://greeting", name="greeting")
    async def greeting() -> str:
        return "hello from fixture"

    @server.resource("test://greet/{name}", name="greet")
    async def greet(name: str) -> str:
        return f"hello {name}"

    @server.resource("test://blob", name="blob")
    async def blob() -> bytes:
        return b"\x00\x01\x02\x03\x04"

    return server


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(description="miniharness MCP fixture server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--die-after", type=float, default=0.0,
                        help="process exits hard after this many seconds (crash simulation)")
    args = parser.parse_args(argv)

    server = build_server()

    async def _run_stdio() -> None:
        if args.die_after and args.die_after > 0:

            async def _die_later() -> None:
                await asyncio.sleep(args.die_after)
                # 先向 stdout 写一行非 JSONRPC（"崩溃前乱码" 仿真）：
                # Python SDK stdio 传输以 parse 异常值投递超标行（见
                # mcp/client/stdio.py _parse_line），驱动客户端 fail-closed
                # 路径确定性触发；裸 os._exit 只关管道，SDK 端不会投递 EOF。
                print("crashing: not jsonrpc", flush=True)
                import os
                os._exit(0)  # noqa: PLR1722 - fixture 崩溃模拟：硬退最贴近进程死亡

            asyncio.create_task(_die_later())
        await server.run_stdio_async()

    if args.transport == "stdio":
        asyncio.run(_run_stdio())
    else:
        if args.die_after and args.die_after > 0:
            import threading

            def _die_thread() -> None:
                threading.Event().wait(args.die_after)
                import os
                os._exit(0)  # noqa: PLR1722 - 同上

            threading.Thread(target=_die_thread, daemon=True).start()
        import uvicorn

        app = server.streamable_http_app(streamable_http_path="/mcp")
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())