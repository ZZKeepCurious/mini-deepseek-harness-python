"""permission-presets（M11）验收。

对齐上游 `packages/interaction/permission-presets/tests/permission-presets.spec.ts`
+ `projection.spec.ts`：

- 预设表：默认 names 顺序、resolve bundle、未知抛错、Auto 生命周期
- derive：组合缺省、durable preset 优先、custom 派生态
- set 写路径：事件序（permission/preset 在前 → 只写变更旋钮）、净零 no-op
- pinInitialPermission：新会话按 default、seeded 只补缺失
- /permission 命令、permissions 投影、catalog
"""
import os
import unittest

from miniharness.core.scope import Context
from miniharness.core.session_store import install_sessions
from miniharness.interaction.approval import ApprovalService
from miniharness.interaction.permission_presets import (
    AUTO_PRESET,
    CUSTOM_PRESET,
    PermissionPresetError,
    PermissionPresetService,
    install_permission_presets,
)
from miniharness.seams.sandbox_policy import SandboxPolicyService
from miniharness.session_projection import install_session_projections

PRESETS = {
    "read-only": {"sandbox": "read-only", "approval": "ask"},
    "workspace-write": {"sandbox": "workspace-write", "approval": "ask"},
    "danger-full-access": {"sandbox": "danger-full-access", "approval": "never"},
}


class PermissionPresetTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="perm")
        self.store = install_sessions(self.ctx)
        install_session_projections(self.ctx)
        SandboxPolicyService(self.ctx, {"mode": "workspace-write"})
        self.ctx.provide("approval", ApprovalService(self.ctx))
        self.svc = install_permission_presets(self.ctx, {"presets": PRESETS})

    def tearDown(self):
        self.ctx.dispose()

    def _session(self, session_id="s1"):
        return self.store.create(session_id, {"meta": {"cwd": os.getcwd()}})

    def test_default_names_order(self):
        self.assertEqual(self.svc.names,
                         ["read-only", "workspace-write", "danger-full-access"])

    def test_resolve_bundle_and_unknown(self):
        self.assertEqual(self.svc.resolve("danger-full-access"),
                         {"sandbox": "danger-full-access", "approval": "never"})
        with self.assertRaises(PermissionPresetError) as raised:
            self.svc.resolve("plan")
        self.assertIn("unknown preset \"plan\"", str(raised.exception))

    def test_current_fresh_session_uses_composition_default(self):
        session = self._session()
        # 组合缺省 workspace-write + ask → workspace-write
        self.assertEqual(self.svc.current(session), "workspace-write")
        # 钉扎写全三事件，序：permission/preset → sandbox/mode → approval/policy
        self.assertEqual([e["type"] for e in session.events],
                         ["permission/preset", "sandbox/mode", "approval/policy"])

    def test_set_writes_through_in_order(self):
        session = self._session()
        self.svc.set(session, "danger-full-access")
        events = [e["type"] for e in session.events]
        self.assertEqual(events, ["permission/preset", "sandbox/mode", "approval/policy",
                                  "permission/preset", "sandbox/mode", "approval/policy"])
        self.assertEqual(self.svc.current(session), "danger-full-access")

    def test_noop_on_already_current(self):
        session = self._session()
        before = len(session.events)
        self.svc.set(session, "workspace-write")
        self.assertEqual(len(session.events), before)

    def test_reassert_from_drifted_repairs_only_changed_knob(self):
        session = self._session()
        self.svc.set(session, "danger-full-access")
        # 漂移到 read-only 沙箱（approval 仍是 never）
        from miniharness.seams.sandbox_policy import set_sandbox_mode
        set_sandbox_mode(session, "read-only")
        self.assertEqual(self.svc.current(session), CUSTOM_PRESET)
        before = len(session.events)
        self.svc.set(session, "danger-full-access")
        # 只补 sandbox（approval never 已一致）+ 重写 preset
        tail = [e["type"] for e in session.events][before:]
        self.assertEqual(tail, ["permission/preset", "sandbox/mode"])

    def test_shared_bundle_identity_only_switch(self):
        session = self._session()
        self.svc.set(session, "danger-full-access")
        before = len(session.events)
        self.svc.set(session, "danger-full-access")
        self.assertEqual(len(session.events), before)

    def test_option_of(self):
        self.assertEqual(self.svc.option_of("danger-full-access")["value"],
                         "danger-full-access")
        self.assertEqual(self.svc.option_of(CUSTOM_PRESET)["name"], "Custom")
        self.assertEqual(self.svc.option_of("plain")["value"], "plain")

    def test_reserved_names_rejected(self):
        for reserved in (CUSTOM_PRESET, AUTO_PRESET):
            with self.assertRaises(PermissionPresetError):
                install_permission_presets(
                    Context(name=f"reserved-{reserved}"),
                    {"presets": {reserved: {"sandbox": "read-only", "approval": "ask"}}})

    def test_catalog(self):
        catalog = self.svc.catalog()
        self.assertEqual([o["value"] for o in catalog["options"]],
                         ["read-only", "workspace-write", "danger-full-access"])
        self.assertEqual(catalog["defaultPreset"], "workspace-write")

    def test_auto_lifecycle(self):
        admits = []
        disposer = self.svc.register_auto(lambda: admits.append(1))
        self.assertIn(AUTO_PRESET, self.svc.names)
        self.assertEqual(self.svc.resolve(AUTO_PRESET),
                         {"sandbox": "danger-full-access", "approval": "ask"})
        # Auto 选择先 admit 再写
        session = self._session()
        self.svc.set(session, AUTO_PRESET)
        self.assertEqual(admits, [1])
        disposer()
        self.assertNotIn(AUTO_PRESET, self.svc.names)
        with self.assertRaises(PermissionPresetError):
            self.svc.resolve(AUTO_PRESET)

    def test_auto_matches_delegated_never(self):
        # 委派子会话钉 `never`，仍选中的 Auto 解析回自身（index.ts:355）。
        self.svc.register_auto(lambda: None)
        from miniharness.interaction.approval import set_approval_policy
        session = self._session()
        self.svc.set(session, "danger-full-access")
        self.svc.set(session, AUTO_PRESET)
        self.assertEqual(self.svc.current(session), AUTO_PRESET)
        set_approval_policy(session, "never")
        self.assertEqual(self.svc.current(session), AUTO_PRESET)
        # 沙箱漂移后不再匹配 → custom。
        from miniharness.seams.sandbox_policy import set_sandbox_mode
        set_sandbox_mode(session, "read-only")
        self.assertEqual(self.svc.current(session), CUSTOM_PRESET)

    def test_duplicate_auto_rejected(self):
        self.svc.register_auto(lambda: None)
        with self.assertRaises(PermissionPresetError):
            self.svc.register_auto(lambda: None)

    def test_admission_throw_leaves_session_untouched(self):
        session = self._session()
        self.svc.register_auto(lambda: (_ for _ in ()).throw(
            RuntimeError("auto review is closing")))
        with self.assertRaises(RuntimeError):
            self.svc.set(session, AUTO_PRESET)
        # 会话事件不被污染（admission 在任何写前）
        self.assertEqual([e["type"] for e in session.events],
                         ["permission/preset", "sandbox/mode", "approval/policy"])


class PermissionProjectionTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="perm-proj")
        self.store = install_sessions(self.ctx)
        self.registry = install_session_projections(self.ctx)
        SandboxPolicyService(self.ctx, {"mode": "workspace-write"})
        self.ctx.provide("approval", ApprovalService(self.ctx))
        self.svc = install_permission_presets(self.ctx, {"presets": PRESETS})
        self.session = self.store.create("p1", {"meta": {"cwd": os.getcwd()}})

    def tearDown(self):
        self.ctx.dispose()

    def test_projection_wire_view(self):
        snapshot = self.registry.snapshot(self.session)
        self.assertEqual(snapshot["values"]["permissions"],
                         {"currentValue": "workspace-write"})

    def test_projection_folds_events(self):
        self.svc.set(self.session, "danger-full-access")
        snapshot = self.registry.snapshot(self.session)
        self.assertEqual(snapshot["values"]["permissions"],
                         {"currentValue": "danger-full-access"})

    def test_command_surface(self):
        # 命令在构造时注册（commands 在场时）；用独立 ctx 验证完整命令面
        self._verify_command_on_fresh_ctx()

    def _verify_command_on_fresh_ctx(self):
        from miniharness.commands import install_commands
        ctx = Context(name="perm-cmd")
        try:
            install_sessions(ctx)
            install_session_projections(ctx)
            install_commands(ctx)
            SandboxPolicyService(ctx, {"mode": "workspace-write"})
            ctx.provide("approval", ApprovalService(ctx))
            svc = install_permission_presets(ctx, {"presets": PRESETS})
            session = ctx.get("sessions").create("c1", {"meta": {"cwd": os.getcwd()}})
            commands = ctx.get("commands")

            def _agent():
                agent = type("Agent", (), {})()
                agent.session = session
                agent.injected = []
                agent.inject = lambda message: agent.injected.append(message)
                return agent

            result = commands.dispatch(_agent(), "/permission")
            self.assertEqual(result["kind"], "success")
            self.assertTrue(result["text"].startswith("current preset "))
            result = commands.dispatch(_agent(), "/permission bogus")
            self.assertEqual(result["kind"], "error")
            self.assertIn('unknown preset "bogus"', result["text"])
            result = commands.dispatch(_agent(), "/permission danger-full-access")
            self.assertEqual(result["text"], "preset danger-full-access")
            self.assertEqual(svc.current(session), "danger-full-access")
        finally:
            ctx.dispose()

    def _agent(self):
        class _Agent:
            session = self.session
        return _Agent()


class PermissionPresetWebWireTest(unittest.TestCase):
    """permissionPresets/catalog 经 WebApi dispatch 的 wire 面。"""

    def setUp(self):
        from miniharness.llm import FakeLlmAdapter
        from miniharness.web.api import WebApi
        self.ctx = Context(name="perm-web")
        self.store = install_sessions(self.ctx)
        install_session_projections(self.ctx)
        SandboxPolicyService(self.ctx, {"mode": "workspace-write"})
        self.ctx.provide("approval", ApprovalService(self.ctx))
        install_permission_presets(self.ctx, {"presets": PRESETS})
        self.api = WebApi(self.ctx, FakeLlmAdapter())

    def tearDown(self):
        self.ctx.dispose()

    def test_catalog_route(self):
        response = self.api.dispatch("permissionPresets/catalog", "r1", {})
        self.assertTrue(response["result"]["ok"])
        catalog = response["result"]["value"]
        self.assertEqual([o["value"] for o in catalog["options"]],
                         ["read-only", "workspace-write", "danger-full-access"])
        self.assertEqual(catalog["defaultPreset"], "workspace-write")

    def test_catalog_not_mounted(self):
        from miniharness.core.scope import Context as Ctx
        from miniharness.llm import FakeLlmAdapter
        from miniharness.web.api import WebApi
        ctx2 = Ctx(name="bare-perm")
        try:
            api = WebApi(ctx2, FakeLlmAdapter())
            response = api.dispatch("permissionPresets/catalog", "r2", {})
            self.assertFalse(response["result"]["ok"])
            self.assertEqual(response["result"]["error"]["code"],
                             "gateway/invocation-unavailable")
        finally:
            ctx2.dispose()


if __name__ == "__main__":
    unittest.main()