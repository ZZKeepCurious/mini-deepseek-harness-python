"""LocalPtySession 就绪状态机（同步载体确定性面）。

驱动模型：start_send 建立独占 send → 测试同步 emit / 前进假钟 / 逐次
poll_readiness 直至 settle —— 等价上游 session.spec.ts 里由 timer 驱动、以
PassThrough 门控的异步面。结算元组 {waitReason, sessionStatus, viewport, truncated}
断言 wire 结果；错误行为断言消息或 TerminalError.code。
"""

import threading
import time
import unittest

from miniharness.terminal_bash import (
    LocalPtySession,
    SubprocessForeground,
    SubprocessOutcome,
    TerminalError,
    resolve_config,
)
from miniharness.terminal.types import Cancellation

MARKER = "\x1b]133;D;0\x07"
PROMPT = "dsh> "


class FakeClock:
    """每次读取前进 step_ms：驱动循环依赖时钟流逝，测试即墙钟的一次方。"""

    def __init__(self, start=1_000_000, step_ms=10):
        self.now = start
        self.step_ms = step_ms

    def __call__(self):
        value = self.now
        self.now += self.step_ms
        return value

    def advance(self, ms):
        self.now += ms


class FakeTerminalHandle:
    """测试载体：同步 emit / 动态前台 / 脚本化启动输出。"""

    def __init__(self, pid=100, fg=None):
        self.pid = pid
        self.output = FakeOutputChannel()
        self.written = []
        self.signal_calls = []
        self.terminated = []
        self.fg = fg
        self.dynamic_fg = None
        self._startup_bytes = None
        self._startup_done = False

    def autostart(self, text, delay_ms=20):
        self._startup_bytes = text.encode("utf-8")

        def pump():
            time.sleep(delay_ms / 1000)
            if not self._startup_done:
                self.emit_bytes(self._startup_bytes)
                self._startup_done = True

        threading.Thread(target=pump, daemon=True).start()

    def emit(self, text):
        self.output.emit_data(text.encode("utf-8"))

    def emit_bytes(self, data):
        self.output.emit_data(data)

    def emit_exit(self, exit_code=0, signal=None):
        self.output.emit_end(SubprocessOutcome(exit_code, signal))

    def emit_error(self, error):
        self.output.emit_error(error)

    def write(self, text):
        self.written.append(text)

    def resize(self, cols, rows):
        pass

    def inspect_foreground(self):
        if self.dynamic_fg is not None:
            return self.dynamic_fg
        return self.fg

    def signal_foreground(self, sig):
        self.signal_calls.append(sig)
        fg = self.inspect_foreground()
        return fg.process_group_id if fg is not None else self.pid

    def terminate(self):
        self.terminated.append("terminate")

    def wait(self, timeout=None):
        return self.output.wait_end(timeout or 0.5)


class FakeOutputChannel:
    def __init__(self):
        self._data = []
        self._ended = None
        self._error = None

    def subscribe(self, on_data, on_end=None, on_error=None):
        self._on_data = on_data
        self._on_end = on_end
        self._on_error = on_error

    def emit_data(self, data):
        if self._ended is not None:
            return
        self._data.append(data)
        self._on_data(data)

    def emit_end(self, outcome):
        if self._ended is not None:
            return
        self._ended = outcome
        self._on_end(outcome)

    def emit_error(self, error):
        if self._ended is not None:
            return
        self._error = error
        self._on_error(error)

    def wait_end(self, timeout):
        return self._ended is not None


def make_config(**overrides):
    config = resolve_config({"shellDialect": "bash", "rows": 5, "cols": 20})
    config.update({
        "pollIntervalMs": 10,
        "exactProbeAfterMs": 20,
        "idleSilenceMs": 30,
        "handoffGraceMs": 10,
        "timeoutMs": 200,
        "maxReadBytes": 1024,
        "scrollbackMaxBytes": 4096,
        "scrollbackLines": 1000,
        "disposeGraceMs": 50,
        "rows": 5,
        "cols": 20,
    })
    config.update(overrides)
    return config


def make_session(fg=None, **config_overrides):
    handle = FakeTerminalHandle(fg=fg)
    clock = FakeClock()
    session = LocalPtySession(handle, make_config(**config_overrides), clock=clock)
    return session, handle, clock


def emit_prompt(handle):
    handle.emit(f"{MARKER}{PROMPT}")


class TestLocalPtySession(unittest.TestCase):
    # ---------- 启动 ----------

    def test_initialize_captures_motd_when_prompt_arrives(self):
        session, handle, clock = make_session(timeoutMs=5_000)
        handle.autostart(f"ready banner\n{MARKER}{PROMPT}", delay_ms=15)
        session.initialize()
        self.assertEqual(session.status(), {"kind": "running"})
        self.assertIn(PROMPT, session.motd)
        self.assertIn("ready banner", session.motd)

    def test_initialize_raises_on_session_exit(self):
        session, handle, clock = make_session(timeoutMs=5_000)

        def quit():
            time.sleep(0.03)
            handle.emit_exit(1)

        threading.Thread(target=quit, daemon=True).start()
        self.assertRaisesRegex(RuntimeError, "exited during startup", session.initialize)

    def test_initialize_raises_on_timeout(self):
        session, handle, clock = make_session(timeoutMs=60)
        self.assertRaisesRegex(RuntimeError, "startup timeout", session.initialize)

    # ---------- prompt 就绪 ----------

    def test_send_settles_stdin_read_on_prompt(self):
        session, handle, clock = make_session()
        op = session.start_send({"text": "echo hi", "submit": True})
        self.assertEqual(handle.written, ["echo hi\r"])
        handle.emit("echo hi\r\n")
        emit_prompt(handle)
        for _ in range(20):
            if op.settled:
                break
            clock.advance(10)
            session.poll_readiness(op)
        self.assertTrue(op.settled)
        result = op.done
        self.assertEqual(result["waitReason"], "stdin_read")
        self.assertEqual(result["sessionStatus"], {"kind": "running"})
        self.assertEqual(result["viewport"], f"echo hi\n{PROMPT}")

    def test_prompt_with_foreground_matching_shell_pgid(self):
        session, handle, clock = make_session()
        handle.fg = SubprocessForeground(100, True)
        op = session.start_send({"text": "ls", "submit": False})
        emit_prompt(handle)
        for _ in range(20):
            if op.settled:
                break
            clock.advance(10)
            session.poll_readiness(op)
        self.assertEqual(op.done["waitReason"], "stdin_read")

    # ---------- exact-probe ----------

    def test_exact_probe_requires_departure_and_return(self):
        session, handle, clock = make_session(
            fg=SubprocessForeground(100, True), exactProbeAfterMs=20, idleSilenceMs=100_000)
        # 写入前已是同一组在等（上游注释：write 前存在的 wait 不算证据）。
        op = session.start_send({"text": "grep foo", "submit": True})
        self.assertFalse(op.settled)
        # 同一前台组离开 wait → leftWait 记为证据起点，但此处不结算。
        handle.dynamic_fg = SubprocessForeground(100, False)
        clock.advance(20)
        session.poll_readiness(op)
        self.assertFalse(op.settled)
        # 回到 wait → write 后证据成立，exact-probe 结算。
        handle.dynamic_fg = SubprocessForeground(100, True)
        clock.advance(20)
        session.poll_readiness(op)
        self.assertTrue(op.settled)
        self.assertEqual(op.done["waitReason"], "stdin_read")

    # ---------- 其余结算元 ----------

    def test_send_settles_inferred_idle_on_silence(self):
        session, handle, clock = make_session()
        op = session.start_send({"text": "sleep 2", "submit": True})
        handle.emit("still working\nsecond line\n")
        for _ in range(50):
            if op.settled:
                break
            clock.advance(10)
            session.poll_readiness(op)
        self.assertTrue(op.settled)
        self.assertEqual(op.done["waitReason"], "inferred_idle")
        self.assertEqual(op.done["viewport"], "still working\nsecond line\n")

    def test_send_settles_timeout_without_output(self):
        session, handle, clock = make_session(idleSilenceMs=100_000)
        op = session.start_send({"text": "hang", "submit": True})
        for _ in range(60):
            if op.settled:
                break
            clock.advance(10)
            session.poll_readiness(op)
        self.assertTrue(op.settled)
        self.assertEqual(op.done["waitReason"], "timeout")

    def test_send_settles_session_exit(self):
        session, handle, clock = make_session()
        op = session.start_send({"text": "exit", "submit": True})
        handle.emit("bye\n")
        handle.emit_exit(0)
        self.assertTrue(op.settled)
        result = op.done
        self.assertEqual(result["waitReason"], "session_exit")
        self.assertEqual(result["sessionStatus"], {"kind": "exited", "exitCode": 0, "signal": None})

    # ---------- 取消 / 中断 ----------

    def test_cancel_sends_sigint_then_resumes_polling(self):
        session, handle, clock = make_session()
        op = session.start_send({"text": "sleep 99", "submit": True})
        op.cancel()
        self.assertEqual(handle.signal_calls, ["SIGINT"])
        self.assertFalse(op.settled)
        emit_prompt(handle)
        clock.advance(10)
        session.poll_readiness(op)
        self.assertTrue(op.settled)
        self.assertEqual(op.done["waitReason"], "stdin_read")

    def test_abort_before_write_raises(self):
        session, handle, clock = make_session()
        signal = Cancellation()
        signal.abort(RuntimeError("nope"))
        self.assertRaisesRegex(RuntimeError, "aborted before write",
                               lambda: session.start_send({"text": "x", "signal": signal}))

    # ---------- 前哨 ----------

    def test_second_send_while_active_raises_send_active(self):
        session, handle, clock = make_session()
        op = session.start_send({"text": "one", "submit": True})
        with self.assertRaises(TerminalError) as raised:
            session.start_send({"text": "two", "submit": True})
        self.assertEqual(raised.exception.code, "SEND_ACTIVE")
        emit_prompt(handle)
        for _ in range(20):
            if op.settled:
                break
            clock.advance(10)
            session.poll_readiness(op)
        self.assertTrue(op.settled)
        session.start_send({"text": "three", "submit": True})

    def test_start_send_after_exit_raises(self):
        session, handle, clock = make_session()
        handle.emit_exit(0)
        self.assertRaises(RuntimeError, lambda: session.start_send({"text": "x"}))

    # ---------- read ----------

    def test_read_pages_from_end(self):
        session, handle, clock = make_session()
        for line in ("a", "b", "c", "d", "e"):
            handle.emit(f"{line}\n")
        # 末行 `\n` 之后光标落在空行 → 翻页单位含该末尾空行（totalLines 计 6）。
        result = session.read({"offset": 0, "count": 2})
        self.assertEqual(result["text"], "e\n")
        self.assertEqual(result["totalLines"], 6)
        self.assertEqual(result["lineBegin"], 0)
        self.assertEqual(result["lineEnd"], 2)
        self.assertFalse(result["truncated"])
        result = session.read({"offset": 2, "count": 2})
        self.assertEqual(result["text"], "c\nd")
        result = session.read({"offset": 6})
        self.assertEqual(result["text"], "")
        self.assertRaisesRegex(RuntimeError, "offset", lambda: session.read({"offset": -1}))
        self.assertRaisesRegex(RuntimeError, "count", lambda: session.read({"count": 0}))

    def test_read_carries_scrollback_truncation(self):
        session, handle, clock = make_session(scrollbackMaxBytes=28)
        handle.emit("0123456789012345678901234567\n")
        handle.emit("END LINE\n")
        result = session.read({})
        self.assertTrue(result["truncated"])

    # ---------- signal / close ----------

    def test_signal_targets_foreground(self):
        session, handle, clock = make_session(fg=SubprocessForeground(100, True))
        result = session.signal("SIGINT")
        self.assertEqual(result, {"delivered": True, "targetPgid": 100})

    def test_close_terminates_and_settles_existing_send(self):
        session, handle, clock = make_session()
        op = session.start_send({"text": "run", "submit": True})
        session.close("done")
        self.assertEqual(handle.terminated, ["terminate"])
        self.assertTrue(op.settled)
        self.assertEqual(op.done["waitReason"], "session_exit")
        self.assertRaises(RuntimeError, lambda: session.start_send({"text": "x"}))

    def test_close_raises_transport_failure(self):
        session, handle, clock = make_session()
        handle.emit_error(RuntimeError("pty exploded"))
        self.assertEqual(session.status(), {"kind": "exited", "exitCode": None, "signal": None})
        with self.assertRaises(RuntimeError):
            session.close("dispose")
            self.fail("transport failure must be raised")

    def test_signal_on_closing_raises(self):
        session, handle, clock = make_session()
        session.close("done")
        self.assertRaises(RuntimeError, lambda: session.signal("SIGINT"))

    # ---------- 终端协议应答 ----------

    def test_da_query_in_output_writes_reply_to_pty(self):
        session, handle, clock = make_session()
        handle.emit("\x1b[c")
        self.assertIn("\x1b[?1;2c", "".join(handle.written))

    def test_utf8_incremental_decode_across_chunks(self):
        session, handle, clock = make_session()
        handle.emit("ab")
        handle.emit_bytes("你".encode("utf-8")[:2])
        handle.emit_bytes("你".encode("utf-8")[2:])
        handle.emit("\r\n")
        result = session.read({})
        self.assertIn("ab你", result["text"])


if __name__ == "__main__":
    unittest.main()