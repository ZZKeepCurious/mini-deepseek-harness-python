"""web profile 启动器测试：uvicorn 心跳选项 + run_web 装配。

对齐 upstream `packages/host/webserver` 的监听契约：心跳 = transport 级 ping
（`ws_ping_interval=30`、`ws_ping_timeout=None` 不强制 Pong），只保活不杀连接。
"""
import unittest
from unittest import mock

from miniharness.core.scope import Context
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.launcher import uvicorn_options, run_web, WS_HEARTBEAT_INTERVAL


class UvicornOptionsTest(unittest.TestCase):
    def test_default_heartbeat_interval(self):
        self.assertEqual(uvicorn_options(),
                         {"ws_ping_interval": WS_HEARTBEAT_INTERVAL,
                          "ws_ping_timeout": None})

    def test_custom_interval(self):
        self.assertEqual(uvicorn_options(15),
                         {"ws_ping_interval": 15, "ws_ping_timeout": None})

    def test_disabled(self):
        self.assertEqual(uvicorn_options(None), {})

    def test_invalid_interval(self):
        for bad in (0, -1, "x"):
            with self.assertRaises(ValueError):
                uvicorn_options(bad)


class RunWebHeartbeatTest(unittest.TestCase):
    def test_run_web_passes_heartbeat_to_uvicorn(self):
        with mock.patch("uvicorn.run") as run:
            ctx = Context(name="test-launcher")
            try:
                run_web(FakeLlmAdapter(), None, ctx=ctx, host="127.0.0.1", port=0)
            finally:
                ctx.dispose()
        self.assertEqual(run.call_count, 1)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["ws_ping_interval"], WS_HEARTBEAT_INTERVAL)
        self.assertIsNone(kwargs["ws_ping_timeout"])


if __name__ == "__main__":
    unittest.main()