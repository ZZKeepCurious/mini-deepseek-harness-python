"""storage.jsonbackend.atomic 补充覆盖：原子整文件替换的持久发布与失败清理。

对齐 packages/storage/storage-json/src/atomic.ts（writeAtomic）。真文件系统
输入：临时目录下真实路径，断言磁盘字节与失败后无残留临时文件。
"""
import os
import tempfile
import unittest

from miniharness.storage.jsonbackend.atomic import _fsync_directory, write_atomic


class WriteAtomicTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def test_creates_and_replaces_content(self):
        path = os.path.join(self.root, "unit.json")
        write_atomic(path, "first")
        with open(path, "r", encoding="utf-8", newline="") as handle:
            self.assertEqual(handle.read(), "first")
        write_atomic(path, "second")
        with open(path, "r", encoding="utf-8", newline="") as handle:
            self.assertEqual(handle.read(), "second")

    def test_writes_lf_without_translation(self):
        path = os.path.join(self.root, "unit.json")
        write_atomic(path, "a\nb\n")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"a\nb\n")

    def test_no_tmp_residue_after_success(self):
        path = os.path.join(self.root, "unit.json")
        write_atomic(path, "x")
        leftovers = [name for name in os.listdir(self.root) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_replace_onto_directory_cleans_tmp_and_raises(self):
        target = os.path.join(self.root, "a-directory")
        os.makedirs(target)
        with self.assertRaises(OSError):
            write_atomic(target, "cannot replace a directory")
        leftovers = [name for name in os.listdir(self.root) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_fsync_directory_returns_without_error(self):
        # Windows 走 os.name == "nt" 提前返回；POSIX 打开目录句柄 fsync。
        _fsync_directory(self.root)


if __name__ == "__main__":
    unittest.main()
