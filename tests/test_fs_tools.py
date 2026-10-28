"""tool-fs（read/read_image/write/edit）验收（对齐 packages/fs/tool-fs）。"""
import base64
import pathlib
import tempfile
import unittest

from miniharness.attachment.store import LocalAttachmentStore
from miniharness.core.agent_loop.tool_calls import append_tool_call, emit_tool_result
from miniharness.core.session.session import Session
from miniharness.core.scope import Context
from miniharness.core.tools import ToolExec, ToolRegistry, ToolResult, run_pipeline_async
from miniharness.fs import FsError, install_local_fs, install_fs_observation_policy
from miniharness.fs.tools import install_fs_tools, sniff_image_media_type

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class _Adapter:
    def __init__(self, model="m", modalities=("text", "image")):
        self._model = model
        self._modalities = modalities

    def resolve_model_info(self):
        return {"provider": "fake", "model": self._model,
                "input_modalities": list(self._modalities)}


class _Agent:
    def __init__(self, session, adapter=None):
        self.session = session
        self.adapter = adapter


class FsToolsTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self.ctx = Context(name="root")
        self.registry = ToolRegistry(self.ctx)
        self.fs = install_local_fs(self.ctx, {"cwd": str(self.dir)})
        self.tools = install_fs_tools(self.ctx)

    def tearDown(self):
        self.ctx.dispose()
        self._tmp.cleanup()

    def _exec(self, cwd: str | None = None) -> ToolExec:
        session = Session("s1", meta={"cwd": cwd} if cwd else {})
        return ToolExec(agent=_Agent(session))

    async def test_read_line_numbers_and_window(self):
        (self.dir / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        value = await self.tools["read"].execute({"file_path": "a.txt"}, self._exec())
        self.assertEqual(value["totalLines"], 3)
        self.assertEqual([line["number"] for line in value["lines"]], [1, 2, 3])
        text = self.tools["read"].render({"file_path": "a.txt"}, value)[0]["text"]
        self.assertIn("1: one", text)
        self.assertIn("(End of file - total 3 lines)", text)
        windowed = await self.tools["read"].execute(
            {"file_path": "a.txt", "offset": 2, "limit": 1}, self._exec())
        self.assertEqual([line["text"] for line in windowed["lines"]], ["two"])
        self.assertIn("Use offset=3 to continue", self.tools["read"].render({}, windowed)[0]["text"])

    async def test_read_missing_and_offset_out_of_range(self):
        with self.assertRaises(FsError) as missing:
            await self.tools["read"].execute({"file_path": "nope.txt"}, self._exec())
        self.assertEqual(missing.exception.code, "FS_NOT_FOUND")
        (self.dir / "a.txt").write_text("one\n", encoding="utf-8")
        with self.assertRaises(FsError) as out:
            await self.tools["read"].execute({"file_path": "a.txt", "offset": 5}, self._exec())
        self.assertEqual(out.exception.code, "FS_NOT_FOUND")

    async def test_read_uses_session_cwd(self):
        session_dir = self.dir / "ws"
        session_dir.mkdir()
        (session_dir / "f.txt").write_text("hi\n", encoding="utf-8")
        value = await self.tools["read"].execute({"file_path": "f.txt"}, self._exec(str(session_dir)))
        # 结果 `path` 是 canonical process_path（mutationResult/session-cwd 对齐），
        # 而非 display_path——Windows 上会展开 8.3 短名。
        target = await self.fs.resolve("f.txt", {"cwd": str(session_dir)})
        self.assertEqual(value["path"], self.fs.process_path(target))

    async def test_write_create_and_update_without_policy(self):
        created = await self.tools["write"].execute(
            {"file_path": "out.txt", "content": "a\n"}, self._exec())
        self.assertEqual(created["operation"], "create")
        self.assertIsNone(created["before"])
        updated = await self.tools["write"].execute(
            {"file_path": "out.txt", "content": "b\n"}, self._exec())
        self.assertEqual(updated["operation"], "update")
        self.assertEqual(updated["before"], "a\n")
        self.assertEqual((self.dir / "out.txt").read_text(encoding="utf-8"), "b\n")
        text = self.tools["write"].render({}, updated)[0]["text"]
        self.assertIn("Updated file", text)

    async def test_observation_policy_guards_write_and_edit(self):
        install_fs_observation_policy(self.ctx)
        (self.dir / "g.txt").write_text("hello world\n", encoding="utf-8")
        exec_ = self._exec()
        with self.assertRaises(FsError) as unread:
            await self.tools["write"].execute(
                {"file_path": "g.txt", "content": "x\n"}, exec_)
        self.assertEqual(unread.exception.code, "FS_NOT_OBSERVED")
        self.assertIn("has not been read", str(unread.exception))
        read = await self.tools["read"].execute({"file_path": "g.txt"}, exec_)
        self.assertEqual(read["totalLines"], 1)
        edited = await self.tools["edit"].execute(
            {"file_path": "g.txt", "old_string": "hello", "new_string": "HI"}, exec_)
        self.assertEqual(edited["after"], "HI world\n")
        text = self.tools["edit"].render({"file_path": "g.txt"}, edited)[0]["text"]
        self.assertIn("has been updated successfully", text)

    async def test_edit_unconditional_without_policy(self):
        (self.dir / "e.txt").write_text("alpha beta alpha", encoding="utf-8")
        with self.assertRaises(FsError) as ambiguous:
            await self.tools["edit"].execute(
                {"file_path": "e.txt", "old_string": "alpha", "new_string": "A"}, self._exec())
        self.assertEqual(ambiguous.exception.code, "FS_AMBIGUOUS_EDIT")
        value = await self.tools["edit"].execute(
            {"file_path": "e.txt", "old_string": "alpha", "new_string": "A",
             "replace_all": True}, self._exec())
        self.assertEqual(value["after"], "A beta A")
        meta = self.tools["edit"].presentation_meta({"file_path": "e.txt"}, value)
        self.assertEqual(meta["operation"], "update")
        self.assertTrue(meta["diffs"])
        self.assertEqual(meta["diffs"][0]["path"], "e.txt")

    async def test_edit_argument_validation(self):
        with self.assertRaisesRegex(ValueError, "must differ"):
            await self.tools["edit"].execute(
                {"file_path": "x", "old_string": "a", "new_string": "a"}, self._exec())
        with self.assertRaisesRegex(ValueError, "old_string must be a non-empty"):
            await self.tools["edit"].execute(
                {"file_path": "x", "old_string": "", "new_string": "b"}, self._exec())

    async def test_write_presentation_meta_operation_and_diffs(self):
        tool = self.registry.resolve("write")
        created = await tool.execute(
            {"file_path": "m.txt", "content": "one\ntwo\n"}, self._exec())
        meta = tool.presentation_meta({"file_path": "m.txt"}, created)
        self.assertEqual(meta["operation"], "create")
        self.assertEqual(meta["diffs"], [])
        updated = await tool.execute(
            {"file_path": "m.txt", "content": "one\nTWO\n"}, self._exec())
        meta = tool.presentation_meta({"file_path": "m.txt"}, updated)
        self.assertEqual(meta["operation"], "update")
        self.assertTrue(meta["diffs"])
        self.assertEqual(meta["diffs"][0]["path"], "m.txt")

    async def test_pipeline_populates_write_meta(self):
        tool = self.registry.resolve("write")
        result = await run_pipeline_async(
            self.ctx, tool, {"file_path": "p.txt", "content": "hi\n"}, self._exec())
        self.assertTrue(result.ok)
        self.assertEqual(result.meta["operation"], "create")
        self.assertEqual(result.meta["diffs"], [])
        nested_exec = self._exec()
        nested_exec.parent = object()
        nested = await run_pipeline_async(
            self.ctx, tool, {"file_path": "q.txt", "content": "x"}, nested_exec)
        self.assertEqual(nested.meta, {})

    async def test_read_presentation_meta_carries_lang(self):
        (self.dir / "main.py").write_text("print(1)\n", encoding="utf-8")
        tool = self.registry.resolve("read")
        value = await tool.execute({"file_path": "main.py"}, self._exec())
        meta = tool.presentation_meta({"file_path": "main.py"}, value)
        self.assertEqual(meta["lang"], "py")
        self.assertEqual(meta["path"], value["path"])
        self.assertEqual(meta["totalLines"], 1)

    def test_read_presentation_meta_omits_unknown_lang(self):
        from miniharness.fs.tools import read_presentation_meta
        meta = read_presentation_meta({}, {"path": "notes.txt", "offset": 1,
                                           "lines": [], "totalLines": 0})
        self.assertNotIn("lang", meta)

    def test_sandbox_permissions_description_verbatim(self):
        from miniharness.fs.tools import sandbox_permissions_description
        self.assertEqual(
            sandbox_permissions_description("operation"),
            "The narrowest wider sandbox mode for a one-shot retry of the exact "
            "operation the sandbox just denied; the retry asks the user for approval.")

    async def test_tool_result_event_carries_presentation_meta(self):
        session = Session("s-meta")
        call_seq = append_tool_call(session, 1, 1, "c1", "write", "{}")
        emit_tool_result(
            session, 1, 1, "c1",
            ToolResult(ok=True, content="ok", value={},
                       meta={"operation": "create", "diffs": []}),
            call_seq)
        event = session.events[-1]
        self.assertEqual(event["type"], "tool/result")
        meta = event["data"]["meta"]
        self.assertEqual(meta["operation"], "create")
        self.assertEqual(list(meta["diffs"]), [])


    async def test_install_switches(self):
        ctx = Context(name="switches")
        registry = ToolRegistry(ctx)
        install_local_fs(ctx, {"cwd": str(self.dir)})
        install_fs_tools(ctx, read=False, write=False, edit=True)
        names = {name for name in ("read", "read_image", "write", "edit")
                 if registry.resolve(name) is not None}
        self.assertEqual(names, {"edit"})

    async def test_write_edit_result_path_is_process_path(self):
        observed = []
        self.ctx.on("fs/observed", lambda payload: observed.append(payload))
        created = await self.tools["write"].execute(
            {"file_path": "out.txt", "content": "a\n"}, self._exec())
        target = await self.fs.resolve("out.txt", {"cwd": str(self.dir)})
        self.assertEqual(created["path"], self.fs.process_path(target))
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0], target)
        self.assertEqual(observed[0][1].kind, "present")
        updated = await self.tools["edit"].execute(
            {"file_path": "out.txt", "old_string": "a", "new_string": "b"}, self._exec())
        self.assertEqual(updated["path"], self.fs.process_path(target))
        self.assertEqual(len(observed), 2)


class ReadImageToolsTestCase(unittest.IsolatedAsyncioTestCase):
    """read_image 组装/门禁/持久化（对齐 packages/fs/tool-fs src/read-image.ts）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self.ctx = Context(name="root")
        self.registry = ToolRegistry(self.ctx)
        self.fs = install_local_fs(self.ctx, {"cwd": str(self.dir)})
        self.attachments = LocalAttachmentStore(root=str(self.dir / "attachments"))
        self.ctx.provide("attachments", self.attachments)
        install_fs_tools(self.ctx)

    def tearDown(self):
        self.ctx.dispose()
        self._tmp.cleanup()

    def _exec(self, exec_adapter=None) -> ToolExec:
        session = Session("s-image")
        exec_adapter = exec_adapter if exec_adapter is not None else _Adapter()
        return ToolExec(agent=_Agent(session, adapter=exec_adapter))

    def _write_png(self, name="img.png", data=None):
        path = self.dir / name
        path.write_bytes(data if data is not None else TINY_PNG)
        return path

    async def test_read_image_end_to_end(self):
        self._write_png()
        tool = self.registry.resolve("read_image")
        self.assertIsNotNone(tool)
        value = await tool.execute({"file_path": "img.png"}, self._exec())
        target = await self.fs.resolve("img.png", {"cwd": str(self.dir)})
        self.assertEqual(value["path"], self.fs.process_path(target))
        self.assertEqual(value["image"]["mediaType"], "image/png")
        self.assertEqual(value["image"]["width"], 1)
        self.assertEqual(value["image"]["height"], 1)
        self.assertEqual(value["image"]["name"], "img.png")
        self.assertGreater(value["image"]["bytes"], 0)
        # 渲染 = 文本信封 + image block；meta 携带 canonical path。
        blocks = tool.render({"file_path": "img.png"}, value)
        self.assertEqual(blocks[0]["type"], "text")
        self.assertIn("<type>image</type>", blocks[0]["text"])
        self.assertIn("1x1 px", blocks[0]["text"])
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(blocks[1]["attachment"]["attachmentId"],
                         value["image"]["attachmentId"])
        self.assertEqual(tool.presentation_meta({}, value)["path"], value["path"])
        # 内容寻址去重：同名同字节重读复用同一 attachmentId。
        again = await tool.execute({"file_path": "img.png"}, self._exec())
        self.assertEqual(again["image"]["attachmentId"], value["image"]["attachmentId"])

    async def test_read_image_sniffs_extensionless_png(self):
        self._write_png("plain")
        value = await self.registry.resolve("read_image").execute(
            {"file_path": "plain"}, self._exec())
        self.assertEqual(value["image"]["mediaType"], "image/png")

    async def test_read_image_rejects_unsupported_extension_before_io(self):
        (self.dir / "notes.txt").write_text("hi", encoding="utf-8")
        with self.assertRaises(RuntimeError) as caught:
            await self.registry.resolve("read_image").execute(
                {"file_path": "notes.txt"}, self._exec())
        self.assertIn("does not declare a supported image format", str(caught.exception))

    async def test_read_image_rejects_format_mismatch(self):
        # 真 JPEG 字节 + .png 扩展名声明 → IMAGE_TYPE_MISMATCH 的可恢复改写
        import io
        from PIL import Image as PILImage
        buffer = io.BytesIO()
        PILImage.new("RGB", (1, 1)).save(buffer, format="JPEG")
        self._write_png("bad.png", data=buffer.getvalue())
        with self.assertRaises(RuntimeError) as caught:
            await self.registry.resolve("read_image").execute(
                {"file_path": "bad.png"}, self._exec())
        message = str(caught.exception)
        self.assertIn(".png", message)
        self.assertIn("different image format", message)

    async def test_read_image_rejects_garbage_declared_png_verbatim(self):
        # 无扩展名智慧的垃圾字节 + 声明扩展 → 原样抛存储校验错误（上游同样不匹记载）
        self._write_png("garbage.png", data=b"definitely not a png")
        with self.assertRaises(Exception) as caught:
            await self.registry.resolve("read_image").execute(
                {"file_path": "garbage.png"}, self._exec())
        self.assertIn("Unsupported or malformed image data", str(caught.exception))

    async def test_read_image_rejects_unsupported_content(self):
        # 无扩展名 + 非图像字节 → 嗅探失败，在任何存储前拒绝
        (self.dir / "blob").write_bytes(b"plain text")
        with self.assertRaises(RuntimeError) as caught:
            await self.registry.resolve("read_image").execute(
                {"file_path": "blob"}, self._exec())
        self.assertIn("not a supported image format", str(caught.exception))

    async def test_read_image_rejects_route_without_image_input(self):
        self._write_png()
        with self.assertRaises(RuntimeError) as caught:
            await self.registry.resolve("read_image").execute(
                {"file_path": "img.png"}, self._exec(_Adapter(modalities=("text",))))
        self.assertIn("does not declare image input", str(caught.exception))

    async def test_read_image_rejects_unresolved_route(self):
        self._write_png()
        with self.assertRaises(RuntimeError) as caught:
            await self.registry.resolve("read_image").execute(
                {"file_path": "img.png"}, self._exec(_Adapter(model=None)))
        self.assertIn("could not be resolved", str(caught.exception))

    async def test_read_image_requires_attachment_service(self):
        ctx = Context(name="no-attach")
        ToolRegistry(ctx)
        install_local_fs(ctx, {"cwd": str(self.dir)})
        install_fs_tools(ctx)
        self.assertIsNone(ctx.get("tools").resolve("read_image"))
        from miniharness.fs.tools import apply_read_image_tool
        apply_read_image_tool(ctx)
        with self.assertRaises(RuntimeError) as caught:
            await ctx.get("tools").resolve("read_image").execute(
                {"file_path": "img.png"}, self._exec())
        self.assertIn("no attachment service is mounted", str(caught.exception))

    def test_sniff_image_media_type(self):
        self.assertEqual(sniff_image_media_type(TINY_PNG), "image/png")
        self.assertEqual(sniff_image_media_type(b"RIFF1234WEBPVP8 "), "image/webp")
        self.assertEqual(sniff_image_media_type(b"\xff\xd8\xff\xe0xx"), "image/jpeg")
        self.assertEqual(sniff_image_media_type(b"GIF89a......"), "image/gif")
        self.assertIsNone(sniff_image_media_type(b"hello world"))

    async def test_install_switch_can_disable_read_image(self):
        ctx = Context(name="no-image")
        ToolRegistry(ctx)
        store = LocalAttachmentStore(root=str(self.dir / "att2"))
        ctx.provide("attachments", store)
        install_local_fs(ctx, {"cwd": str(self.dir)})
        install_fs_tools(ctx, read_image=False)
        self.assertIsNone(ctx.get("tools").resolve("read_image"))
        self.assertIsNotNone(ctx.get("tools").resolve("read"))


if __name__ == "__main__":
    unittest.main()
