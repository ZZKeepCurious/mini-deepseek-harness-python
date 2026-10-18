"""BrowserTerminal：屏幕连续性、输入控制与进程所有权（对齐 terminal-controller/tests/terminal.spec.ts）。"""

import unittest

from miniharness.terminal.types import Cancellation
from miniharness.terminal_bash import SubprocessOutcome
from miniharness.terminal_controller.terminal import BrowserTerminal

INFO = {"id": "terminal-test", "title": "bash",
        "shell": {"path": "/bin/bash", "name": "bash", "args": ["-i"]},
        "cwd": "/workspace", "cols": 80, "rows": 24, "state": "running", "exitCode": None}


class FakeOutputChannel:
    def __init__(self):
        self._on_data = None
        self._on_end = None
        self._on_error = None
        self._ended = False

    def subscribe(self, on_data, on_end=None, on_error=None):
        self._on_data = on_data
        self._on_end = on_end
        self._on_error = on_error

    def emit_data(self, data):
        if not self._ended:
            self._on_data(bytes(data))

    def emit_end(self, outcome):
        if not self._ended:
            self._ended = True
            if self._on_end is not None:
                self._on_end(outcome)

    def emit_error(self, error):
        if not self._ended:
            self._ended = True
            if self._on_error is not None:
                self._on_error(error)

    def wait_end(self, timeout=None):
        return self._ended


class FakeBrowserHandle:
    """终止时结束输出并结算退出（对齐上游 spec 的 PassThrough fixture）。"""

    def __init__(self, pid=123, exit_code=0):
        self.pid = pid
        self.output = FakeOutputChannel()
        self.written = []
        self.resized = []
        self.terminate_calls = 0
        self.write_failures = []
        self.resize_failures = []
        self.on_terminate = None
        self.exit_code = exit_code

    def write(self, text):
        if self.write_failures:
            raise self.write_failures.pop(0)
        self.written.append(text)

    def resize(self, cols, rows):
        if self.resize_failures:
            raise self.resize_failures.pop(0)
        self.resized.append((cols, rows))

    def inspect_foreground(self):
        return None

    def signal_foreground(self, sig):
        return self.pid

    def terminate(self):
        self.terminate_calls += 1
        if self.on_terminate is not None:
            self.on_terminate()
            return
        self.output.emit_end(SubprocessOutcome(self.exit_code, None))


LIMITS = {"maxInputBytes": 64 * 1024, "maxCols": 500, "maxRows": 200}


def attach(terminal, signal=None, input_window=None):
    return terminal.attach(LIMITS, signal, input_window)


class TestBrowserTerminal(unittest.TestCase):
    def fixture(self, **kwargs):
        handle = FakeBrowserHandle(**kwargs)
        terminal = BrowserTerminal(handle, dict(INFO), 100, 100_000)
        return terminal, handle

    def test_restores_screen_after_detach_without_replaying_duplicate_output(self):
        terminal, handle = self.fixture()
        first = attach(terminal)
        self.assertEqual(first.baseline["type"], "snapshot")
        self.assertEqual(first.baseline["sequence"], 0)
        handle.output.emit_data(b"hello\r\n")
        self.assertEqual(first.follower.pop(),
                         {"type": "output", "sequence": 1, "data": "hello\r\n"})
        first.detach()
        self.assertEqual(handle.terminate_calls, 0)
        handle.output.emit_data(b"world")
        self.assertEqual(terminal.info["state"], "running")
        second = attach(terminal)
        frames = [second.baseline]
        if "world" not in second.baseline["screen"]:
            frames.append(second.follower.pop())
        rendered = str(frames)
        self.assertIn("world", rendered)
        self.assertIn("hello", rendered)
        self.assertEqual(handle.terminate_calls, 0)

    def test_preserves_split_utf8_and_moves_input_control_to_newest_attachment(self):
        terminal, handle = self.fixture()
        first = attach(terminal)
        encoded = "终端".encode("utf-8")
        handle.output.emit_data(encoded[:2])
        self.assertIsNone(first.follower.pop())
        handle.output.emit_data(encoded[2:])
        self.assertEqual(first.follower.pop(),
                         {"type": "output", "sequence": 1, "data": "终端"})
        second = attach(terminal)
        self.assertEqual(first.follower.pop()["type"], "state")
        first.submit({"type": "write", "sequence": 1, "data": "ignored"})
        self.assertEqual(first.follower.pop(),
                         {"type": "ack", "sequence": 1, "refusal": "read-only"})
        second.submit({"type": "write", "sequence": 1, "data": "\t"})
        self.assertEqual(handle.written, ["\t"])
        second.submit({"type": "resize", "sequence": 2, "cols": 100, "rows": 30})
        self.assertEqual(handle.resized, [(100, 30)])
        self.assertEqual(terminal.info["cols"], 100)
        self.assertEqual(terminal.info["rows"], 30)
        second.detach()
        self.assertFalse(second.writable)

    def test_keeps_exit_facts_and_screen_until_explicit_cleanup(self):
        terminal, handle = self.fixture()
        first = attach(terminal)
        handle.output.emit_data(b"done")
        handle.output.emit_end(SubprocessOutcome(7, None))
        self.assertEqual(first.follower.pop()["data"], "done")
        state = first.follower.pop()
        self.assertEqual(state["type"], "state")
        self.assertEqual(state["info"]["state"], "exited")
        self.assertEqual(state["info"]["exitCode"], 7)
        self.assertEqual(handle.terminate_calls, 0)
        second = attach(terminal)
        self.assertEqual(second.baseline["info"]["state"], "exited")
        self.assertEqual(second.baseline["info"]["exitCode"], 7)
        terminal.close()
        self.assertEqual(handle.terminate_calls, 1)

    def test_drains_final_output_and_exit_state_before_closing_followers(self):
        terminal, handle = self.fixture()
        first = attach(terminal)

        def terminate():
            handle.output.emit_data(b"FINAL OUTPUT\r\n")
            handle.output.emit_end(SubprocessOutcome(0, None))

        handle.on_terminate = terminate
        terminal.close()
        self.assertEqual(first.follower.pop()["data"], "FINAL OUTPUT\r\n")
        self.assertEqual(first.follower.pop()["info"]["state"], "exited")
        self.assertTrue(first.follower.finished)

    def test_rejects_input_after_exit_with_a_refusal_acknowledgement(self):
        terminal, handle = self.fixture()
        first = attach(terminal)
        handle.output.emit_end(SubprocessOutcome(0, None))
        self.assertEqual(first.follower.pop()["info"]["state"], "exited")
        first.submit({"type": "write", "sequence": 1, "data": "ignored"})
        self.assertEqual(first.follower.pop(),
                         {"type": "ack", "sequence": 1, "refusal": "not-running"})
        first.submit({"type": "resize", "sequence": 2, "cols": 100, "rows": 30})
        self.assertEqual(first.follower.pop()["refusal"], "not-running")
        self.assertEqual(handle.written, [])
        self.assertEqual(handle.resized, [])

    def test_preserves_input_controller_when_an_older_follower_detaches(self):
        terminal, handle = self.fixture()
        first = attach(terminal)
        second = attach(terminal)
        first.detach()
        terminal.rename("build output")
        state = second.follower.pop()
        self.assertEqual(state["info"]["title"], "build output")
        self.assertTrue(state["writable"])
        second.submit({"type": "write", "sequence": 1, "data": "pwd\r"})
        self.assertEqual(handle.written, ["pwd\r"])

    def test_does_not_grant_input_to_attachments_cancelled_before_snapshot(self):
        terminal, handle = self.fixture()
        aborted = Cancellation()
        aborted.abort(RuntimeError("already detached"))
        with self.assertRaisesRegex(RuntimeError, "already detached"):
            attach(terminal, aborted)
        first = attach(terminal)
        self.assertTrue(first.writable)

    def test_continues_accepting_operations_after_provider_write_or_resize_fails(self):
        terminal, handle = self.fixture()
        first = attach(terminal)
        handle.write_failures.append(RuntimeError("input transport failed"))
        handle.resize_failures.append(RuntimeError("resize transport failed"))
        with self.assertRaisesRegex(RuntimeError, "input transport failed"):
            first.submit({"type": "write", "sequence": 1, "data": "failed"})
        with self.assertRaisesRegex(RuntimeError, "resize transport failed"):
            first.submit({"type": "resize", "sequence": 2, "cols": 100, "rows": 30})
        self.assertEqual(terminal.info["cols"], 80)
        first.submit({"type": "write", "sequence": 3, "data": "accepted"})
        first.submit({"type": "resize", "sequence": 4, "cols": 90, "rows": 25})
        self.assertEqual(handle.written[-1], "accepted")
        self.assertEqual((terminal.info["cols"], terminal.info["rows"]), (90, 25))

    def test_publishes_failed_process_outcome_and_retains_recovery_screen(self):
        terminal, handle = self.fixture()
        first = attach(terminal)
        handle.output.emit_data(b"last output")
        handle.output.emit_error(RuntimeError("process wait failed"))
        self.assertEqual(first.follower.pop()["data"], "last output")
        state = first.follower.pop()
        self.assertEqual(state["info"]["state"], "failed")
        self.assertEqual(state["info"]["error"], "process wait failed")
        second = attach(terminal)
        self.assertEqual(second.baseline["info"]["state"], "failed")
        self.assertIn("last output", second.baseline["screen"])

    def test_flushes_incomplete_utf8_at_eof_before_publishing_exit(self):
        terminal, handle = self.fixture()
        first = attach(terminal)
        handle.output.emit_data(bytes([0xE7, 0xBB]))
        handle.output.emit_end(SubprocessOutcome(0, None))
        self.assertEqual(first.follower.pop()["data"], "\ufffd")
        self.assertEqual(first.follower.pop()["info"]["state"], "exited")

    def test_preserves_leading_utf8_bom(self):
        terminal, handle = self.fixture()
        first = attach(terminal)
        handle.output.emit_data(b"\xef\xbb\xbf" + "终端".encode("utf-8"))
        self.assertEqual(first.follower.pop()["data"], "\ufeff终端")

    def test_permits_retry_after_termination_fails(self):
        terminal, handle = self.fixture()
        first = attach(terminal)
        handle.on_terminate = lambda: (_ for _ in ()).throw(RuntimeError("process range remains alive"))
        with self.assertRaisesRegex(RuntimeError, "remains alive"):
            terminal.close()
        handle.on_terminate = None
        first.submit({"type": "write", "sequence": 1, "data": "retry cleanup next"})
        terminal.close()
        self.assertEqual(handle.terminate_calls, 2)
        self.assertTrue(first.follower.closed or first.follower.finished)

    def test_snapshot_carries_writable_and_the_carrier_input_window(self):
        terminal, handle = self.fixture()
        first = attach(terminal, input_window={"maxBytes": 512, "frameOverheadBytes": 40})
        self.assertEqual(first.baseline["writable"], True)
        self.assertEqual(first.baseline["inputWindow"],
                         {"maxBytes": 512, "frameOverheadBytes": 40})
        second = attach(terminal)
        self.assertNotIn("inputWindow", second.baseline)
        self.assertEqual(second.baseline["writable"], True)
        self.assertFalse(first.writable)

    def test_rejects_an_input_sequence_gap_without_acknowledging(self):
        terminal, handle = self.fixture()
        first = attach(terminal)
        first.submit({"type": "write", "sequence": 1, "data": "a"})
        self.assertEqual(first.follower.pop(), {"type": "ack", "sequence": 1})
        with self.assertRaisesRegex(RuntimeError, "sequence has a gap"):
            first.submit({"type": "write", "sequence": 3, "data": "c"})
        self.assertEqual(handle.written, ["a"])


if __name__ == "__main__":
    unittest.main()
