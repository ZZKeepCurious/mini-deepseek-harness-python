"""workspace-files：只读预览、目录列举与 fs/observed 观察流。

对齐 packages/api/workspace-files（index.ts / changes.ts 的确定性面）。
"""

import base64
import os
import tempfile
import unittest

from miniharness.core.agent_loop.resident_loop import run_on_resident
from miniharness.core.scope import Context
from miniharness.fs import FsObservation, install_local_fs
from miniharness.workspace_files import WorkspaceFileFault, install_workspace_files


class WorkspaceFilesCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.ctx = Context(name="workspace-files-test")
        self.addCleanup(self.ctx.dispose)
        self.fs = install_local_fs(self.ctx, {"cwd": self.root})
        self.controller = install_workspace_files(self.ctx)
        self.scope = {"sessionId": "s", "workspaceRoot": self.root}

    def _write(self, name, data: bytes):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def _code(self, call):
        with self.assertRaises(WorkspaceFileFault) as caught:
            call()
        return caught.exception.code

    def _fresh(self, **config):
        ctx = Context(name="workspace-files-cap")
        self.addCleanup(ctx.dispose)
        install_local_fs(ctx, {"cwd": self.root})
        return install_workspace_files(ctx, config)

    def test_stat_and_line_pages(self):
        path = self._write("a.txt", b"line1\nline2\nline3\n")
        stat = self.controller.stat(self.scope, path)
        self.assertEqual(stat["absolutePath"], os.path.realpath(path))
        self.assertEqual(stat["bytes"], len(b"line1\nline2\nline3\n"))
        self.assertTrue(stat["version"])
        page = self.controller.read(self.scope, path, {"offset": 2, "limit": 1})
        self.assertEqual((page["text"], page["lines"], page["eof"]), ("line2", 1, False))
        whole = self.controller.read(self.scope, path, {})
        self.assertEqual((whole["lines"], whole["eof"]), (3, True))
        past = self.controller.read(self.scope, path, {"offset": 99})
        self.assertEqual((past["text"], past["lines"], past["eof"]), ("", 0, True))

    def test_byte_windows_and_complete_reads(self):
        path = self._write("a.bin", b"0123456789")
        window = self.controller.read_bytes(self.scope, path, {"offset": 2, "length": 3})
        self.assertEqual(base64.b64decode(window["data"]), b"234")
        self.assertFalse(window["eof"])
        end = self.controller.read_bytes(self.scope, path, {"offset": 8, "length": 5})
        self.assertEqual(base64.b64decode(end["data"]), b"89")
        self.assertTrue(end["eof"])
        whole = self.controller.read_all(self.scope, path)
        self.assertEqual(base64.b64decode(whole["data"]), b"0123456789")
        self.assertTrue(whole["eof"])

    def test_read_related_resolves_from_base_directory(self):
        base = self._write("dir/a.txt", b"a")
        self._write("dir/b.txt", b"related")
        result = self.controller.read_related(self.scope, base, "b.txt")
        self.assertEqual(base64.b64decode(result["data"]), b"related")
        self.assertEqual(self._code(lambda: self.controller.read_related(
            self.scope, base, "../escape")), "workspace-file/not-found")
        self.assertEqual(self._code(lambda: self.controller.read_related(
            self.scope, base, "/abs")), "gateway/bad-request")

    def test_text_refusals(self):
        binary = self._write("bin.dat", b"ab\x00cd")
        self.assertEqual(self._code(lambda: self.controller.read(self.scope, binary, {})),
                         "workspace-file/not-text")
        self.assertEqual(self._code(lambda: self.controller.read(
            self.scope, os.path.join(self.root, "missing.txt"), {})),
            "workspace-file/not-found")
        self.assertEqual(self._code(lambda: self.controller.read(
            self.scope, self.root, {})), "workspace-file/not-regular-file")

    def test_read_all_refuses_above_the_complete_file_cap(self):
        path = self._write("big.txt", b"0123456789")
        controller = self._fresh(maxFileBytes=4)
        self.assertEqual(self._code(lambda: controller.read_all(self.scope, path)),
                         "workspace-file/too-large")

    def test_list_directory_bounds_and_confinement(self):
        self._write("dir/one.txt", b"1")
        self._write("dir/two.txt", b"22")
        directory = os.path.join(self.root, "dir")
        listing = self.controller.list(self.scope, directory)
        self.assertEqual(listing["path"], "dir")
        self.assertEqual([entry["name"] for entry in listing["entries"]], ["one.txt", "two.txt"])
        bounded = self._fresh(maxEntries=1).list(self.scope, directory)
        self.assertTrue(bounded["truncated"])
        self.assertEqual(len(bounded["entries"]), 1)
        self.assertEqual(self._code(lambda: self.controller.list(
            self.scope, os.path.dirname(self.root))), "workspace-file/outside-workspace")
        self.assertEqual(self._code(lambda: self.controller.list(
            self.scope, os.path.join(self.root, "dir", "one.txt"))),
            "workspace-file/not-directory")

    def test_changes_streams_ready_then_contained_observations(self):
        path = self._write("watched.txt", b"x")
        target = run_on_resident(self.fs.resolve(path))
        info = run_on_resident(self.fs.stat(target))
        changes = self.controller.changes(self.scope)
        self.assertEqual(changes.ready, {"kind": "ready"})
        self.ctx.emit("fs/observed", (target, FsObservation("present", info.version), None))
        frame = changes.pop()
        self.assertEqual(frame["kind"], "change")
        self.assertEqual(frame["change"]["absolutePath"], os.path.realpath(path))
        self.assertEqual(frame["change"]["version"], info.version)
        outside = run_on_resident(self.fs.resolve(os.path.dirname(self.root)))
        self.ctx.emit("fs/observed", (outside, FsObservation("absent"), None))
        self.assertIsNone(changes.pop())
        changes.close()
        self.ctx.emit("fs/observed", (target, FsObservation("absent"), None))
        self.assertIsNone(changes.pop())


if __name__ == "__main__":
    unittest.main()
