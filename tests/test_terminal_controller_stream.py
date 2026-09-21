"""TerminalFollower：字节预算、排空与分离（对齐 terminal-controller/tests/stream.spec.ts）。"""

import threading
import unittest

from miniharness.terminal_controller.stream import TerminalFollower
from miniharness.terminal_controller.types import frame_bytes

FRAME = {"type": "output", "sequence": 1, "data": "终端"}


class TestTerminalFollower(unittest.TestCase):
    def test_accepts_exact_byte_limit_and_restores_capacity(self):
        follower = TerminalFollower(frame_bytes(FRAME))
        follower.push(FRAME)
        self.assertEqual(follower.pop(), FRAME)
        follower.push(FRAME)
        follower.finish()
        follower.push({**FRAME, "sequence": 2})
        self.assertEqual(follower.pop(), FRAME)
        self.assertIsNone(follower.pop())
        self.assertTrue(follower.finished)
        self.assertIsNone(follower.failure)

    def test_fails_accumulated_overflow_using_encoded_byte_size(self):
        follower = TerminalFollower(frame_bytes(FRAME))
        follower.push(FRAME)
        follower.push(FRAME)
        self.assertIsNotNone(follower.failure)
        self.assertIn("reconnect to recover", str(follower.failure))
        self.assertTrue(follower.closed)

    def test_ignores_later_output_after_close(self):
        follower = TerminalFollower(1000)
        follower.push(FRAME)
        follower.close()
        follower.push(FRAME)
        self.assertEqual(list(follower.read()), [])
        self.assertTrue(follower.closed)

    def test_read_yields_queued_frames_then_closes(self):
        follower = TerminalFollower(1000)
        follower.push(FRAME)
        frames = []
        for frame in follower.read():
            frames.append(frame)
            if len(frames) == 1:
                follower.finish()
        self.assertEqual(frames, [FRAME])
        self.assertTrue(follower.closed)

    def test_read_wakes_waiting_reader_for_output_then_completion(self):
        follower = TerminalFollower(1000)
        result = {}

        def consume():
            result["frames"] = list(follower.read())

        thread = threading.Thread(target=consume)
        thread.start()
        follower.push(FRAME)
        follower.finish()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["frames"], [FRAME])

    def test_read_wakes_waiting_reader_on_close(self):
        follower = TerminalFollower(1000)
        result = {}

        def consume():
            result["frames"] = list(follower.read())

        thread = threading.Thread(target=consume)
        thread.start()
        follower.close()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["frames"], [])

    def test_raises_failure_when_drained_by_overflow(self):
        follower = TerminalFollower(frame_bytes(FRAME))
        follower.push(FRAME)
        follower.push(FRAME)
        with self.assertRaisesRegex(RuntimeError, "buffer"):
            list(follower.read())


if __name__ == "__main__":
    unittest.main()
