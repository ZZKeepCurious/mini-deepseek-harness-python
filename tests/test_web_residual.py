"""web 残余 api 控制器：workspace / workspaceFiles / settings / credentials 路由与流。"""

import asyncio
import base64
import os
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.fs import install_local_fs
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.seams.credentials_local import LocalCredentialProvider, install_credentials
from miniharness.seams.sandbox_policy import SandboxPolicyService
from miniharness.settings import install_settings
from miniharness.settings_controller import install_settings_controller
from miniharness.web.api import WebApi
from miniharness.workspace import install_workspaces
from miniharness.workspace_controller import install_workspace_controller
from miniharness.workspace_files import install_workspace_files


class WebResidualTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = os.path.join(self._tmp.name, "work")
        os.makedirs(self.work)
        self.ctx = Context(name="web-residual-test")
        self.addCleanup(self.ctx.dispose)
        SandboxPolicyService(self.ctx, {"mode": "danger-full-access"})
        install_local_fs(self.ctx, {"cwd": self.work})
        install_settings(self.ctx, path=os.path.join(self._tmp.name, "settings.json"))
        install_credentials(self.ctx, LocalCredentialProvider(
            filename=os.path.join(self._tmp.name, "credentials.json")))
        install_workspaces(self.ctx, root=os.path.join(self._tmp.name, "home"))
        install_workspace_controller(self.ctx)
        install_workspace_files(self.ctx)
        install_settings_controller(self.ctx, roster=None)
        self.api = WebApi(self.ctx, FakeLlmAdapter())
        self.session_id = self._value(
            self.api.dispatch("session.create", "r0", {"cwd": self.work}))["sessionId"]
        self.file = os.path.join(self.work, "hello.txt")
        with open(self.file, "wb") as handle:
            handle.write(b"hello\nworld\n")

    def _value(self, response):
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]

    def _error(self, response):
        self.assertFalse(response["result"]["ok"])
        return response["result"]["error"]

    def test_workspace_routes_mutate_the_registry(self):
        created = self._value(self.api.dispatch(
            "workspace/create", "w1", {"path": self.work}))
        self.assertTrue(created["created"])
        workspace_id = created["workspace"]["workspaceId"]
        renamed = self._value(self.api.dispatch(
            "workspace/rename", "w2", {"workspaceId": workspace_id, "title": " Work "}))
        self.assertEqual(renamed["workspace"]["title"], "Work")
        self._value(self.api.dispatch("workspace/delete", "w3", {"workspaceId": workspace_id}))
        missing = self._error(self.api.dispatch(
            "workspace/delete", "w4", {"workspaceId": workspace_id}))
        self.assertEqual(missing["code"], "workspace/not-found")

    def test_workspace_files_routes_read_and_stat(self):
        stat = self._value(self.api.dispatch(
            "workspaceFiles/stat", "f1",
            {"workspaceFileScopeId": self.session_id, "path": self.file}))
        self.assertEqual(stat["absolutePath"], os.path.realpath(self.file))
        page = self._value(self.api.dispatch(
            "workspaceFiles/read", "f2",
            {"workspaceFileScopeId": self.session_id, "path": self.file, "offset": 2, "limit": 1}))
        self.assertEqual((page["text"], page["lines"], page["eof"]), ("world", 1, True))
        window = self._value(self.api.dispatch(
            "workspaceFiles/readBytes", "f3",
            {"workspaceFileScopeId": self.session_id, "path": self.file,
             "offset": 0, "length": 5}))
        self.assertEqual(base64.b64decode(window["data"]), b"hello")
        listing = self._value(self.api.dispatch(
            "workspaceFiles/list", "f4",
            {"workspaceFileScopeId": self.session_id, "path": self.work}))
        self.assertEqual([entry["name"] for entry in listing["entries"]], ["hello.txt"])

    def test_workspace_files_rejects_boundary_and_unknown_scope(self):
        missing = self._error(self.api.dispatch(
            "workspaceFiles/stat", "f5", {"path": self.file}))
        self.assertEqual(missing["code"], "gateway/arguments-invalid")
        unknown = self._error(self.api.dispatch(
            "workspaceFiles/stat", "f6",
            {"workspaceFileScopeId": "ghost", "path": self.file}))
        self.assertEqual(unknown["code"], "gateway/lookup-not-found")
        not_found = self._error(self.api.dispatch(
            "workspaceFiles/stat", "f7",
            {"workspaceFileScopeId": self.session_id,
             "path": os.path.join(self.work, "missing.txt")}))
        self.assertEqual(not_found["code"], "workspace-file/not-found")

    def test_settings_and_credentials_routes(self):
        described = self._value(self.api.dispatch("settings/describe", "s1", {}))
        self.assertIn("writable", described)
        self.assertFalse(self._value(self.api.dispatch(
            "settings/canOpenAgentPresetDirectory", "s2", {})))
        self.assertEqual(self._error(self.api.dispatch(
            "settings/openSettingsDocument", "s3", {}))["code"], "gateway/internal")
        self.assertEqual(self._error(self.api.dispatch(
            "settings/update", "s4", {"ns": "nope", "patch": {}}))["code"], "settings/rejected")
        self.assertEqual(self._value(self.api.dispatch(
            "credentials/describe", "c1", {"refs": ["MY_KEY"]})),
            {"MY_KEY": {"configured": False, "writable": True}})
        self._value(self.api.dispatch("credentials/set", "c2", {"ref": "MY_KEY", "value": "v"}))
        self.assertEqual(self._value(self.api.dispatch("credentials/describe", "c3", {"refs": ["MY_KEY"]})),
                         {"MY_KEY": {"configured": True, "source": "file", "writable": True}})

    def test_workspace_follow_and_file_changes_streams(self):
        created = self._value(self.api.dispatch("workspace/create", "w1", {"path": self.work}))
        workspace_id = created["workspace"]["workspaceId"]

        async def workspace_stream():
            stream = self.api.gateway.open_stream("workspace/follow", {"args": {}})
            baseline = await stream.__anext__()
            self.api.dispatch("workspace/rename", "w2",
                              {"workspaceId": workspace_id, "title": "Live"})
            increment = await stream.__anext__()
            await stream.aclose()
            return baseline, increment

        baseline, increment = asyncio.run(workspace_stream())
        self.assertEqual(baseline["type"], "baseline")
        self.assertEqual(increment["type"], "upsert")
        self.assertEqual(increment["workspace"]["title"], "Live")

        async def changes_stream():
            stream = self.api.gateway.open_stream(
                "workspaceFiles/changes", {"args": {"workspaceFileScopeId": self.session_id}})
            ready = await stream.__anext__()
            await stream.aclose()
            return ready

        self.assertEqual(asyncio.run(changes_stream()), {"kind": "ready"})

    def test_unmounted_namespaces_reject_honestly(self):
        bare = Context(name="bare-residual")
        self.addCleanup(bare.dispose)
        api = WebApi(bare, FakeLlmAdapter())
        for method, payload in (("workspace/create", {"path": self.work}),
                                ("settings/describe", {}),
                                ("credentials/describe", {"refs": []})):
            response = api.dispatch(method, "x", payload)
            self.assertEqual(self._error(response)["code"], "gateway/invocation-unavailable")


if __name__ == "__main__":
    unittest.main()
