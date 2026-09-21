"""web terminal 域：unary 路由 + terminal/follow 流（对齐 typert gateway 契约）。"""

import asyncio
import os
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.seams.sandbox_policy import SandboxPolicyService
from miniharness.terminal_controller.index import install_terminal_controller
from miniharness.web.api import WebApi

from tests.test_terminal_controller_terminal import FakeBrowserHandle

CWD = os.getcwd()
SHELL = {"path": "/bin/bash", "name": "bash", "args": ["-i"]}


class WebTerminalTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="web-terminal-test")
        self.addCleanup(self.ctx.dispose)
        SandboxPolicyService(self.ctx, {"mode": "danger-full-access"})
        self.api = WebApi(self.ctx, FakeLlmAdapter())
        self.handles = []
        self.spawn_specs = []
        install_terminal_controller(
            self.ctx, {"maxCols": 200, "maxRows": 100, "maxInputBytes": 1000},
            spawn_terminal=self._spawn,
            resolve_shell_fn=lambda configured, signal=None: dict(SHELL))
        self.session_id = self._value(
            self.api.dispatch("session.create", "r0", {"cwd": CWD}))["sessionId"]

    def _spawn(self, spec):
        self.spawn_specs.append(spec)
        handle = FakeBrowserHandle()
        self.handles.append(handle)
        return handle

    def _value(self, response):
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]

    def _error(self, response):
        self.assertFalse(response["result"]["ok"])
        return response["result"]["error"]

    def test_terminal_routes_round_trip_through_the_controller(self):
        agent_id = self.session_id
        environment = self._value(self.api.dispatch(
            "terminal/environment", "t1", {"agentId": agent_id}))
        self.assertEqual(environment["maxCols"], 200)
        created = self._value(self.api.dispatch(
            "terminal/create", "t2",
            {"agentId": agent_id, "id": "terminal-1", "cols": 80, "rows": 24}))
        self.assertEqual(created["state"], "running")
        listed = self._value(self.api.dispatch(
            "terminal/list", "t3", {"sessionId": agent_id}))
        self.assertEqual([item["id"] for item in listed], ["terminal-1"])
        self._value(self.api.dispatch(
            "terminal/rename", "t4", {"agentId": agent_id, "id": "terminal-1",
                                      "title": "  logs  "}))
        self.assertEqual(self._value(self.api.dispatch(
            "terminal/list", "t5", {"sessionId": agent_id}))[0]["title"], "logs")
        self._value(self.api.dispatch(
            "terminal/close", "t6", {"agentId": agent_id, "id": "terminal-1"}))
        self.assertEqual(self._value(self.api.dispatch(
            "terminal/list", "t7", {"sessionId": agent_id})), [])

    def test_terminal_errors_map_to_remote_codes(self):
        agent_id = self.session_id
        self.api.dispatch("terminal/create", "c1",
                          {"agentId": agent_id, "id": "terminal-1", "cols": 80, "rows": 24})
        read_only = self._error(self.api.dispatch(
            "terminal/write", "c2", {"agentId": agent_id, "id": "terminal-1",
                                     "attachmentId": "writer", "data": "x"}))
        self.assertEqual(read_only["code"], "terminal/control-unavailable")
        self.assertEqual(read_only["details"], {"reason": "read-only"})
        bad_dims = self._error(self.api.dispatch(
            "terminal/create", "c3",
            {"agentId": agent_id, "id": "terminal-2", "cols": 1, "rows": 24}))
        self.assertEqual(bad_dims["code"], "gateway/internal")
        oversized = self._error(self.api.dispatch(
            "terminal/write", "c4", {"agentId": agent_id, "id": "terminal-1",
                                     "attachmentId": "writer", "data": "x" * 1001}))
        self.assertEqual(oversized["code"], "gateway/internal")

    def test_terminal_args_are_validated_at_the_boundary(self):
        missing = self._error(self.api.dispatch(
            "terminal/create", "a1", {"agentId": self.session_id, "id": "terminal-1"}))
        self.assertEqual(missing["code"], "gateway/arguments-invalid")
        unexpected = self._error(self.api.dispatch(
            "terminal/list", "a2", {"sessionId": self.session_id, "extra": True}))
        self.assertEqual(unexpected["code"], "gateway/arguments-invalid")

    def test_missing_controller_rejects_honestly(self):
        bare = Context(name="no-terminal")
        self.addCleanup(bare.dispose)
        api = WebApi(bare, FakeLlmAdapter())
        session_id = api.dispatch("session.create", "r", {"cwd": CWD})["result"]["value"]["sessionId"]
        response = api.dispatch("terminal/environment", "r2", {"agentId": session_id})
        self.assertEqual(self._error(response)["code"], "gateway/invocation-unavailable")

    def test_terminal_follow_streams_snapshot_then_output_then_state(self):
        agent_id = self.session_id
        self.api.dispatch("terminal/create", "s1",
                          {"agentId": agent_id, "id": "terminal-1", "cols": 80, "rows": 24})
        handle = self.handles[0]

        async def go():
            stream = self.api.gateway.open_stream(
                "terminal/follow", {"args": {"agentId": agent_id, "id": "terminal-1",
                                             "attachmentId": "browser-1"}})
            frames = [await stream.__anext__()]
            handle.output.emit_data(b"hello\r\n")
            frames.append(await stream.__anext__())
            handle.output.emit_end(None)
            frames.append(await stream.__anext__())
            await stream.aclose()
            return frames

        frames = asyncio.run(go())
        self.assertEqual(frames[0]["type"], "snapshot")
        self.assertEqual(frames[1], {"type": "output", "sequence": 1, "data": "hello\r\n"})
        self.assertEqual(frames[2]["type"], "state")
        self.assertEqual(frames[2]["info"]["state"], "exited")

    def test_terminal_follow_rejects_an_unknown_shell_identity(self):
        async def go():
            stream = self.api.gateway.open_stream(
                "terminal/follow", {"args": {"agentId": self.session_id, "id": "missing",
                                             "attachmentId": "browser-1"}})
            return await stream.__anext__()

        with self.assertRaises(Exception):
            asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
