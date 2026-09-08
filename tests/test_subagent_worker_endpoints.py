"""worker 子进程端点的进程内覆盖测试（第 6 章子 agent 远程通道）。

`miniharness.seams.subagent.worker` 是 stdio worker 的进程入口；子进程运行
（test_upstream_sdk_interop）不进入 coverage 计数，这里以进程内喂 stdin /
截 stdout 的方式直接覆盖 run_acp_worker / run_sdk_worker / 端点分派与通知
渲染的确定性语义（不依赖真实子进程与上游 SDK）。

运行：python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock

from miniharness.seams.subagent.worker import (
    _acp_update_sink,
    _dispatch_acp,
    _error_frame,
    _write_stdout,
    run_acp_worker,
    run_sdk_worker,
)
from miniharness.protocol.acp import AcpRequestError, AcpServer


def _run(function, feed: str) -> list[str]:
    stdin = io.StringIO(feed)
    stdout = io.StringIO()
    with mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", stdout):
        function()
    return stdout.getvalue().splitlines()


class TestWriteHelpers(unittest.TestCase):
    def test_write_stdout_emits_flushed_line(self):
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            _write_stdout("hello")
        self.assertEqual(stdout.getvalue(), "hello\n")

    def test_error_frame_preserves_code_and_message(self):
        frame = json.loads(_error_frame("r1", -32000, "boom"))
        self.assertEqual(frame["error"], {"code": -32000, "message": "boom"})

    def test_acp_update_sink_writes_notification(self):
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            _acp_update_sink("s1", {"type": "turn/end"})
        frame = json.loads(stdout.getvalue().strip())
        self.assertEqual(frame["method"], "session/update")
        self.assertEqual(frame["params"]["sessionId"], "s1")


class TestDispatchAcp(unittest.TestCase):
    def test_unknown_method_maps_to_minus_32601(self):
        server = AcpServer()
        with self.assertRaises(AcpRequestError) as caught:
            _dispatch_acp(server, "bogus", {})
        self.assertEqual(caught.exception.code, -32601)

    def test_all_method_branches_roundtrip(self):
        server = AcpServer()
        initialize = _dispatch_acp(server, "initialize", {})
        self.assertEqual(initialize["protocolVersion"], 1)
        session = _dispatch_acp(
            server, "newSession", {"cwd": os.getcwd()})
        session_id = session["sessionId"]
        prompt = _dispatch_acp(server, "prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": "hi"}],
        })
        self.assertEqual(prompt, {"stopReason": "end_turn"})
        self.assertIsNone(_dispatch_acp(server, "cancel", {"sessionId": session_id}))
        self.assertIsNone(_dispatch_acp(server, "shutdown", {}))


class TestRunAcpWorker(unittest.TestCase):
    def _feed_for(self, permission: str) -> str:
        return "\n".join([
            "",
            "not-json",
            '{"jsonrpc":"2.0","id":"7","method":"initialize"}',
            '{"jsonrpc":"2.0","id":"99"}',  # no method → ignored
            json.dumps({"jsonrpc": "2.0", "id": "new",
                        "method": "newSession", "params": {"cwd": os.getcwd()}}),
            json.dumps({"jsonrpc": "2.0", "id": "bad",
                        "method": "newSession", "params": {"cwd": "relative"}}),
            json.dumps({"jsonrpc": "2.0", "id": "sd", "method": "shutdown"}),
            json.dumps({"jsonrpc": "2.0", "id": "uk", "method": "nope"}),
            "",
        ])

    def test_acp_worker_reject_policy_full_session(self):
        lines = _run(lambda: run_acp_worker("reject"), self._feed_for("reject"))
        frames = [json.loads(line) for line in lines if line.strip()]
        by_id = {frame["id"]: frame for frame in frames if "id" in frame}
        self.assertEqual(by_id["7"]["result"]["protocolVersion"], 1)
        self.assertIn("sessionId", by_id["new"]["result"])
        self.assertEqual(by_id["bad"]["error"]["code"], -32602)
        self.assertIn("cwd must be an absolute path", by_id["bad"]["error"]["message"])
        self.assertIsNone(by_id["sd"]["result"])
        self.assertEqual(by_id["uk"]["error"]["code"], -32601)

    def test_acp_worker_allow_policy_answers_allow_once(self):
        lines = _run(lambda: run_acp_worker("allow"), self._feed_for("allow"))
        frames = [json.loads(line) for line in lines if line.strip()]
        by_id = {frame["id"]: frame for frame in frames if "id" in frame}
        self.assertIn("sessionId", by_id["new"]["result"])


class TestRunSdkWorker(unittest.TestCase):
    def test_sdk_worker_roundtrip_emits_notifications_before_response(self):
        feed = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": "init",
                        "method": "initialize", "params": {"cwd": os.getcwd()}}),
            json.dumps({"jsonrpc": "2.0", "id": "p", "method": "session/prompt",
                        "params": {"sessionId": "s1",
                                   "contentBlocks": [{"type": "text", "text": "work"}]}}),
            json.dumps({"jsonrpc": "2.0", "id": "sd", "method": "shutdown"}),
            "",
        ])
        lines = _run(run_sdk_worker, feed)
        frames = [json.loads(line) for line in lines if line.strip()]
        methods = [frame.get("method") for frame in frames
                   if frame.get("method") is not None]
        by_id = {frame["id"]: frame for frame in frames if "id" in frame}
        self.assertEqual(by_id["init"]["result"]["serverInfo"]["name"],
                         "deepseek-harness-sdk-runtime")
        self.assertIn("messageId", by_id["p"]["result"])
        self.assertEqual(by_id["sd"]["result"], {})
        self.assertIn("session.event", methods)
        self.assertIn("session.status", methods)


if __name__ == "__main__":
    unittest.main()