"""fs-local 后端验收（对齐 packages/fs/fs-local：身份/读取/原子写/字面编辑）。"""
import os
import pathlib
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.fs import (
    FsEditRequest,
    FsError,
    FsWriteIntent,
    LocalFileSystem,
    install_local_fs,
)


class FsLocalTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self.ctx = Context(name="root")
        self.fs = LocalFileSystem(self.ctx, {"cwd": str(self.dir)})

    def tearDown(self):
        self.ctx.dispose()
        self._tmp.cleanup()

    def _write(self, name: str, data: bytes) -> pathlib.Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    async def test_resolve_process_path_file_url_contains(self):
        target = await self.fs.resolve("sub/../a.txt")
        self.assertEqual(target.display_path, str(self.dir / "a.txt"))
        self.assertEqual(self.fs.process_path(target), os.path.realpath(str(self.dir / "a.txt")))
        self.assertTrue(self.fs.file_url(target).startswith("file:"))
        parent = await self.fs.resolve(".")
        child = await self.fs.resolve("a.txt")
        self.assertTrue(self.fs.contains(parent, child))
        self.assertFalse(self.fs.contains(child, parent))

    async def test_resolve_missing_uses_realpath_ancestor(self):
        target = await self.fs.resolve("new/deep/file.txt")
        self.assertEqual(target.display_path, str(self.dir / "new" / "deep" / "file.txt"))
        self.assertEqual(target.target_key,
                         os.path.join(os.path.realpath(str(self.dir)), "new", "deep", "file.txt"))

    async def test_stat_and_lstat(self):
        path = self._write("a.txt", b"hello")
        target = await self.fs.resolve("a.txt")
        info = await self.fs.stat(target)
        self.assertEqual((info.type, info.size), ("file", 5))
        self.assertIsNone(await self.fs.stat(await self.fs.resolve("nope.txt")))
        path_info = await self.fs.lstat("a.txt")
        self.assertEqual(path_info.type, "file")
        self.assertIsNone(await self.fs.lstat("nope.txt"))
        self.assertIsNotNone(path)

    async def test_read_text_and_binary_rejection(self):
        self._write("a.txt", "café\n".encode("utf-8"))
        target = await self.fs.resolve("a.txt")
        self.assertEqual(await self.fs.read_text(target), "café\n")
        self._write("bin", b"\x00\x01\x02")
        with self.assertRaises(FsError) as binary:
            await self.fs.read_text(await self.fs.resolve("bin"))
        self.assertEqual(binary.exception.code, "FS_NOT_TEXT")
        self._write("bad", b"\xff\xfe")
        with self.assertRaises(FsError) as invalid:
            await self.fs.read_text(await self.fs.resolve("bad"))
        self.assertEqual(invalid.exception.code, "FS_NOT_TEXT")

    async def test_read_text_missing_and_directory(self):
        with self.assertRaises(FsError) as missing:
            await self.fs.read_text(await self.fs.resolve("nope"))
        self.assertEqual(missing.exception.code, "FS_NOT_FOUND")
        with self.assertRaises(FsError) as directory:
            await self.fs.read_text(await self.fs.resolve("."))
        self.assertEqual(directory.exception.code, "FS_NOT_REGULAR_FILE")

    async def test_stream_text_matches_read(self):
        self._write("big.txt", ("行\n" * 5000).encode("utf-8"))
        target = await self.fs.resolve("big.txt")
        chunks = []
        async for chunk in self.fs.stream_text(target):
            chunks.append(chunk)
        self.assertEqual("".join(chunks), await self.fs.read_text(target))

    async def test_read_bytes_cap_and_window(self):
        self._write("a.bin", bytes(range(256)) * 4)
        target = await self.fs.resolve("a.bin")
        self.assertEqual(await self.fs.read_bytes(target, None, 2048), bytes(range(256)) * 4)
        with self.assertRaises(FsError) as too_large:
            await self.fs.read_bytes(target, None, 10)
        self.assertEqual(too_large.exception.code, "FS_TOO_LARGE")
        self.assertEqual(await self.fs.read_byte_range(target, 2, 3), bytes([2, 3, 4]))
        self.assertEqual(await self.fs.read_byte_range(target, 99999, 4), b"")

    async def test_write_create_update_and_contextual_basis(self):
        target = await self.fs.resolve("out.txt")
        created = await self.fs.write_text(target, "one\ntwo\n")
        self.assertEqual(created.operation, "create")
        self.assertIsNone(created.before)
        self.assertEqual(created.after, "one\ntwo\n")
        updated = await self.fs.write_text(target, "one\nTWO\n")
        self.assertEqual(updated.operation, "update")
        self.assertEqual(updated.before, "one\ntwo\n")
        self.assertEqual((self.dir / "out.txt").read_text(encoding="utf-8"), "one\nTWO\n")

    async def test_guarded_intents(self):
        target = await self.fs.resolve("g.txt")
        first = await self.fs.write_text(target, "v1", FsWriteIntent("createIfAbsent"))
        with self.assertRaises(FsError) as existing:
            await self.fs.write_text(target, "v2", FsWriteIntent("createIfAbsent"))
        self.assertEqual(existing.exception.code, "FS_NOT_OBSERVED")
        with self.assertRaises(FsError) as stale:
            await self.fs.write_text(target, "v2",
                                     FsWriteIntent("replaceIfVersion", version="deadbeef"))
        self.assertEqual(stale.exception.code, "FS_STALE_VERSION")
        replaced = await self.fs.write_text(
            target, "v2", FsWriteIntent("replaceIfVersion", version=first.version))
        self.assertEqual(replaced.operation, "update")

    async def test_write_onto_directory(self):
        target = await self.fs.resolve(".")
        with self.assertRaises(FsError) as error:
            await self.fs.write_text(target, "x")
        self.assertEqual(error.exception.code, "FS_NOT_REGULAR_FILE")

    async def test_list_dir_sorted_with_metadata(self):
        self._write("b.txt", b"22")
        self._write("a.txt", b"1")
        (self.dir / "sub").mkdir()
        entries = await self.fs.list_dir(await self.fs.resolve("."))
        self.assertEqual([e.name for e in entries], ["a.txt", "b.txt", "sub"])
        by_name = {e.name: e for e in entries}
        self.assertEqual((by_name["a.txt"].type, by_name["a.txt"].size), ("file", 1))
        self.assertEqual(by_name["sub"].type, "directory")
        self.assertEqual(by_name["a.txt"].target.display_path, str(self.dir / "a.txt"))

    async def test_edit_text_literal_and_guards(self):
        self._write("e.txt", b"alpha beta alpha")
        target = await self.fs.resolve("e.txt")
        with self.assertRaises(FsError) as ambiguous:
            await self.fs.edit_text(target, FsEditRequest("alpha", "A", replace_all=False))
        self.assertEqual(ambiguous.exception.code, "FS_AMBIGUOUS_EDIT")
        outcome = await self.fs.edit_text(target, FsEditRequest("alpha", "A", replace_all=True))
        self.assertEqual(outcome.before, "alpha beta alpha")
        self.assertEqual(outcome.after, "A beta A")
        with self.assertRaises(FsError) as not_found:
            await self.fs.edit_text(target, FsEditRequest("zzz", "A", replace_all=False))
        self.assertEqual(not_found.exception.code, "FS_EDIT_NOT_FOUND")
        with self.assertRaises(FsError) as stale:
            await self.fs.edit_text(target, FsEditRequest("A", "B", replace_all=False),
                                    expected={"version": "old"})
        self.assertEqual(stale.exception.code, "FS_STALE_VERSION")

    async def test_edit_preserves_crlf(self):
        self._write("c.txt", b"one\r\ntwo\r\n")
        target = await self.fs.resolve("c.txt")
        outcome = await self.fs.edit_text(target, FsEditRequest("two", "TWO", replace_all=False))
        self.assertEqual(outcome.before, "one\ntwo\n")
        self.assertEqual(outcome.after, "one\nTWO\n")
        self.assertEqual((self.dir / "c.txt").read_bytes(), b"one\r\nTWO\r\n")

    async def test_install_is_idempotent(self):
        ctx = Context(name="root2")
        try:
            first = install_local_fs(ctx, {"cwd": str(self.dir)})
            self.assertIs(ctx.get("fs"), first)
            self.assertIs(install_local_fs(ctx), first)
        finally:
            ctx.dispose()


if __name__ == "__main__":
    unittest.main()
