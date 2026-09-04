"""web profile 启动器测试：uvicorn 心跳选项 + run_web 装配。

对齐 upstream gateway heartbeat 契约（alpha.1 复核批）：缺省 2s transport 级
Ping + 连续 2 周期无 Pong 判死（上游 MAX_MISSED_HEARTBEATS=2 → terminate；
mini 映射 ws_ping_interval=2 / ws_ping_timeout=4）。
"""
import unittest
from unittest import mock

from miniharness.core.scope import Context
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.launcher import (
    uvicorn_options,
    run_web,
    WS_HEARTBEAT_INTERVAL,
    WS_HEARTBEAT_TIMEOUT,
)


class UvicornOptionsTest(unittest.TestCase):
    def test_default_heartbeat_matches_gateway(self):
        # 上游 websocketHeartbeatIntervalMs @default 2000 + miss 2 → terminate
        self.assertEqual(WS_HEARTBEAT_INTERVAL, 2.0)
        self.assertEqual(WS_HEARTBEAT_TIMEOUT, 4.0)
        self.assertEqual(uvicorn_options(),
                         {"ws_ping_interval": WS_HEARTBEAT_INTERVAL,
                          "ws_ping_timeout": WS_HEARTBEAT_TIMEOUT})

    def test_custom_interval(self):
        self.assertEqual(uvicorn_options(15),
                         {"ws_ping_interval": 15, "ws_ping_timeout": WS_HEARTBEAT_TIMEOUT})

    def test_keepalive_only_mode(self):
        # timeout=None = 只保活不强制 Pong（旧注册行为，显式可选）
        self.assertEqual(uvicorn_options(15, None),
                         {"ws_ping_interval": 15, "ws_ping_timeout": None})

    def test_disabled(self):
        self.assertEqual(uvicorn_options(None), {})

    def test_invalid_interval(self):
        for bad in (0, -1, "x"):
            with self.assertRaises(ValueError):
                uvicorn_options(bad)
        with self.assertRaises(ValueError):
            uvicorn_options(2, 0)
        with self.assertRaises(ValueError):
            uvicorn_options(2, "x")


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
        self.assertEqual(kwargs["ws_ping_timeout"], WS_HEARTBEAT_TIMEOUT)


if __name__ == "__main__":
    unittest.main()