"""C20 MCP 组验收。运行：python -m pytest tests/test_mcp.py -xvs"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
import unittest

from unittest.mock import patch

from mcp.types import (
    CallToolResult,
    TextContent,
    Tool as McpSdkTool,
    ToolListChangedNotification,
)

from miniharness.core.scope import Context
from miniharness.core.tools import ToolExec, ToolRegistry
from miniharness.mcp.client import apply
from miniharness.mcp.connection import McpServerConnection
from miniharness.mcp.resources.render import render_resource_result
from miniharness.mcp.resources.runtime import McpResourceRuntime, install_mcp_resources
from miniharness.mcp.tools import (
    ToolBridgeOptions,
    _structured_to_dict,
    create_output,
    frost_equal,
    public_tool_name,
    sync_tools,
)
from miniharness.mcp.types import (
    RECONNECT_DEFAULTS,
    resolve_mcp_config,
    resolve_reconnect_policy,
)

_FIXTURE = [sys.executable, "-m", "miniharness.mcp.fixture_server"]
_BASE = {
    "transport": "stdio",
    "command": sys.executable,
    "args": ["-m", "miniharness.mcp.fixture_server"],
    "serverName": "fixture",
    "reconnect": {"enabled": False},
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def run(coro):
    return asyncio.run(coro)


# ---------- pure unit: naming & types ----------


class TestPublicName(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(public_tool_name("s", "x"), "mcp__s__x")

    def test_space_triggers_hash(self):
        name = public_tool_name("s", "do thing")
        self.assertIn("do_thing", name)
        self.assertRegex(name, r"_([0-9a-f]{12})$")
        self.assertTrue(len(name) <= 64)

    def test_long_truncates_with_hash(self):
        raw = "a" * 120
        name = public_tool_name("s", raw)
        self.assertLessEqual(len(name), 64)
        self.assertRegex(name, r"_([0-9a-f]{12})$")


class TestTypes(unittest.TestCase):
    def test_reconnect_defaults(self):
        out = resolve_reconnect_policy(None, "test")
        self.assertEqual(out, RECONNECT_DEFAULTS)

    def test_config_fail_loud(self):
        with self.assertRaises((ValueError, TypeError)):
            resolve_mcp_config({})


class TestCreateOutput(unittest.TestCase):
    def test_schema_content_required(self):
        s, _ = create_output("tool", None)
        self.assertIn("content", s["required"])

    def test_schema_structured(self):
        s, _ = create_output("t", {"type": "string"})
        self.assertIn("structuredContent", s["required"])

    def test_render_calls_extract(self):
        _, render = create_output("t", None)
        out = render({}, {"content": [{"type": "text", "text": "hi"}]})
        self.assertEqual(out, [{"type": "text", "text": "hi"}])


class TestFrostEqual(unittest.TestCase):
    def test_dict_deep(self):
        self.assertTrue(frost_equal({"a": [1, 2]}, {"a": [1, 2]}))
        self.assertFalse(frost_equal({"a": [1]}, {"a": [1, 2]}))


class TestStructuredToDict(unittest.TestCase):
    def test_pass_through(self):
        self.assertEqual(_structured_to_dict({"x": 1}), {"x": 1})


# ---------- offline sync_tools with stub client ----------


class TestSyncTools(unittest.TestCase):
    def _tool(self, name: str, description: str = "") -> McpSdkTool:
        return McpSdkTool(name=name, description=description, inputSchema={"type": "object"})

    def _stub(self, tools):
        class Client:
            server_capabilities = type("Caps", (), {"tools": True})()
            async def list_tools(self):
                return tools
        return Client()

    def test_registration(self):
        reg = ToolRegistry(Context())
        opts = ToolBridgeOptions("contain", "s", 1000)
        d = run(sync_tools(self._stub([self._tool("echo")]), reg.root, opts, {}))
        self.assertIn("mcp__s__echo", d)
        self.assertIsNotNone(reg.resolve("mcp__s__echo"))

    def test_hash_registration(self):
        reg = ToolRegistry(Context())
        opts = ToolBridgeOptions("contain", "s", 1000)
        d = run(sync_tools(self._stub([self._tool("do thing")]), reg.root, opts, {}))
        keys = list(d.keys())
        self.assertEqual(len(keys), 1)
        self.assertIn("do_thing", keys[0])

    def test_contain_on_conflict(self):
        reg = ToolRegistry(Context())
        opts = ToolBridgeOptions("contain", "s", 1000)
        reg.register(type("T", (), {"name": "mcp__s__x"})())
        d = run(sync_tools(self._stub([self._tool("x")]), reg.root, opts, {}))
        self.assertEqual(d, {})
        # 前一代/外部注册保持占据命名空间（sync 回滚，绝不部分换代）
        self.assertIsNotNone(reg.resolve("mcp__s__x"))

    def test_throw_on_conflict(self):
        reg = ToolRegistry(Context())
        opts = ToolBridgeOptions("throw", "s", 1000)
        reg.register(type("T", (), {"name": "mcp__s__x"})())
        with self.assertRaises(RuntimeError):
            run(sync_tools(self._stub([self._tool("x")]), reg.root, opts, {}))

    def test_swap_removes_old(self):
        reg = ToolRegistry(Context())
        opts = ToolBridgeOptions("contain", "s", 1000)
        d1 = run(sync_tools(self._stub([self._tool("a"), self._tool("b")]), reg.root, opts, {}))
        self.assertIn("mcp__s__a", d1)
        d2 = run(sync_tools(self._stub([self._tool("a")]), reg.root, opts, d1))
        self.assertNotIn("mcp__s__b", d2)
        self.assertIsNone(reg.resolve("mcp__s__b"))

    def test_no_capabilities_skips(self):
        reg = ToolRegistry(Context())
        opts = ToolBridgeOptions("contain", "s", 1000)
        class NoCaps:
            server_capabilities = None
        d = run(sync_tools(NoCaps(), reg.root, opts, {}))
        self.assertEqual(d, {})


# ---------- resources render ----------


class TestResourceRender(unittest.TestCase):
    def test_mask_blob(self):
        val = [{"type": "blob", "blob": "aGVsbG8=", "mimeType": "application/octet-stream"}]
        out = render_resource_result("s", val)
        self.assertIn("binary resource: 8 base64 characters", out)
        self.assertNotIn("aGVsbG8=", out)

    def test_nested_mask(self):
        out = render_resource_result("s", {"nested": {"blob": "abc"}})
        self.assertIn("binary resource:", out)


# ---------- resources runtime ----------


class TestResourcesRuntime(unittest.TestCase):
    def test_register_tools_on_first(self):
        ctx = Context()
        reg = ToolRegistry(ctx)
        rt = install_mcp_resources(ctx)
        provider = type("P", (), {"request": lambda *a: None})()
        disposer = rt.register("x", provider)
        self.assertIn("list_mcp_resources", reg.names())
        self.assertEqual(rt.server_names(), ["x"])
        disposer()
        self.assertEqual(rt.server_names(), [])


# ---------- fixture e2e (stdio) ----------


class TestFixtureConnect(unittest.TestCase):
    def test_echo(self):
        reg = ToolRegistry(Context())
        conn = run(apply(reg.root, _BASE))
        try:
            tool = reg.resolve("mcp__fixture__echo")
            self.assertIsNotNone(tool)
            res = run(tool.execute({"message": "hi"}, ToolExec(name="x")))
            self.assertIn("hi", res["content"][0]["text"])
        finally:
            conn.dispose()

    def test_add(self):
        reg = ToolRegistry(Context())
        conn = run(apply(reg.root, _BASE))
        try:
            tool = reg.resolve("mcp__fixture__add")
            res = run(tool.execute({"a": 1, "b": 2}, ToolExec(name="x")))
            self.assertEqual(res["content"][0]["text"], "3")
        finally:
            conn.dispose()

    def test_hashed_name(self):
        reg = ToolRegistry(Context())
        conn = run(apply(reg.root, _BASE))
        try:
            names = reg.names()
            self.assertTrue(any("do_thing" in n for n in names))
        finally:
            conn.dispose()

    def test_isError_tool(self):
        reg = ToolRegistry(Context())
        conn = run(apply(reg.root, _BASE))
        try:
            tool = reg.resolve("mcp__fixture__broken")
            with self.assertRaises(RuntimeError):
                run(tool.execute({}, ToolExec(name="x")))
        finally:
            conn.dispose()

    def test_instructions(self):
        conn = run(apply(ToolRegistry(Context()).root, _BASE))
        try:
            self.assertIn("fixture server instructions", conn.instructions())
        finally:
            conn.dispose()

    def test_serverName_conflict(self):
        ctx = Context()
        ToolRegistry(ctx)
        conn1 = run(apply(ctx, _BASE))
        try:
            with self.assertRaises(ReferenceError):
                run(apply(ctx, _BASE))
        finally:
            conn1.dispose()

    def test_missing_command_fail_loud(self):
        with self.assertRaises((ValueError, TypeError)):
            run(apply(Context(), {"transport": "stdio", "serverName": "x"}))


# ---------- fixture e2e (streamable-http, uvicorn) ----------

try:
    import uvicorn  # noqa: F401
    _HAS_HTTP = True
except Exception:  # noqa: BLE001
    _HAS_HTTP = False


@unittest.skipUnless(_HAS_HTTP, "streamable-http 传输需 uvicorn")
class TestFixtureHTTP(unittest.TestCase):
    def test_list_tools(self):
        port = _free_port()
        proc = subprocess.Popen(_FIXTURE + ["--transport", "http", "--port", str(port)])
        try:
            for _ in range(80):
                try:
                    import httpx
                    httpx.get(f"http://127.0.0.1:{port}/mcp", timeout=0.2)
                    break
                except Exception:
                    time.sleep(0.1)
            else:
                self.skipTest("fixture http did not start")
            ctx = Context()
            reg = ToolRegistry(ctx)
            config = {
                "transport": "streamable-http",
                "url": f"http://127.0.0.1:{port}/mcp",
                "serverName": "fixture_http",
                "reconnect": {"enabled": False},
            }
            conn = run(apply(ctx, config))
            try:
                self.assertIn("mcp__fixture_http__echo", reg.names())
            finally:
                conn.dispose()
        finally:
            proc.terminate()
            proc.wait(5)


# ---------- disconnect & reconnect ----------


class TestDisconnectReconnect(unittest.TestCase):
    @patch("miniharness.mcp.connection._LIFELINE_INTERVAL_S", 0.3)
    def test_die_after_triggers_reconnect_disabled(self):
        ctx = Context()
        reg = ToolRegistry(ctx)
        config = {**_BASE, "serverName": "crash",
                  "args": ["-m", "miniharness.mcp.fixture_server", "--die-after", "0.2"],
                  "reconnect": {"enabled": False},
                  "toolCallTimeoutMs": 500}
        try:
            conn = run(apply(ctx, config))
            # fixture 进程启动较慢（import mcp SDK ≈1.5s），崩溃在 apply 之后；
            # 生命线 ping 在 ~0.3s 内暴露死亡 → generation_down → stopped
            deadline = time.monotonic() + 10
            while conn._state != "stopped" and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertEqual(conn._state, "stopped")
        finally:
            conn.dispose()

    @patch("miniharness.mcp.connection._LIFELINE_INTERVAL_S", 0.3)
    def test_budget_exhaustion(self):
        ctx = Context()
        reg = ToolRegistry(ctx)
        config = {**_BASE, "serverName": "crash2",
                  "args": ["-m", "miniharness.mcp.fixture_server", "--die-after", "0.05"],
                  "reconnect": {"enabled": True, "initialDelayMs": 10,
                                "maxDelayMs": 5000, "maxAttempts": 3},
                  "toolCallTimeoutMs": 200}
        try:
            conn = run(apply(ctx, config))
            deadline = time.monotonic() + 15
            while (conn._state != "stopped" or conn._disposers) \
                    and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertEqual(conn._state, "stopped")
            self.assertEqual(conn._disposers, {})
        finally:
            conn.dispose()


# ---------- tools/list_changed resync (inject via _on_message) ----------


class TestToolsChangedResync(unittest.TestCase):
    def test_resync_called(self):
        ctx = Context()
        reg = ToolRegistry(ctx)
        conn = run(apply(ctx, _BASE))
        try:
            before = conn._disposers
            self.assertTrue(before)
            gen = conn._generation
            self.assertIsNotNone(gen)
            # 注入 tools/list_changed → wait_for_life 消费后 re-sync（fetch same list）
            self.assertTrue(gen.tools_dirty.is_set() is False)
            run_coro = asyncio.run_coroutine_threadsafe(
                conn._on_message(ToolListChangedNotification()), conn._loop)
            run_coro.result(3)
            deadline = time.monotonic() + 4
            while conn._disposers is before and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIsNot(conn._disposers, before)
        finally:
            conn.dispose()


# ---------- instruction byte limit ----------


class TestInstructionByteLimit(unittest.TestCase):
    def test_too_small_rejects(self):
        ctx = Context()
        ToolRegistry(ctx)
        config = {**_BASE, "maxInstructionBytes": 5, "failOnStartupError": True}
        with self.assertRaises(RuntimeError):
            run(apply(ctx, config))


if __name__ == "__main__":
    unittest.main()