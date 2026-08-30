"""上游官方 Python SDK 互操作测试（第 14 步主线）。

用上游 `deepseek-harness/python/sdk` 的官方 SDK 客户端（`DeepSeekHarness` /
`HarnessClient`）通过 `_launch_args` 驱动 mini 的 stdio worker
（`python -m miniharness.seams.subagent.worker sdk`），验证 mini 服务端与
官方 SDK 客户端的 wire 契约完全互通。

两个可选前提（缺任一即 skip，不进默认 CI 门禁）：
  - pydantic>=2.12 已安装（上游 SDK 硬依赖）
  - 上游 SDK 源码可达：`MINIHARNESS_UPSTREAM_SDK` 环境变量指向
    `python/sdk/src`；缺省探测 `../deepseek-harness/python/sdk/src`
    （相对 mini 仓库根）。不可假设测试环境与工作区布局一致，
    找不到就 skip。

本地运行（Windows PowerShell 示例）：
  python -m pip install "pydantic>=2.12,<3"
  $env:MINIHARNESS_UPSTREAM_SDK = "path-to-upstream-python-sdk-src"
  python -m unittest discover -s tests -t . -p "test_upstream_sdk_interop.py"
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

MINI_ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_SDK = os.environ.get(
    "MINIHARNESS_UPSTREAM_SDK",
    str(MINI_ROOT.parent / "deepseek-harness" / "python" / "sdk" / "src"),
)

HAS_PYDANTIC = importlib.util.find_spec("pydantic") is not None
SDK_INIT = Path(UPSTREAM_SDK) / "deepseek_harness" / "__init__.py"
HAS_SDK = SDK_INIT.is_file()

if HAS_PYDANTIC and HAS_SDK:
    sys.path.insert(0, str(Path(UPSTREAM_SDK)))
    try:
        from deepseek_harness import DeepSeekHarness  # type: ignore[import-not-found]
        HAS_SDK = True
    except Exception:  # pragma: no cover - 导入失败按不可用处理
        HAS_SDK = False


@unittest.skipUnless(
    HAS_PYDANTIC and HAS_SDK,
    "需要 pydantic>=2.12 与上游 SDK 源码（MINIHARNESS_UPSTREAM_SDK）",
)
class TestUpstreamSdkInterop(unittest.TestCase):
    """官方 SDK 客户端驱动 mini worker 的全流程互操作验证。"""

    def _harness(self) -> DeepSeekHarness:
        return DeepSeekHarness(
            _launch_args=(sys.executable, "-m",
                          "miniharness.seams.subagent.worker", "sdk"),
            cwd=str(MINI_ROOT),
            runtime_cwd=str(MINI_ROOT),
        )

    def test_run_collects_final_response(self) -> None:
        harness = self._harness()
        try:
            result = harness.run("写个函数")
            self.assertEqual(result.final_response, "任务完成。")
            self.assertEqual(result.finish_reason, "completed")
            self.assertIsNotNone(result.session_id)
            types = [e.get("type") for e in result.events]
            self.assertIn("agent/inbox/spliced", types)
            self.assertIn("assistant/message", types)
            self.assertIn("turn/end", types)
        finally:
            harness.close()

    def test_run_emits_notification_sequence(self) -> None:
        harness = self._harness()
        try:
            seen = []
            harness.run("hello", on_notification=lambda n: seen.append(n.method))
            self.assertIn("session.event", seen)
            self.assertIn("session.status", seen)
            status = [n for n in seen if n == "session.status"]
            self.assertEqual(len(status), 1)
        finally:
            harness.close()

    def test_session_reuse_accumulates_turns(self) -> None:
        harness = self._harness()
        try:
            session = harness.start_session()
            first = session.run("第一问")
            second = session.run("第二问")
            self.assertNotEqual(first.session_id, "")
            self.assertEqual(second.finish_reason, "completed")
            turn_nums = [
                e.get("data", {}).get("turn")
                for e in second.events if e.get("type") == "turn/end"
            ]
            self.assertEqual(turn_nums, [2])
        finally:
            harness.close()

    def test_inbox_receipt_message_id_matches(self) -> None:
        """SDK Session.run 的 inbox 回执：inserted 含响应 messageId。"""
        harness = self._harness()
        try:
            notifications = []
            harness.run("hi", on_notification=notifications.append)
            spliced = [
                n.payload for n in notifications
                if n.method == "session.event"
                and n.payload.get("event", {}).get("type") == "agent/inbox/spliced"
            ]
            self.assertTrue(spliced)
            inserted = spliced[-1]["event"]["data"]["inserted"]
            self.assertTrue(
                any(isinstance(m, dict) and "id" in m for m in inserted),
                "inbox 回执必须含消息 id",
            )
        finally:
            harness.close()


if __name__ == "__main__":
    unittest.main()