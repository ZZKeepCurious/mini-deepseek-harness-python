import unittest

from miniharness.fs.patch_codec import PatchParseError, apply_hunks, parse_patch, PatchHunk


class PatchCodecTest(unittest.TestCase):
    def test_parse_empty_patch(self):
        ops = parse_patch("*** Begin Patch\n*** End Patch\n")
        self.assertEqual(ops, [])

    def test_parse_add_file(self):
        text = (
            "*** Begin Patch\n"
            "*** Add File: created.txt\n"
            "+hello\n"
            "*** End Patch\n"
        )
        ops = parse_patch(text)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].kind, "add")
        self.assertEqual(ops[0].path, "created.txt")
        self.assertEqual(ops[0].content, "hello\n")

    def test_apply_hunks_update(self):
        before = "old line\n"
        hunks = [PatchHunk(line_number=2, old_lines=["old line"], new_lines=["new line"])]
        matched, after = apply_hunks(before, hunks)
        self.assertTrue(matched)
        self.assertEqual(after, "new line\n")

    def test_parse_rejects_bad_begin(self):
        with self.assertRaises(PatchParseError):
            parse_patch("*** Oops\n*** End Patch\n")


if __name__ == "__main__":
    unittest.main()
