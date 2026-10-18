"""web profile 启动器测试：uvicorn 心跳选项 + run_web 装配。

对齐 upstream gateway heartbeat 契约（alpha.1 复核批）：缺省 2s transport 级
Ping + 连续 2 周期无 Pong 判死（上游 MAX_MISSED_HEARTBEATS=2 → terminate；
mini 映射 ws_ping_interval=2 / ws_ping_timeout=4）。
"""
import os
import unittest
from unittest import mock

from miniharness.core.scope import Context
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.host import is_loopback_host, is_wildcard_host
from miniharness.web.launcher import (
    uvicorn_options,
    run_web,
    WS_HEARTBEAT_INTERVAL,
    WS_HEARTBEAT_TIMEOUT,
)


class HostClassificationTest(unittest.TestCase):
    def test_wildcard_rejected_by_value_not_text(self):
        # 上游 webserver.spec.ts:240 的全部等价拼写
        for host in ("0.0.0.0", "::", "::0", "0000::", "0::", "0:0:0:0:0:0:0:0",
                     "::0.0.0.0", "::0.0.0.0%lo", "::%lo", "::ffff:0.0.0.0",
                     "::ffff:0:0", "::ffff:0000:0000", "0:0:0:0:0:ffff:0:0",
                     "0:0:0:0:0:ffff:0.0.0.0", "::ffff:0.0.0.0%eth0"):
            self.assertIs(is_wildcard_host(host), True, host)

    def test_non_wildcard_hosts_pass(self):
        for host in ("127.0.0.1", "::1", "192.168.1.5", "localhost", "example.com"):
            self.assertIs(is_wildcard_host(host), False, host)

    def test_loopback_by_parsed_value(self):
        for host in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "127.0.0.1%lo"):
            self.assertIs(is_loopback_host(host), True, host)
        for host in ("192.168.1.5", "::127.0.0.1", "0.0.0.0", "localhost"):
            self.assertIs(is_loopback_host(host), False, host)


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

    def test_run_web_rejects_wildcard_host(self):
        # 通配加载期拒绝（上游 webserver index.ts:283）：绑全部接口暴露 RCE
        ctx = Context(name="test-launcher")
        try:
            with self.assertRaises(ValueError) as raised:
                run_web(FakeLlmAdapter(), None, ctx=ctx, host="0.0.0.0", port=0)
        finally:
            ctx.dispose()
        self.assertIn("unspecified (wildcard) address", str(raised.exception))

    def test_run_web_requires_token_on_non_loopback(self):
        # 生产纪律：非回环具体地址无 token → fail loud（web/auth.py 部署契约）
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MINIHARNESS_WEB_TOKEN", None)
            with self.assertRaises(ValueError):
                run_web(FakeLlmAdapter(), None, host="192.0.2.1", port=0)

    def test_run_web_non_loopback_with_token(self):
        with mock.patch("uvicorn.run") as run:
            run_web(FakeLlmAdapter(), None, host="192.0.2.1", port=0, token="t")
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()