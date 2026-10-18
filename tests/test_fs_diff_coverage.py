"""fs/diff 补充覆盖：hunk 生成（insert/delete/replace）与落盘 meta 收窄。

对齐 packages/fs/tool-fs/src/diff.ts。真输入取自纯文本 before/after，
断言具体 hunk 形状与畸形 meta 的 None 归类。
"""
import unittest

from miniharness.fs.diff import (
    DIFF_CONTEXT,
    FS_DIFF_OPERATIONS,
    compute_hunk_diffs,
    diffs_from_meta,
    fs_diff_meta,
)


class ComputeHunkDiffsTest(unittest.TestCase):
    def test_insert_branch(self):
        diffs = compute_hunk_diffs("p.txt", "a\nc", "a\nb\nc")
        self.assertEqual(diffs, [{
            "path": "p.txt",
            "oldText": "a\nc",
            "newText": "a\nb\nc",
        }])

    def test_delete_branch(self):
        diffs = compute_hunk_diffs("p.txt", "a\nb\nc", "a\nc")
        self.assertEqual(diffs, [{
            "path": "p.txt",
            "oldText": "a\nb\nc",
            "newText": "a\nc",
        }])

    def test_replace_branch(self):
        diffs = compute_hunk_diffs("p.txt", "one\ntwo\n", "one\nTWO\n")
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["oldText"], "one\ntwo\n")
        self.assertEqual(diffs[0]["newText"], "one\nTWO\n")

    def test_distant_changes_split_into_hunks(self):
        before = "\n".join(f"line{i}" for i in range(30))
        after_lines = before.split("\n")
        after_lines[0] = "TOP"
        after_lines[29] = "BOTTOM"
        diffs = compute_hunk_diffs("p.txt", before, "\n".join(after_lines))
        self.assertEqual(len(diffs), 2)
        self.assertEqual(DIFF_CONTEXT, 3)

    def test_no_change_yields_no_hunks(self):
        self.assertEqual(compute_hunk_diffs("p.txt", "same\n", "same\n"), [])


class FsDiffMetaTest(unittest.TestCase):
    def test_create_before_none_empty_diffs(self):
        self.assertEqual(fs_diff_meta("p", None, "content\n", "create"),
                         {"operation": "create", "diffs": []})

    def test_update_generates_hunks(self):
        meta = fs_diff_meta("p", "a\n", "b\n", "update")
        self.assertEqual(meta["operation"], "update")
        self.assertEqual(meta["diffs"][0]["path"], "p")

    def test_unknown_operation_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown fs diff operation: 'bogus'"):
            fs_diff_meta("p", None, "x", "bogus")

    def test_known_operations(self):
        self.assertEqual(FS_DIFF_OPERATIONS, ("create", "update"))


class DiffsFromMetaTest(unittest.TestCase):
    def test_valid_list_returned(self):
        diffs = [{"path": "p", "oldText": "a", "newText": "b"},
                 {"path": "q", "oldText": None, "newText": "c"}]
        self.assertEqual(diffs_from_meta({"diffs": diffs}), diffs)

    def test_non_dict_meta_none(self):
        self.assertIsNone(diffs_from_meta("not-a-dict"))
        self.assertIsNone(diffs_from_meta(None))

    def test_missing_or_empty_diffs_none(self):
        self.assertIsNone(diffs_from_meta({}))
        self.assertIsNone(diffs_from_meta({"diffs": []}))
        self.assertIsNone(diffs_from_meta({"diffs": "nope"}))

    def test_malformed_entry_none(self):
        self.assertIsNone(diffs_from_meta({"diffs": [{"path": "p"}]}))
        self.assertIsNone(diffs_from_meta({"diffs": [{"path": 1, "oldText": None,
                                                      "newText": "x"}]}))
        self.assertIsNone(diffs_from_meta({"diffs": [{"path": "p", "oldText": 5,
                                                      "newText": "x"}]}))
        self.assertIsNone(diffs_from_meta({"diffs": [{"path": "p", "oldText": None,
                                                      "newText": 7}]}))
        self.assertIsNone(diffs_from_meta({"diffs": [{"path": "p",
                                                      "oldText": None,
                                                      "newText": "x"}, "bad"]}))


if __name__ == "__main__":
    unittest.main()
