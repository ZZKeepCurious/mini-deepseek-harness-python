"""officeToPdf Remote 与 LibreOffice 转换面。"""
import asyncio
import os
import tempfile
import unittest
import zipfile

from miniharness.core.scope import Context
from miniharness.fs import install_local_fs
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.office_to_pdf import install_office_to_pdf, resolve_soffice
from miniharness.seams.sandbox_policy import SandboxPolicyService
from miniharness.web.api import WebApi
from miniharness.workspace import install_workspaces
from miniharness.workspace_controller import install_workspace_controller
from miniharness.workspace_files import install_workspace_files


def _write_minimal_odt(path: str) -> None:
    content = b"""<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
  <office:body><office:text><text:p>office preview test</text:p></office:text></office:body>
</office:document-content>"""
    manifest = b"""<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">
  <manifest:file-entry manifest:media-type="application/vnd.oasis.opendocument.text"
    manifest:full-path="/"/>
  <manifest:file-entry manifest:media-type="text/xml" manifest:full-path="content.xml"/>
</manifest:manifest>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", b"application/vnd.oasis.opendocument.text")
        archive.writestr("content.xml", content)
        archive.writestr("META-INF/manifest.xml", manifest)


class OfficeToPdfTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = os.path.join(self._tmp.name, "work")
        os.makedirs(self.work)
        self.ctx = Context(name="office-to-pdf-test")
        self.addCleanup(self.ctx.dispose)
        SandboxPolicyService(self.ctx, {"mode": "danger-full-access"})
        install_local_fs(self.ctx, {"cwd": self.work})
        install_workspaces(self.ctx, root=os.path.join(self._tmp.name, "home"))
        install_workspace_controller(self.ctx)
        install_workspace_files(self.ctx)
        install_office_to_pdf(self.ctx)
        self.api = WebApi(self.ctx, FakeLlmAdapter())
        self.session_id = self._value(
            self.api.dispatch("session.create", "r0", {"cwd": self.work}))["sessionId"]

    def _value(self, response):
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]

    def _error(self, response):
        self.assertFalse(response["result"]["ok"])
        return response["result"]["error"]

    def test_get_generation_route(self):
        value = self._value(self.api.dispatch("officeToPdf/getGeneration", "g1", {}))
        self.assertIn("generation", value)
        self.assertTrue(str(value["generation"]).startswith("gen_"))

    def test_render_rejects_non_office_extension(self):
        bad = os.path.join(self.work, "notes.txt")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write("plain")
        error = self._error(self.api.dispatch(
            "officeToPdf/render", "r1",
            {"workspaceFileScopeId": self.session_id, "path": bad}))
        self.assertEqual(error["code"], "office-to-pdf/invalid-input")

    def test_render_engine_unavailable_without_soffice(self):
        from miniharness import office_to_pdf as module
        original = module.resolve_soffice
        module.resolve_soffice = lambda: None
        self.addCleanup(setattr, module, "resolve_soffice", original)
        doc = os.path.join(self.work, "report.docx")
        with open(doc, "wb") as handle:
            handle.write(b"not a real docx")
        error = self._error(self.api.dispatch(
            "officeToPdf/render", "r2",
            {"workspaceFileScopeId": self.session_id, "path": doc}))
        self.assertEqual(error["code"], "office-to-pdf/engine-unavailable")


@unittest.skipUnless(resolve_soffice(), "LibreOffice soffice not on PATH")
class OfficeToPdfIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = os.path.join(self._tmp.name, "work")
        os.makedirs(self.work)
        self.ctx = Context(name="office-to-pdf-int")
        self.addCleanup(self.ctx.dispose)
        SandboxPolicyService(self.ctx, {"mode": "danger-full-access"})
        install_local_fs(self.ctx, {"cwd": self.work})
        install_workspaces(self.ctx, root=os.path.join(self._tmp.name, "home"))
        install_workspace_controller(self.ctx)
        install_workspace_files(self.ctx)
        install_office_to_pdf(self.ctx)
        self.api = WebApi(self.ctx, FakeLlmAdapter())
        self.session_id = self.api.dispatch("session.create", "r0", {"cwd": self.work})
        self.session_id = self.session_id["result"]["value"]["sessionId"]
        self.odt = os.path.join(self.work, "sample.odt")
        _write_minimal_odt(self.odt)

    def test_render_returns_pdf_bytes(self):
        response = self.api.dispatch(
            "officeToPdf/render", "r3",
            {"workspaceFileScopeId": self.session_id, "path": self.odt})
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        value = response["result"]["value"]
        data = value["data"]
        self.assertIsInstance(data, (bytes, bytearray))
        self.assertTrue(bytes(data).startswith(b"%PDF"))


class RemoteAccessFollowTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="remote-access-test")
        self.addCleanup(self.ctx.dispose)
        self.api = WebApi(self.ctx, FakeLlmAdapter())

    def test_follow_stream_owner_baseline(self):
        async def run():
            stream = self.api.gateway.open_stream("remoteAccess/follow", {"args": {}})
            frame = await stream.__anext__()
            await stream.aclose()
            return frame

        frame = asyncio.run(run())
        self.assertEqual(frame, {"type": "baseline", "owner": True})
