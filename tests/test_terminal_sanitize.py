"""TerminalSanitizer 确定性面（对齐 terminal-bash/tests/sanitize.spec.ts 全量移植）。

覆盖：跨块 CSI/OSC/prompt marker、无关 OSC/短转义/BEL、CRLF 与孤立 CR 归一、
prompt 尾巴跨块累计、超限 discard 模式（BEL/ST 双终结、OSC 内 ESC 状态机）。
"""

import unittest

from miniharness.terminal.sanitize import TerminalSanitizer, normalize_terminal_text


class TestTerminalSanitizer(unittest.TestCase):
    def test_removes_split_csi_and_owned_osc_prompt_markers(self):
        sanitizer = TerminalSanitizer(64)
        self.assertEqual(sanitizer.push("red\x1b[3"), {"text": "red", "prompt": False})
        self.assertEqual(sanitizer.push("1m text\x1b[0m\r\n"),
                         {"text": " text\n", "prompt": False})
        self.assertEqual(sanitizer.push("\x1b]133;"), {"text": "", "prompt": False})
        self.assertEqual(sanitizer.push("D;0\x07dsh> "),
                         {"text": "dsh> ", "prompt": True, "promptTail": "dsh> "})

    def test_drops_unrelated_osc_short_escapes_bel_and_incomplete_trailing_escape(self):
        sanitizer = TerminalSanitizer(64)
        self.assertEqual(sanitizer.push("a\x1b]0;title\x1b\\b\x1b7c\x07"),
                         {"text": "abc", "prompt": False})
        self.assertEqual(sanitizer.push("tail\x1b"), {"text": "tail", "prompt": False})
        self.assertEqual(sanitizer.flush(), "")
        self.assertEqual(sanitizer.flush(), "")
        self.assertEqual(sanitizer.push("\x1b]0;one\x07middle\x1b\\"),
                         {"text": "middle", "prompt": False})
        self.assertEqual(sanitizer.push("\x1b]0;one\x1b\\middle\x07"),
                         {"text": "middle", "prompt": False})
        self.assertEqual(sanitizer.push("\x1b]0;title\x1b\\"),
                         {"text": "", "prompt": False})

    def test_normalizes_crlf_and_standalone_carriage_returns(self):
        self.assertEqual(normalize_terminal_text("a\r\nb\rc\x07"), "a\nb\nc")

    def test_carries_trailing_carriage_return_across_chunks_and_flushes_standalone_cr(self):
        sanitizer = TerminalSanitizer(64)
        self.assertEqual(sanitizer.push("a\r"), {"text": "a", "prompt": False})
        self.assertEqual(sanitizer.push("\nb"), {"text": "\nb", "prompt": False})
        self.assertEqual(sanitizer.push("\r"), {"text": "", "prompt": False})
        self.assertEqual(sanitizer.flush(), "\n")

    def test_reports_printable_prompt_text_that_follows_marker_in_later_chunk(self):
        sanitizer = TerminalSanitizer(64)
        self.assertEqual(sanitizer.push("\x1b]133;D;0\x07"),
                         {"text": "", "prompt": True, "promptTail": ""})
        self.assertEqual(sanitizer.push("dsh> "),
                         {"text": "dsh> ", "prompt": False, "promptTail": "dsh> "})

    def test_bounds_and_discards_unterminated_control_sequences(self):
        osc_bel = TerminalSanitizer(8)
        self.assertEqual(osc_bel.push(f"\x1b]0;{'x' * 16}"), {"text": "", "prompt": False})
        self.assertEqual(osc_bel.push("more\x07tail"), {"text": "tail", "prompt": False})

        osc_st = TerminalSanitizer(8)
        osc_st.push(f"\x1b]0;{'x' * 16}")
        self.assertEqual(osc_st.push("more\x1b"), {"text": "", "prompt": False})
        self.assertEqual(osc_st.push("\\tail"), {"text": "tail", "prompt": False})

        osc_direct_st = TerminalSanitizer(8)
        osc_direct_st.push(f"\x1b]0;{'x' * 16}")
        self.assertEqual(osc_direct_st.push("more\x1b\\tail"), {"text": "tail", "prompt": False})

        osc_false_st = TerminalSanitizer(8)
        osc_false_st.push(f"\x1b]0;{'x' * 16}")
        osc_false_st.push("\x1b")
        self.assertEqual(osc_false_st.push("more"), {"text": "", "prompt": False})
        self.assertEqual(osc_false_st.push("\x07tail"), {"text": "tail", "prompt": False})

        osc_non_terminating = TerminalSanitizer(8)
        osc_non_terminating.push(f"\x1b]0;{'x' * 16}")
        self.assertEqual(osc_non_terminating.push("more\x1bxmore\x07tail"),
                         {"text": "tail", "prompt": False})

        csi = TerminalSanitizer(8)
        self.assertEqual(csi.push(f"\x1b[{'1' * 16}"), {"text": "", "prompt": False})
        self.assertEqual(csi.push("123"), {"text": "", "prompt": False})
        self.assertEqual(csi.push("mtext"), {"text": "text", "prompt": False})

        flushed = TerminalSanitizer(8)
        flushed.push(f"\x1b]0;{'x' * 16}")
        self.assertEqual(flushed.flush(), "")
        self.assertEqual(flushed.push("text"), {"text": "text", "prompt": False})


if __name__ == "__main__":
    unittest.main()