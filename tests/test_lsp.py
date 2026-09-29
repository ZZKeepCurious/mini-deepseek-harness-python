"""LSP 工具族：seam（ctx.lsp）+ stdio provider（lsp-stdio）+ 模型工具（tool-lsp）。

对齐 packages/lsp/{lsp,lsp-stdio,tool-lsp}。端到端用 `tests/lsp_stub_server.py`
桩服务器（LSP base protocol over stdio）。
"""
import asyncio
import os
import sys
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.fs import install_local_fs
from miniharness.lsp import (
    LSP_OPERATIONS,
    Lsp,
    LspError,
    final_extension,
    install_lsp,
)
from miniharness.lsp_stdio.abort import abort_error, abortable, signal_aborted
from miniharness.lsp_stdio.framing import MAX_HEADER_BYTES, MessageDecoder, encode_message
from miniharness.lsp_stdio.host import canonicalize_workspace, read_host_source
from miniharness.lsp_stdio.translate import (
    negotiate_position_encoding,
    normalize_hover,
    normalize_locations,
    request_method,
    supports_operation,
    supports_transient_open,
)
from miniharness.subprocess import install_subprocess
from miniharness.tool_lsp import (
    LSP_PROMPT_TEXT,
    TOOL_LSP_SECTION_ORDER,
    create_lsp_tool,
)
from miniharness.tool_lsp.render import (
    format_hover,
    format_locations,
    parse_lsp_args,
    present_lsp_call,
    render_uri,
)

STUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lsp_stub_server.py")


class _AbortSignal:
    """一个已置位的鸭子类型取消信号（threading.Event 形状）。"""

    def is_set(self) -> bool:
        return True


class TestFraming(unittest.TestCase):
    def test_encode_decode_round_trip(self):
        message = {"jsonrpc": "2.0", "id": 1, "method": "x", "params": {"a": "b"}}
        framed = encode_message(message)
        self.assertTrue(framed.startswith(b"Content-Length: "))
        decoder = MessageDecoder(1_000_000)
        self.assertEqual(decoder.push(framed), [message])

    def test_multiple_messages_and_chunking(self):
        decoder = MessageDecoder(1_000_000)
        stream = encode_message({"id": 1}) + encode_message({"id": 2})
        out = []
        for index in range(0, len(stream), 3):
            out.extend(decoder.push(stream[index:index + 3]))
        self.assertEqual(out, [{"id": 1}, {"id": 2}])

    def test_ignores_other_headers(self):
        body = b'{"id":1}'
        framed = b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n" \
                 b"Content-Length: 8\r\n\r\n" + body
        self.assertEqual(MessageDecoder(1_000_000).push(framed), [{"id": 1}])

    def test_oversize_message_rejected(self):
        decoder = MessageDecoder(4)
        with self.assertRaises(ValueError):
            decoder.push(encode_message({"id": 1}))

    def test_missing_content_length_rejected(self):
        with self.assertRaises(ValueError):
            MessageDecoder(1_000_000).push(b"X: 1\r\n\r\n")

    def test_header_without_terminator_bounded(self):
        decoder = MessageDecoder(1_000_000)
        with self.assertRaises(ValueError):
            decoder.push(b"x" * (MAX_HEADER_BYTES + 1))


class TestTranslate(unittest.TestCase):
    def test_request_method(self):
        self.assertEqual(request_method("goToDefinition"), "textDocument/definition")
        self.assertEqual(request_method("findReferences"), "textDocument/references")
        self.assertEqual(request_method("goToImplementation"), "textDocument/implementation")
        self.assertEqual(request_method("hover"), "textDocument/hover")

    def test_supports_operation(self):
        caps = {"definitionProvider": True, "hoverProvider": {}}
        self.assertTrue(supports_operation(caps, "goToDefinition"))
        self.assertTrue(supports_operation(caps, "hover"))
        self.assertFalse(supports_operation(caps, "findReferences"))
        self.assertFalse(supports_operation({"definitionProvider": False}, "goToDefinition"))

    def test_supports_transient_open(self):
        self.assertTrue(supports_transient_open(1))
        self.assertTrue(supports_transient_open(2))
        self.assertFalse(supports_transient_open(0))
        self.assertFalse(supports_transient_open(None))
        self.assertTrue(supports_transient_open({"openClose": True}))
        self.assertFalse(supports_transient_open({"change": 1}))

    def test_negotiate_position_encoding(self):
        self.assertEqual(negotiate_position_encoding(None), "utf-16")
        self.assertEqual(negotiate_position_encoding("utf-16"), "utf-16")
        with self.assertRaises(ValueError):
            negotiate_position_encoding("utf-8")

    def test_normalize_locations(self):
        location = {"uri": "file:///a.ts",
                    "range": {"start": {"line": 1, "character": 2},
                              "end": {"line": 1, "character": 5}}}
        self.assertEqual(normalize_locations(None), [])
        self.assertEqual(normalize_locations(location), [location])
        link = {"targetUri": "file:///b.ts",
                "targetSelectionRange": {"start": {"line": 0, "character": 0},
                                         "end": {"line": 0, "character": 3}}}
        self.assertEqual(normalize_locations(link)[0]["uri"], "file:///b.ts")
        with self.assertRaises(LspError) as cm:
            normalize_locations([42])
        self.assertEqual(cm.exception.code, "LSP_MALFORMED_RESPONSE")

    def test_normalize_hover(self):
        self.assertIsNone(normalize_hover(None))
        self.assertEqual(normalize_hover({"contents": {"kind": "markdown", "value": "x"}}),
                         {"contents": "x"})
        self.assertEqual(normalize_hover({"contents": "plain"}), {"contents": "plain"})
        self.assertEqual(
            normalize_hover({"contents": [{"language": "ts", "value": "y"}, "z"]}),
            {"contents": "```ts\ny\n```\n\nz"})
        with self.assertRaises(LspError):
            normalize_hover({"contents": 42})


class TestLspSeam(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="t")
        self.addCleanup(self.ctx.dispose)
        self.lsp = install_lsp(self.ctx)
        self._handled = []

    def _provider(self, pid, mapping):
        seam = self

        class _Provider:
            def __init__(self):
                self.id = pid
                self.extension_to_language = mapping

            async def query(self, request, signal=None):
                seam._handled.append(request)
                return {"kind": "locations", "locations": [],
                        "resolvedWorkspaceUri": request["workspaceRoot"]}

        return _Provider()

    def test_final_extension(self):
        self.assertEqual(final_extension("foo.d.ts"), ".ts")
        self.assertEqual(final_extension("Foo.TS"), ".ts")
        self.assertEqual(final_extension("dir/file.py"), ".py")
        self.assertEqual(final_extension("dir\\file.py"), ".py")
        self.assertEqual(final_extension(".bashrc"), "")
        self.assertEqual(final_extension("noext"), "")

    def test_register_and_route(self):
        dispose = self.lsp.register_provider(self._provider("py", {".py": "python"}))
        result = asyncio.run(self.lsp.query({
            "operation": "hover", "filePath": "a.py",
            "position": {"line": 0, "character": 0}, "workspaceRoot": "/w"}))
        self.assertEqual(result["kind"], "locations")
        self.assertEqual(self._handled[0]["languageId"], "python")
        dispose()
        with self.assertRaises(LspError) as cm:
            asyncio.run(self.lsp.query({
                "operation": "hover", "filePath": "a.py",
                "position": {"line": 0, "character": 0}, "workspaceRoot": "/w"}))
        self.assertEqual(cm.exception.code, "LSP_UNAVAILABLE")

    def test_register_validations(self):
        with self.assertRaises(LspError) as cm:
            self.lsp.register_provider(self._provider("", {".py": "python"}))
        self.assertEqual(cm.exception.code, "LSP_INVALID_PROVIDER")
        with self.assertRaises(LspError):
            self.lsp.register_provider(self._provider("py", {}))
        with self.assertRaises(LspError):
            self.lsp.register_provider(self._provider("py", {"py": ""}))
        with self.assertRaises(LspError):
            self.lsp.register_provider(self._provider("py", {"a/b": "python"}))

    def test_register_conflict_rolls_back(self):
        self.lsp.register_provider(self._provider("a", {".py": "python"}))
        with self.assertRaises(LspError) as cm:
            self.lsp.register_provider(self._provider("b", {".py": "python"}))
        self.assertEqual(cm.exception.code, "LSP_CONFLICT")
        # 冲突注册不发布任何东西：b 的 id 未被占用。
        self.lsp.register_provider(self._provider("b", {".rb": "ruby"}))

    def test_intra_provider_duplicate_extension(self):
        with self.assertRaises(LspError):
            self.lsp.register_provider(self._provider("a", {".TS": "typescript", ".ts": "ts"}))


class TestAbort(unittest.TestCase):
    def test_signal_aborted(self):
        self.assertFalse(signal_aborted(None))
        self.assertTrue(signal_aborted(_AbortSignal()))

    def test_abortable_raises_when_pre_aborted(self):
        async def run():
            future = asyncio.get_running_loop().create_future()
            future.set_result(1)
            with self.assertRaises(Exception):
                await abortable(future, _AbortSignal())
        asyncio.run(run())

    def test_abortable_returns_when_complete(self):
        async def run():
            async def work():
                return 7
            return await abortable(work(), None)
        self.assertEqual(asyncio.run(run()), 7)


class TestHost(unittest.IsolatedAsyncioTestCase):
    async def test_canonicalize_and_read(self):
        with tempfile.TemporaryDirectory() as ws:
            src = os.path.join(ws, "a.py")
            with open(src, "w", encoding="utf-8", newline="") as handle:
                handle.write("print('hi')\n")
            ctx = Context(name="t")
            self.addCleanup(ctx.dispose)
            fs = install_local_fs(ctx, {"cwd": ws})
            workspace = await canonicalize_workspace(fs, ws)
            self.assertEqual(workspace["canonical_path"], os.path.realpath(ws))
            self.assertTrue(workspace["file_url"].startswith("file:"))
            source = await read_host_source(fs, "a.py", workspace, 1_000_000)
            self.assertEqual(source["text"], "print('hi')\n")

    async def test_read_rejects_outside_workspace(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as outside:
            with open(os.path.join(outside, "b.py"), "w", encoding="utf-8", newline="") as handle:
                handle.write("x\n")
            ctx = Context(name="t")
            self.addCleanup(ctx.dispose)
            fs = install_local_fs(ctx, {"cwd": ws})
            workspace = await canonicalize_workspace(fs, ws)
            with self.assertRaises(RuntimeError):
                await read_host_source(
                    fs, os.path.join(outside, "b.py"), workspace, 1_000_000)

    async def test_read_enforces_byte_cap(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "big.py"), "w", encoding="utf-8", newline="") as handle:
                handle.write("x" * 100)
            ctx = Context(name="t")
            self.addCleanup(ctx.dispose)
            fs = install_local_fs(ctx, {"cwd": ws})
            workspace = await canonicalize_workspace(fs, ws)
            with self.assertRaises(RuntimeError):
                await read_host_source(fs, "big.py", workspace, 10)


class TestLspStdio(unittest.IsolatedAsyncioTestCase):
    async def _boot(self, mode=None, filename="a.py"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = os.path.abspath(tmp.name)
        self.ws = ws
        with open(os.path.join(ws, filename), "w", encoding="utf-8", newline="") as handle:
            handle.write("print('hi')\n")
        ctx = Context(name="t")
        self.addCleanup(ctx.dispose)
        install_local_fs(ctx, {"cwd": ws})
        install_subprocess(ctx)
        install_lsp(ctx)
        from miniharness.lsp_stdio import install_lsp_stdio

        args = [STUB] + (["--mode", mode] if mode else [])
        runtime = install_lsp_stdio(ctx, {"servers": {"stub": {
            "command": sys.executable, "args": args,
            "extensionToLanguage": {".py": "python"},
            "killGraceMs": 1000, "shutdownTimeoutMs": 1000}}})
        self.addAsyncCleanup(runtime.dispose)
        return ctx

    async def _query(self, ctx, operation, **extra):
        request = {"operation": operation, "filePath": extra.pop("filePath", "a.py"),
                   "position": extra.pop("position", {"line": 0, "character": 0}),
                   "workspaceRoot": self.ws, **extra}
        return await ctx.get("lsp").query(request)

    async def test_definition(self):
        ctx = await self._boot()
        result = await self._query(ctx, "goToDefinition")
        self.assertEqual(result["kind"], "locations")
        self.assertEqual(len(result["locations"]), 1)
        self.assertTrue(result["locations"][0]["uri"].endswith("a.py"))

    async def test_references_location_and_link(self):
        ctx = await self._boot()
        result = await self._query(ctx, "findReferences")
        self.assertEqual(len(result["locations"]), 2)

    async def test_hover(self):
        ctx = await self._boot()
        result = await self._query(ctx, "hover")
        self.assertEqual(result["kind"], "hover")
        self.assertEqual(result["hover"]["contents"], "**stub hover**")

    async def test_implementation_empty(self):
        ctx = await self._boot()
        result = await self._query(ctx, "goToImplementation")
        self.assertEqual(result["locations"], [])

    async def test_unsupported_operation(self):
        ctx = await self._boot(mode="no-definition")
        with self.assertRaises(LspError) as cm:
            await self._query(ctx, "goToDefinition")
        self.assertEqual(cm.exception.code, "LSP_UNSUPPORTED_OPERATION")

    async def test_transient_open_unsupported(self):
        ctx = await self._boot(mode="no-openclose")
        with self.assertRaises(LspError) as cm:
            await self._query(ctx, "goToDefinition")
        self.assertEqual(cm.exception.code, "LSP_UNSUPPORTED_OPERATION")

    async def test_position_encoding_rejected(self):
        ctx = await self._boot(mode="utf-8")
        with self.assertRaises(Exception):
            await self._query(ctx, "goToDefinition")

    async def test_malformed_response(self):
        ctx = await self._boot(mode="malformed")
        with self.assertRaises(LspError) as cm:
            await self._query(ctx, "goToDefinition")
        self.assertEqual(cm.exception.code, "LSP_MALFORMED_RESPONSE")

    async def test_crash_on_open_raises(self):
        ctx = await self._boot(mode="crash-on-open")
        with self.assertRaises(Exception):
            await self._query(ctx, "goToDefinition")

    async def test_unknown_extension_unavailable(self):
        ctx = await self._boot()
        with self.assertRaises(LspError) as cm:
            await self._query(ctx, "goToDefinition", filePath="a.rb")
        self.assertEqual(cm.exception.code, "LSP_UNAVAILABLE")

    async def test_shutdown_ignoring_force_terminates(self):
        ctx = await self._boot(mode="exit-ignoring")
        result = await self._query(ctx, "goToDefinition")
        self.assertEqual(result["kind"], "locations")


class TestToolLsp(unittest.IsolatedAsyncioTestCase):
    def test_parse_and_render_helpers(self):
        parsed = parse_lsp_args({"operation": "hover", "file_path": "a.py",
                                 "line": 3, "character": 7})
        self.assertEqual(parsed["position"], {"line": 2, "character": 6})
        with self.assertRaises(ValueError):
            parse_lsp_args({"operation": "bogus", "file_path": "a", "line": 1,
                            "character": 1})
        with self.assertRaises(ValueError):
            parse_lsp_args({"operation": "hover", "file_path": "a", "line": 0,
                            "character": 1})
        rendered = format_locations(
            [{"uri": "file:///w/a.py",
              "range": {"start": {"line": 2, "character": 1},
                        "end": {"line": 2, "character": 4}}}], "file:///w", 100, 16000)
        self.assertEqual(rendered, "a.py:3:2")
        self.assertEqual(format_locations([], "file:///w", 100, 16000), "No results.")
        self.assertEqual(format_hover(None, 100), "No hover information.")
        self.assertIn("truncated", format_hover({"contents": "x" * 200}, 50))
        self.assertEqual(render_uri("file:///w/sub/a.py", "file:///w"), "sub/a.py")
        self.assertEqual(render_uri("https://x/y", "file:///w"), "https://x/y")
        call = present_lsp_call({"operation": "hover", "file_path": "a.py",
                                 "line": 1, "character": 2})
        self.assertEqual(call["title"], "LSP hover a.py:1:2")

    def test_render_caps_locations(self):
        locations = [{"uri": f"file:///w/f{i}.py",
                      "range": {"start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 1}}}
                     for i in range(5)]
        rendered = format_locations(locations, "file:///w", 2, 16000)
        self.assertIn("omitted (limit 2)", rendered)

    async def test_tool_execute_and_render(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = os.path.abspath(tmp.name)
        with open(os.path.join(ws, "a.py"), "w", encoding="utf-8", newline="") as handle:
            handle.write("print('hi')\n")
        ctx = Context(name="t")
        self.addCleanup(ctx.dispose)
        install_local_fs(ctx, {"cwd": ws})
        install_subprocess(ctx)
        install_lsp(ctx)
        from miniharness.lsp_stdio import install_lsp_stdio

        runtime = install_lsp_stdio(ctx, {"servers": {"stub": {
            "command": sys.executable, "args": [STUB],
            "extensionToLanguage": {".py": "python"}}}})
        self.addAsyncCleanup(runtime.dispose)
        tool = create_lsp_tool(ctx, {"maxLocations": 100, "maxResultChars": 16000})
        session = _FakeSession(ws)
        exec_ = _FakeExec(session)
        value = await tool.execute(
            {"operation": "goToDefinition", "file_path": "a.py",
             "line": 1, "character": 1}, exec_)
        self.assertEqual(value["kind"], "locations")
        rendered = tool.render(
            {"operation": "goToDefinition", "file_path": "a.py",
             "line": 1, "character": 1}, value)
        self.assertEqual(rendered[0]["type"], "text")
        self.assertIn("a.py:1:1", rendered[0]["text"])

    async def test_tool_requires_session_cwd(self):
        ctx = Context(name="t")
        self.addCleanup(ctx.dispose)
        install_lsp(ctx)
        tool = create_lsp_tool(ctx)
        with self.assertRaises(LspError) as cm:
            await tool.execute(
                {"operation": "hover", "file_path": "a.py", "line": 1, "character": 1},
                _FakeExec(None))
        self.assertEqual(cm.exception.code, "LSP_WORKSPACE_REQUIRED")

    def test_prompt_text_and_order(self):
        self.assertEqual(TOOL_LSP_SECTION_ORDER, 2200)
        self.assertIn("definitions", LSP_PROMPT_TEXT)
        self.assertIn("findReferences", LSP_OPERATIONS)

    def test_default_tools_mounts_lsp_when_seam_present(self):
        from miniharness.cli.default_tools import default_tools
        from miniharness.core.system_prompt import install_system_prompt

        ctx = Context(name="t")
        self.addCleanup(ctx.dispose)
        install_local_fs(ctx, {"cwd": os.getcwd()})
        install_subprocess(ctx)
        install_lsp(ctx)
        install_system_prompt(ctx)
        reg = default_tools(ctx)
        self.assertIsNotNone(reg.resolve("lsp"))


class _FakeSession:
    def __init__(self, cwd):
        self.meta = {"cwd": cwd}


class _FakeAgent:
    def __init__(self, session):
        self.session = session


class _FakeExec:
    def __init__(self, session):
        self.agent = _FakeAgent(session) if session is not None else None
        self.signal = None


if __name__ == "__main__":
    unittest.main()
