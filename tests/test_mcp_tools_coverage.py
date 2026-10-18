"""mcp/tools 覆盖率补充：真实 MCP SDK 类型、真实附件 store、真实 PNG 光栅与
真实 agent/session 路由驱动 `miniharness/mcp/tools.py` 的投影、解码与受理路径，
不使用 mock / 假数据。
"""
import asyncio
import base64
import io
import tempfile
import threading
import unittest

from PIL import Image
from mcp.types import TextContent

from miniharness.attachment import LocalAttachmentStore, SaveImageAttachment
from miniharness.attachment.types import ImageAttachmentLimits
from miniharness.core.scope import Context
from miniharness.core.tools import ToolExec
from miniharness.llm import FakeLlmAdapter
from miniharness.mcp.tools import (
    ToolBridgeOptions,
    _as_json_schema,
    _image_diagnostic,
    _resolve_model_info,
    _structured_to_dict,
    contains_image,
    create_mcp_tool_definition,
    create_output,
    decode_image,
    extract_text,
    frost_equal,
    is_record,
    prepare_image_projection,
    project_content,
    public_tool_name,
    resolve_image_admission,
    sync_tools,
)

from tests.test_continuation import _parent_loop


def _png_bytes(width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for x in range(width):
        for y in range(height):
            pixels[x, y] = ((x * 8) % 256, (y * 8) % 256, 128)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _image_block(data: bytes = None, media_type: str = "image/png") -> dict:
    payload = data if data is not None else _png_bytes(2, 2)
    return {"type": "image", "mimeType": media_type,
            "data": base64.b64encode(payload).decode("ascii")}


class PureHelpersTest(unittest.TestCase):
    def test_public_tool_name_clean_and_hashed(self):
        self.assertEqual(public_tool_name("s", "x"), "mcp__s__x")
        hashed = public_tool_name("s", "do thing")
        self.assertTrue(hashed.startswith("mcp__s__do_thing_"))
        self.assertLessEqual(len(hashed), 64)

    def test_as_json_schema_dict(self):
        schema = {"type": "object"}
        self.assertIs(_as_json_schema(schema), schema)

    def test_as_json_schema_pydantic_model(self):
        dumped = _as_json_schema(TextContent(type="text", text="hi"))
        self.assertEqual(dumped["type"], "text")
        self.assertEqual(dumped["text"], "hi")

    def test_as_json_schema_unknown_returns_empty(self):
        self.assertEqual(_as_json_schema(object()), {})

    def test_structured_to_dict_pydantic(self):
        self.assertEqual(_structured_to_dict(TextContent(type="text", text="x")),
                         {"type": "text", "text": "x"})

    def test_structured_to_dict_passthrough(self):
        self.assertEqual(_structured_to_dict({"a": 1}), {"a": 1})

    def test_frost_equal_mapping_vs_sequence(self):
        self.assertFalse(frost_equal({"a": 1}, [1]))
        self.assertFalse(frost_equal([1], {"a": 1}))

    def test_frost_equal_mapping_key_mismatch(self):
        self.assertFalse(frost_equal({"a": 1}, {"b": 1}))

    def test_frost_equal_sequence_vs_scalar(self):
        self.assertFalse(frost_equal((1, 2), 5))

    def test_frost_equal_sequence_length_mismatch(self):
        self.assertFalse(frost_equal([1], [1, 2]))

    def test_frost_equal_sequence_equal(self):
        self.assertTrue(frost_equal((1, 2), [1, 2]))

    def test_frost_equal_int_and_bool(self):
        self.assertTrue(frost_equal(True, 1))
        self.assertFalse(frost_equal(1, 2))

    def test_frost_equal_scalars(self):
        self.assertTrue(frost_equal("a", "a"))
        self.assertFalse(frost_equal("a", "b"))

    def test_is_record_and_contains_image(self):
        self.assertTrue(is_record({}))
        self.assertFalse(is_record([1]))
        self.assertTrue(contains_image([{"type": "image"}]))
        self.assertFalse(contains_image([{"type": "text"}]))

    def test_image_diagnostic(self):
        self.assertIn("image/gif", _image_diagnostic({"mimeType": "image/gif"}, "why"))
        self.assertIn("unknown media type", _image_diagnostic({}, "why"))

    def test_extract_text_joins_text_and_placeholders_image(self):
        text = extract_text([{"type": "text", "text": "a"},
                             {"type": "text", "text": "b"},
                             _image_block()], "tool")
        self.assertTrue(text.startswith("a\nb\n[image unavailable:"))

    def test_create_output_render_text_only(self):
        schema, render = create_output("t", None)
        self.assertEqual(schema["required"], ["content"])
        self.assertEqual(render({"args": 1}, {"content": [{"type": "text", "text": "hi"}]}),
                         [{"type": "text", "text": "hi"}])


class ProjectContentTest(unittest.TestCase):
    def test_all_block_variants(self):
        content = [
            "not-a-record",
            {"type": "text", "text": "hello"},
            {"type": "text"},
            {"type": "image", "mimeType": "image/png"},
            {"type": "resource_link", "name": "n"},
            {"type": "resource_link", "name": "n", "uri": "u"},
            {"type": "audio", "mimeType": "audio/mp3"},
            {"type": "audio"},
            {"type": "resource"},
            {"type": "mystery"},
        ]
        projected = project_content(content, "tool")
        texts = [block["text"] for block in projected]
        joined = "\n".join(texts)
        self.assertIn("hello", texts[0])
        self.assertTrue(any("image unavailable" in t for t in texts))
        self.assertTrue(any("resource link unavailable" in t for t in texts))
        self.assertIn("Resource link: n (u)", joined)
        self.assertIn("audio result unsupported: audio/mp3", joined)
        self.assertIn("audio result unsupported: unknown media type", joined)
        self.assertIn("embedded resource unsupported", joined)
        self.assertIn("unsupported MCP content type: mystery", joined)

    def test_image_splits_text_runs_in_place(self):
        projected = project_content([
            {"type": "text", "text": "before"},
            {"type": "image", "mimeType": "image/png"},
            {"type": "text", "text": "after"},
        ], "tool")
        self.assertEqual(projected[0], {"type": "text", "text": "before"})
        self.assertEqual(projected[1]["type"], "text")
        self.assertEqual(projected[2], {"type": "text", "text": "after"})

    def test_custom_image_callback(self):
        projected = project_content(
            [{"type": "image", "mimeType": "image/png"}], "tool",
            image=lambda block, index: {"type": "text", "text": f"at-{index}"})
        self.assertEqual(projected, [{"type": "text", "text": "at-0"}])

    def test_empty_content_placeholder(self):
        self.assertEqual(project_content([], "tool"),
                         [{"type": "text", "text": "(tool returned no model-visible content)"}])


class DecodeImageTest(unittest.TestCase):
    def test_success(self):
        raw = _png_bytes(3, 3)
        attachment = decode_image(_image_block(raw))
        self.assertEqual(attachment.mediaType, "image/png")
        self.assertEqual(attachment.data, raw)

    def test_rejects_unsupported_media_type(self):
        with self.assertRaises(RuntimeError):
            decode_image({"type": "image", "mimeType": "image/tiff",
                          "data": base64.b64encode(_png_bytes(2, 2)).decode()})

    def test_rejects_non_canonical_base64(self):
        with self.assertRaises(RuntimeError):
            decode_image({"type": "image", "mimeType": "image/png", "data": "not base64!"})

    def test_rejects_non_string_data(self):
        with self.assertRaises(RuntimeError):
            decode_image({"type": "image", "mimeType": "image/png", "data": 1234})


class SyncToolsTest(unittest.TestCase):
    def test_without_capabilities_requires_registry(self):
        ctx = Context(name="no-tools")
        self.addCleanup(ctx.dispose)
        opts = ToolBridgeOptions("contain", "s", 1000)
        with self.assertRaises(RuntimeError):
            asyncio.run(sync_tools(None, ctx, opts, {}))


class ImageProjectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent, self.ctx, self.reg = _parent_loop(
            session_id="parent", adapter=FakeLlmAdapter(final_text="ok"))
        self.addCleanup(self.ctx.dispose)
        self.parent.session.append(
            "request/header",
            {"header": {"config": {"provider": "fake", "model": "fake-model"}}})
        self.parent.options = {"provider": "fake", "model": "fake-model"}
        self.store = LocalAttachmentStore(root=self.tmp.name)
        self.ctx.provide("attachments", self.store)

    def test_resolve_admission_success(self):
        exec_ = ToolExec(agent=self.parent)
        self.assertIs(resolve_image_admission(self.ctx, exec_), self.store)

    def test_resolve_admission_missing_store(self):
        ctx = Context(name="bare")
        self.addCleanup(ctx.dispose)
        exec_ = ToolExec(agent=self.parent)
        with self.assertRaises(RuntimeError):
            resolve_image_admission(ctx, exec_)

    def test_resolve_admission_unresolved_route(self):
        exec_ = ToolExec(agent=None)
        with self.assertRaises(RuntimeError):
            resolve_image_admission(self.ctx, exec_)

    def test_resolve_admission_canceled_signal(self):
        signal = threading.Event()
        signal.set()
        exec_ = ToolExec(agent=self.parent, signal=signal)
        with self.assertRaises(RuntimeError):
            resolve_image_admission(self.ctx, exec_)

    def test_resolve_model_info_from_llm_service(self):
        ctx = Context(name="llm-only")
        self.addCleanup(ctx.dispose)
        ctx.provide("llm", FakeLlmAdapter())
        info = _resolve_model_info(ctx, None)
        self.assertIn("image", info["input_modalities"])

    def test_resolve_model_info_none_without_sources(self):
        ctx = Context(name="no-llm")
        self.addCleanup(ctx.dispose)
        self.assertIsNone(_resolve_model_info(ctx, None))

    def test_prepare_projection_success(self):
        exec_ = ToolExec(agent=self.parent)
        projected = prepare_image_projection(self.ctx, exec_, [_image_block()], "tool")
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0]["type"], "image")
        self.assertIn("attachment", projected[0])

    def test_prepare_projection_decode_failure(self):
        exec_ = ToolExec(agent=self.parent)
        content = [_image_block(media_type="image/tiff"), _image_block()]
        projected = prepare_image_projection(self.ctx, exec_, content, "tool")
        texts = [b["text"] for b in projected]
        self.assertTrue(any("not PNG, JPEG" in t for t in texts))
        self.assertTrue(any("another image in the same result was invalid" in t
                            for t in texts))

    def test_prepare_projection_admission_failure(self):
        ctx = Context(name="no-attachments")
        self.addCleanup(ctx.dispose)
        ctx.provide("llm", FakeLlmAdapter())
        exec_ = ToolExec(agent=None)
        projected = prepare_image_projection(ctx, exec_, [_image_block()], "tool")
        self.assertEqual(projected[0]["type"], "text")
        self.assertIn("no attachment store is mounted", projected[0]["text"])

    def test_prepare_projection_storage_failure(self):
        limits = ImageAttachmentLimits(
            maxImageBytes=100_000_000, maxImagesPerMessage=20,
            maxMessageImageBytes=128, maxImagePixels=40_000_000)
        ctx = Context(name="tight-store")
        self.addCleanup(ctx.dispose)
        ctx.provide("attachments", LocalAttachmentStore(root=self.tmp.name, limits=limits))
        ctx.provide("llm", FakeLlmAdapter())
        exec_ = ToolExec(agent=self.parent)
        projected = prepare_image_projection(
            ctx, exec_, [_image_block()] * 3, "tool")
        self.assertTrue(all(block["type"] == "text" for block in projected))
        self.assertTrue(any("image admission rejected the result" in block["text"]
                            for block in projected))


class ToolDefinitionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent, self.ctx, self.reg = _parent_loop(
            session_id="parent", adapter=FakeLlmAdapter(final_text="ok"))
        self.addCleanup(self.ctx.dispose)
        self.parent.session.append(
            "request/header",
            {"header": {"config": {"provider": "fake", "model": "fake-model"}}})
        self.parent.options = {"provider": "fake", "model": "fake-model"}
        self.ctx.provide("attachments", LocalAttachmentStore(root=self.tmp.name))

    def _tool(self, call, *, task_required=False, output_schema=None):
        return create_mcp_tool_definition(self.ctx, {
            "name": "mcp__s__shot",
            "rawName": "shot",
            "description": "d",
            "inputSchema": {"type": "object"},
            "outputSchema": output_schema,
            "taskRequired": task_required,
            "call": call,
        })

    def test_task_required_raises(self):
        async def call(args, exec_):
            return {"content": []}
        tool = self._tool(call, task_required=True)
        with self.assertRaises(RuntimeError):
            asyncio.run(tool.execute({}, ToolExec(agent=self.parent)))

    def test_execute_text_and_structured(self):
        async def call(args, exec_):
            return {"content": [{"type": "text", "text": "hi"}],
                    "structuredContent": TextContent(type="text", text="s")}
        tool = self._tool(call)
        value = asyncio.run(tool.execute({}, ToolExec(agent=self.parent)))
        self.assertEqual(value["content"], [{"type": "text", "text": "hi"}])
        self.assertEqual(value["structuredContent"], {"type": "text", "text": "s"})

    def test_execute_error_raises(self):
        async def call(args, exec_):
            return {"content": [{"type": "text", "text": "boom"}], "isError": True}
        tool = self._tool(call)
        with self.assertRaises(RuntimeError):
            asyncio.run(tool.execute({}, ToolExec(agent=self.parent)))

    def test_execute_image_and_project_content(self):
        content = [{"type": "text", "text": "shot"},
                   _image_block(),
                   _image_block()]
        async def call(args, exec_):
            return {"content": content}
        tool = self._tool(call)

        exec_ = ToolExec(agent=self.parent)
        value = asyncio.run(tool.execute({}, exec_))
        fallback = [{"type": "text", "text": extract_text(value["content"], "shot")}]

        # 无投影的执行句柄 → None
        self.assertIsNone(tool.project_content(ToolExec(agent=self.parent),
                                               {"value": value, "content": fallback}))

        # is_error → None
        self.assertIsNone(tool.project_content(exec_, {"is_error": True}))

        # value 深不等 → None
        exec_ = ToolExec(agent=self.parent)
        asyncio.run(tool.execute({}, exec_))
        self.assertIsNone(tool.project_content(exec_, {"value": {"content": []},
                                                       "content": fallback}))

        # content 与 fallback 深不等 → None
        exec_ = ToolExec(agent=self.parent)
        asyncio.run(tool.execute({}, exec_))
        self.assertIsNone(tool.project_content(exec_, {"value": value,
                                                       "content": []}))

        # 三条件一致 → 返回图片投影
        exec_ = ToolExec(agent=self.parent)
        asyncio.run(tool.execute({}, exec_))
        projected = tool.project_content(exec_, {"value": value, "content": fallback})
        self.assertIsNotNone(projected)
        self.assertTrue(any(block["type"] == "image" for block in projected))


if __name__ == "__main__":
    unittest.main()
