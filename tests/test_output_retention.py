"""output-retention 代理对安全截断验收（对齐 output-retention.spec.ts:379-404）。"""
import unittest

from miniharness.core.output_retention import (
    truncate_without_splitting_surrogate_pair,
    utf16_length,
)


class TestTruncateWithoutSplittingSurrogatePair(unittest.TestCase):
    def test_ascii_cap(self):
        self.assertEqual(truncate_without_splitting_surrogate_pair("abcdef", 3), "abc")
        self.assertEqual(truncate_without_splitting_surrogate_pair("abcdef", 0), "")
        self.assertEqual(truncate_without_splitting_surrogate_pair("abcdef", 1), "a")

    def test_within_cap_is_untouched(self):
        self.assertEqual(truncate_without_splitting_surrogate_pair("abc", 3), "abc")
        self.assertEqual(truncate_without_splitting_surrogate_pair("abc", 9), "abc")
        self.assertEqual(truncate_without_splitting_surrogate_pair("", 0), "")

    def test_never_ends_on_a_lone_high_surrogate(self):
        # 😀 是 B7 E5 之外的字符：UTF-16 两个码元。
        self.assertEqual(truncate_without_splitting_surrogate_pair("ab😀cd", 3), "ab")
        self.assertEqual(truncate_without_splitting_surrogate_pair("ab😀cd", 4), "ab😀")
        self.assertEqual(truncate_without_splitting_surrogate_pair("😀😀", 1), "")
        self.assertEqual(truncate_without_splitting_surrogate_pair("😀😀", 3), "😀")

    def test_result_never_exceeds_the_cap(self):
        text = "a😀b😀c😀d"
        for cap in range(0, utf16_length(text) + 2):
            capped = truncate_without_splitting_surrogate_pair(text, cap)
            self.assertLessEqual(utf16_length(capped), cap)
            if capped:
                self.assertFalse(0xD800 <= ord(capped[-1]) <= 0xDBFF)

    def test_utf16_length_counts_non_bmp_as_two(self):
        self.assertEqual(utf16_length("abc"), 3)
        self.assertEqual(utf16_length("😀"), 2)


if __name__ == "__main__":
    unittest.main()
