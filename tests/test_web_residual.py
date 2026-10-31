"""web 残余 api 控制器：workspace / workspaceFiles / settings / credentials 路由与流。"""

import asyncio
import os
import tempfile
import unittest

from miniharness.context import install_file_reference_local, install_session_reference
from miniharness.core.scope import Context
from miniharness.fs import install_local_fs
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.seams.credentials_local import LocalCredentialProvider, install_credentials
from miniharness.seams.sandbox_policy import SandboxPolicyService
from miniharness.session_query import SessionQuery
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
        SessionQuery(self.ctx)
        install_session_reference(self.ctx, {"maxReferenceBytes": 4096})
        install_file_reference_local(self.ctx)
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

    def test_workspace_pin_and_archive_routes(self):
        self._value(self.api.dispatch("workspace/create", "w1", {"path": self.work}))
        pinned = self._value(self.api.dispatch(
            "workspace/pinSession", "w2", {"sessionId": self.session_id}))
        self.assertEqual(pinned["pinnedSessionIds"], [self.session_id])
        unpinned = self._value(self.api.dispatch(
            "workspace/unpinSession", "w3", {"sessionId": self.session_id}))
        self.assertEqual(unpinned["pinnedSessionIds"], [])
        # stopActivity 是可选布尔字段，须通过路由层边界校验
        archived = self._value(self.api.dispatch(
            "workspace/archiveSession", "w4",
            {"sessionId": self.session_id, "stopActivity": True}))
        self.assertEqual(archived["archivedSessionIds"], [self.session_id])
        # dsh-v0.2.0-rc.2：initializeDefault 无入参；注册表已非空 → value 为 None
        self.assertIsNone(self._value(self.api.dispatch(
            "workspace/initializeDefault", "w5", {})))
        boundary = self._error(self.api.dispatch(
            "workspace/archiveSession", "w6",
            {"sessionId": self.session_id, "stopActivity": "yes"}))
        self.assertEqual(boundary["code"], "gateway/input-invalid")

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
             "options": {"range": {"offset": 0, "length": 5}}}))
        self.assertEqual(window["data"], b"hello")
        whole = self._value(self.api.dispatch(
            "workspaceFiles/readBytes", "f3b",
            {"workspaceFileScopeId": self.session_id, "path": self.file}))
        self.assertEqual(whole["data"], b"hello\nworld\n")
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
        # rc.1 删 `settings/canOpenAgentPresetDirectory` 路由（无处理器 → dispatch 返回 None）
        self.assertIsNone(self.api.dispatch("settings/canOpenAgentPresetDirectory", "s2", {}))
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
                "workspaceFiles/changes",
                {"args": {"workspaceFileScopeId": self.session_id, "path": self.file}})
            ready = await stream.__anext__()
            await stream.aclose()
            return ready

        self.assertEqual(asyncio.run(changes_stream()), {"kind": "ready"})

    def test_file_references_list_route(self):
        value = self._value(self.api.dispatch(
            "fileReferences/list", "fr1", {"agentId": self.session_id, "query": "hello"}))
        self.assertEqual(value, [{"path": "hello.txt", "kind": "file"}])
        missing = self._error(self.api.dispatch("fileReferences/list", "fr2", {}))
        self.assertEqual(missing["code"], "gateway/arguments-invalid")

    def test_session_reference_candidates_route(self):
        value = self._value(self.api.dispatch(
            "sessionReferenceResolver/candidates", "sr1", {"agentId": self.session_id}))
        self.assertEqual(value, [])
        missing = self._error(self.api.dispatch(
            "sessionReferenceResolver/candidates", "sr2", {}))
        self.assertEqual(missing["code"], "gateway/arguments-invalid")

    def test_unmounted_namespaces_reject_honestly(self):
        bare = Context(name="bare-residual")
        self.addCleanup(bare.dispose)
        api = WebApi(bare, FakeLlmAdapter())
        for method, payload in (("workspace/create", {"path": self.work}),
                                ("settings/describe", {}),
                                ("credentials/describe", {"refs": []})):
            response = api.dispatch(method, "x", payload)
            self.assertEqual(self._error(response)["code"], "gateway/invocation-unavailable")


class DirectoryPickerTest(unittest.TestCase):
    """`directoryPicker/*` browse 载体（workspace-controller）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "root")
        os.makedirs(os.path.join(self.root, "alpha"))
        os.makedirs(os.path.join(self.root, ".hidden"))
        with open(os.path.join(self.root, "file.txt"), "w", encoding="utf-8") as handle:
            handle.write("x")
        ctx = Context(name="directory-picker-test")
        self.addCleanup(ctx.dispose)
        self.api = WebApi(ctx, FakeLlmAdapter())

    def _value(self, response):
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]

    def _error(self, response):
        self.assertFalse(response["result"]["ok"])
        return response["result"]["error"]

    def test_list_returns_child_directories_and_crumbs(self):
        value = self._value(self.api.dispatch(
            "directoryPicker/list", "d1", {"path": self.root}))
        self.assertEqual(value["path"], self.root)
        self.assertEqual([row["name"] for row in value["entries"]],
                         [".hidden", "alpha"])
        self.assertTrue(value["entries"][0]["hidden"])
        self.assertEqual(value["crumbs"][-1]["path"], self.root)
        self.assertFalse(value["truncated"])

    def test_list_rejects_relative_path(self):
        self.assertEqual(
            self._error(self.api.dispatch(
                "directoryPicker/list", "d2", {"path": "relative"}))["code"],
            "directory-picker/unreadable")

    def test_create_directory_and_conflict(self):
        created = self._value(self.api.dispatch(
            "directoryPicker/createDirectory", "d3",
            {"path": self.root, "name": "beta"}))
        self.assertEqual(created, os.path.join(self.root, "beta"))
        self.assertTrue(os.path.isdir(created))
        self.assertEqual(
            self._error(self.api.dispatch(
                "directoryPicker/createDirectory", "d4",
                {"path": self.root, "name": "beta"}))["code"],
            "directory-picker/exists")
        self.assertEqual(
            self._error(self.api.dispatch(
                "directoryPicker/createDirectory", "d5",
                {"path": self.root, "name": "../x"}))["code"],
            "gateway/bad-request")

    def test_native_pick_is_unavailable(self):
        self.assertEqual(
            self._error(self.api.dispatch("directoryPicker/pick", "d6", {}))["code"],
            "directory-picker/unavailable")


class ColdForkTest(unittest.TestCase):
    """P0-7c-gap：非 live 源会话按持久化前缀分叉。"""

    def setUp(self):
        from pathlib import Path

        from miniharness.core.session.persistence import JsonlPersistence

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = os.path.join(self._tmp.name, "work")
        os.makedirs(self.work)
        self.source_ctx = Context(name="cold-fork-source")
        self.addCleanup(self.source_ctx.dispose)
        source_api = WebApi(self.source_ctx, FakeLlmAdapter())
        self.source_id = self._value(source_api.dispatch(
            "session.create", "r0", {"cwd": self.work}))["sessionId"]
        self._value(source_api.dispatch(
            "session/prompt", "r1",
            {"requestId": "req-1", "sessionId": self.source_id,
             "content": [{"type": "text", "text": "hi"}], "mode": "queue"}))
        source = source_api.store.get(self.source_id)
        self.persistence = JsonlPersistence(Path(self._tmp.name) / "sessions",
                                            compression="none")
        self.persistence.declare(self.source_id, meta={"cwd": self.work},
                                 created_at=source.created_at, cwd=self.work)
        for event in source.events:
            self.persistence.append(self.source_id, dict(event), cwd=self.work)
        self.persistence.flush()

        self.target_ctx = Context(name="cold-fork-target")
        self.addCleanup(self.target_ctx.dispose)
        self.target_ctx.provide("sessionPersistence", self.persistence)
        self.api = WebApi(self.target_ctx, FakeLlmAdapter())

    def _value(self, response):
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]

    def test_fork_cold_source_prefix(self):
        value = self._value(self.api.dispatch(
            "session/fork", "f1", {"sessionId": self.source_id, "allowMigration": True}))
        child_id = value["sessionId"]
        self.assertNotEqual(child_id, self.source_id)
        child = self.api.store.get(child_id)
        self.assertIsNotNone(child)
        self.assertEqual(child.meta["parentSession"], self.source_id)
        self.assertTrue(child.meta["isSeeded"])
        self.assertTrue(any(event["type"] == "session/end-seed"
                            for event in child.events))

    def test_unknown_cold_source_is_not_found(self):
        error = self.api.dispatch(
            "session/fork", "f2", {"sessionId": "missing", "allowMigration": True})
        self.assertFalse(error["result"]["ok"])
        self.assertEqual(error["result"]["error"]["code"], "session/not-found")


class HostScheduleTest(unittest.TestCase):
    """生产 host 装配：storage + schedule 服务 + 每 agent 的 schedule_* 工具。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = os.path.join(self._tmp.name, "work")
        os.makedirs(self.work)
        self.ctx = Context(name="host-schedule-test")
        self.addCleanup(self.ctx.dispose)
        self.api = WebApi(self.ctx, FakeLlmAdapter(),
                          schedule_root=os.path.join(self._tmp.name, "storages"))
        self.session_id = self._value(
            self.api.dispatch("session.create", "r0", {"cwd": self.work}))["sessionId"]

    def _value(self, response):
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]

    def test_schedule_service_and_tools_are_mounted(self):
        self.assertIsNotNone(self.ctx.get("storage"))
        self.assertIsNotNone(self.ctx.get("schedule"))
        loop = self.api._agents[self.session_id]
        for name in ("schedule_create", "schedule_list", "schedule_delete"):
            self.assertIsNotNone(self.api.tools.resolve(name, loop.ctx), name)
        self.assertEqual(self._value(self.api.dispatch(
            "schedule/list", "s1", {"sessionId": self.session_id})), [])


class _CatalogAdapter(FakeLlmAdapter):
    """带已知目录的假适配器（`llm/discoverModels` 成功路径载体）。"""

    def available_models(self):
        return [{"provider": "fake", "model": "fake-1", "name": "Fake One",
                 "input_modalities": ["text"]}]


class LlmRemoteTest(unittest.TestCase):
    """`llm/listProviders|listConfigurableProviders|discoverModels`。"""

    def setUp(self):
        self.ctx = Context(name="llm-remote-test")
        self.addCleanup(self.ctx.dispose)
        self.api = WebApi(self.ctx, _CatalogAdapter())

    def _value(self, response):
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]

    def _error(self, response):
        self.assertFalse(response["result"]["ok"])
        return response["result"]["error"]

    def test_lists_routes(self):
        self.assertEqual(
            self._value(self.api.dispatch("llm/listProviders", "l1", {})),
            [{"id": "fake", "name": "fake"}])
        providers = self._value(
            self.api.dispatch("llm/listConfigurableProviders", "l2", {}))
        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0]["provider"], "fake")

    def test_discovers_models_for_known_route(self):
        self.assertEqual(
            self._value(self.api.dispatch(
                "llm/discoverModels", "l3",
                {"settingsNs": "llm-fake",
                 "request": {"provider": "fake"}})),
            [{"id": "fake-1", "name": "Fake One", "inputModalities": ["text"]}])

    def test_unknown_route_is_rejected(self):
        error = self._error(self.api.dispatch(
            "llm/discoverModels", "l4",
            {"settingsNs": "llm-fake", "request": {"baseURL": "https://x"}}))
        self.assertEqual(error["code"], "llm/model-discovery-rejected")


class CommandsRemoteTest(unittest.TestCase):
    """`commands/list|execute` Remote 面（packages/interaction/commands）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = os.path.join(self._tmp.name, "work")
        os.makedirs(self.work)
        self.ctx = Context(name="commands-remote-test")
        self.addCleanup(self.ctx.dispose)
        self.api = WebApi(self.ctx, FakeLlmAdapter())
        self.session_id = self._value(
            self.api.dispatch("session.create", "r0", {"cwd": self.work}))["sessionId"]
        registry = self.ctx.get("commands")
        registry.register("hello", "Say hello", lambda invocation: {
            "kind": "success", "text": f"hello {invocation.raw_input.strip()}"},
            input_hint="name")

    def _value(self, response):
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]

    def _error(self, response):
        self.assertFalse(response["result"]["ok"])
        return response["result"]["error"]

    def test_list_returns_descriptors(self):
        value = self._value(self.api.dispatch(
            "commands/list", "c1", {"agentId": self.session_id}))
        self.assertEqual(value, [{
            "name": "hello",
            "description": "Say hello",
            "input": {"hint": "name"},
        }])

    def test_execute_returns_command_id_and_result(self):
        value = self._value(self.api.dispatch(
            "commands/execute", "c2",
            {"agentId": self.session_id, "line": "/hello world",
             "submittedAttachments": []}))
        self.assertEqual(value["result"], {"kind": "success", "text": "hello world"})
        self.assertTrue(value["commandId"].startswith("cmd-"))
        types_ = [event["type"] for event in
                  self.ctx.get("sessions").get(self.session_id).events[-2:]]
        self.assertEqual(types_, ["command/run", "command/done"])

    def test_execute_unknown_command_is_null(self):
        self.assertIsNone(self._value(self.api.dispatch(
            "commands/execute", "c3",
            {"agentId": self.session_id, "line": "/missing",
             "submittedAttachments": []})))

    def test_execute_rejects_attachments(self):
        value = self._value(self.api.dispatch(
            "commands/execute", "c4",
            {"agentId": self.session_id, "line": "/hello",
             "submittedAttachments": [{"type": "image"}]}))
        self.assertEqual(value["result"],
                         {"kind": "error", "text": "/hello does not accept attachments"})
        self.assertEqual(
            self._error(self.api.dispatch(
                "commands/execute", "c5",
                {"agentId": self.session_id, "line": "/hello"}))["code"],
            "gateway/arguments-invalid")


class SkillsRemoteTest(unittest.TestCase):
    """`skills/list` Remote 面（session-controller 的 skill catalog 适配）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = os.path.join(self._tmp.name, "work")
        os.makedirs(self.work)
        self.ctx = Context(name="skills-remote-test")
        self.addCleanup(self.ctx.dispose)
        self.api = WebApi(self.ctx, FakeLlmAdapter())
        self.session_id = self._value(
            self.api.dispatch("session.create", "r0", {"cwd": self.work}))["sessionId"]
        registry = self.ctx.get("skills")
        registry.register({
            "name": "alpha-skill",
            "description": "Alpha skill",
            "whenToUse": "when testing",
            "source": "test",
            "content": "Use alpha.",
        })
        registry.register({
            "name": "model-only",
            "description": "Hidden from users",
            "source": "test",
            "content": "Model only.",
            "invocation": {"modelInvocable": True, "userInvocable": False},
        })

    def _value(self, response):
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]

    def _error(self, response):
        self.assertFalse(response["result"]["ok"])
        return response["result"]["error"]

    def test_list_returns_user_invocable_skills(self):
        value = self._value(self.api.dispatch(
            "skills/list", "s1", {"request": {"sessionId": self.session_id}}))
        self.assertEqual([skill["name"] for skill in value["skills"]], ["alpha-skill"])
        self.assertEqual(value["skills"][0]["whenToUse"], "when testing")
        self.assertTrue(value["skills"][0]["modelInvocable"])

    def test_unknown_session_and_argument_boundary(self):
        self.assertEqual(
            self._error(self.api.dispatch(
                "skills/list", "s2", {"request": {"sessionId": "missing"}}))["code"],
            "session/not-found")
        self.assertEqual(
            self._error(self.api.dispatch("skills/list", "s3", {"request": {}}))["code"],
            "gateway/arguments-invalid")


class GoalsRemoteTest(unittest.TestCase):
    """`goals/*` Remote 面（packages/goal 的 host-plane 服务）。"""

    def setUp(self):
        from miniharness.goal import install_goals

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = os.path.join(self._tmp.name, "work")
        os.makedirs(self.work)
        self.ctx = Context(name="goals-remote-test")
        self.addCleanup(self.ctx.dispose)
        install_goals(self.ctx)
        self.api = WebApi(self.ctx, FakeLlmAdapter())
        self.session_id = self._value(
            self.api.dispatch("session.create", "r0", {"cwd": self.work}))["sessionId"]

    def _value(self, response):
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]

    def _error(self, response):
        self.assertFalse(response["result"]["ok"])
        return response["result"]["error"]

    def test_goal_lifecycle_routes(self):
        created = self._value(self.api.dispatch(
            "goals/create", "g1",
            {"agentId": self.session_id,
             "request": {"objective": "ship it", "maxGoalRounds": 2}}))
        self.assertEqual(created["ref"]["revision"], 1)
        ref = created["ref"]

        view = self._value(self.api.dispatch(
            "goals/get", "g2", {"agentId": self.session_id}))
        self.assertEqual(view["objective"], "ship it")
        self.assertEqual(view["phase"], "active")

        edited = self._value(self.api.dispatch(
            "goals/edit", "g3",
            {"agentId": self.session_id, "ref": ref,
             "request": {"objective": "ship it better"}}))
        self.assertEqual(edited["objective"], "ship it better")
        ref = {"id": edited["id"], "revision": edited["revision"]}

        paused = self._value(self.api.dispatch(
            "goals/pause", "g4", {"agentId": self.session_id, "ref": ref}))
        self.assertEqual(paused["phase"], "paused")
        ref = {"id": paused["id"], "revision": paused["revision"]}

        resumed = self._value(self.api.dispatch(
            "goals/resume", "g5", {"agentId": self.session_id, "ref": ref}))
        self.assertEqual(resumed["phase"], "active")
        ref = {"id": resumed["id"], "revision": resumed["revision"]}

        completed = self._value(self.api.dispatch(
            "goals/complete", "g6", {"agentId": self.session_id, "ref": ref}))
        self.assertEqual(completed["phase"], "complete")
        ref = {"id": completed["id"], "revision": completed["revision"]}

        tombstone = self._value(self.api.dispatch(
            "goals/clear", "g7", {"agentId": self.session_id, "ref": ref}))
        self.assertEqual(tombstone["revision"], ref["revision"] + 1)
        self.assertIsNone(self._value(self.api.dispatch(
            "goals/get", "g8", {"agentId": self.session_id})))

    def test_goal_stale_revision_is_rejected(self):
        created = self._value(self.api.dispatch(
            "goals/create", "g1",
            {"agentId": self.session_id, "request": {"objective": "ship"}}))
        stale = {"id": created["ref"]["id"], "revision": 99}
        error = self._error(self.api.dispatch(
            "goals/pause", "g2", {"agentId": self.session_id, "ref": stale}))
        self.assertEqual(error["code"], "gateway/internal")
        self.assertIn("stale goal ref", error["message"])


if __name__ == "__main__":
    unittest.main()
