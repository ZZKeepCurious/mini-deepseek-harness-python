"""sandbox-policy 服务测试：部署缺省 + 会话日志覆盖 + workspace 根决议。

上游对照：packages/sandbox/sandbox-policy/src/{index,session-mode}.ts 契约
（resolve 优先级 / sandbox/mode fold / 三档上下文文案 / canonical 根）。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.system_prompt import (
    install_system_prompt,
    render_context_snapshot,
)
from miniharness.seams.sandbox_local import (
    SandboxUnavailableError,
    canonical_path,
)
from miniharness.seams.sandbox_policy import (
    SANDBOX_MODES,
    SandboxPolicyService,
    effective_sandbox_mode,
    render_policy_context,
    set_sandbox_mode,
)


class EffectiveSandboxModeTest(unittest.TestCase):
    def test_empty_log_has_no_override(self):
        self.assertIsNone(effective_sandbox_mode(Session("s1").events))

    def test_last_mode_wins_across_other_events(self):
        s = Session("s1")
        set_sandbox_mode(s, "workspace-write")
        s.append("turn/start", {"turn": 1})
        set_sandbox_mode(s, "read-only")
        self.assertEqual(effective_sandbox_mode(s.events), "read-only")

    def test_delegation_source_round_trips(self):
        s = Session("s1")
        s.append("sandbox/mode", {"mode": "workspace-write", "source": "delegation"})
        self.assertEqual(s.events[-1]["data"],
                         {"mode": "workspace-write", "source": "delegation"})
        self.assertEqual(effective_sandbox_mode(s.events), "workspace-write")


class SetSandboxModeTest(unittest.TestCase):
    def test_appends_exactly_one_event(self):
        s = Session("s1")
        set_sandbox_mode(s, "danger-full-access")
        self.assertEqual(len(s.events), 1)
        ev = s.events[0]
        self.assertEqual(ev["type"], "sandbox/mode")
        self.assertEqual(ev["data"], {"mode": "danger-full-access"})
        self.assertEqual(ev["seq"], 0)  # 信封 seq == append 前的 log.length

    def test_unknown_mode_fails_loud(self):
        with self.assertRaises(ValueError):
            set_sandbox_mode(Session("s1"), "sudo")


class SandboxModesTest(unittest.TestCase):
    def test_canonical_mode_set(self):
        self.assertEqual(SANDBOX_MODES,
                         ("read-only", "workspace-write", "danger-full-access"))


class ServiceConfigTest(unittest.TestCase):
    def test_registers_service_tag(self):
        ctx = Context(name="t")
        svc = SandboxPolicyService(ctx)
        self.assertIs(ctx.get("sandboxPolicy"), svc)

    def test_default_mode_is_fail_safe_read_only(self):
        self.assertEqual(
            SandboxPolicyService(Context(name="t")).default_mode, "read-only")

    def test_unknown_default_mode_rejected(self):
        with self.assertRaises(ValueError):
            SandboxPolicyService(Context(name="t"), {"mode": "yolo"})

    def test_workspace_root_canonicalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep = Path(tmp) / "w"
            deep.mkdir()
            svc = SandboxPolicyService(Context(name="t"),
                                       {"workspaceRoot": str(deep) + os.sep + "."})
            self.assertEqual(Path(svc.workspace_root), Path(os.path.realpath(str(deep))))

    def test_missing_root_kept_verbatim_conservatively(self):
        missing = os.path.join(tempfile.gettempdir(), "definitely-missing-root-xyz")
        svc = SandboxPolicyService(Context(name="t"), {"workspaceRoot": missing})
        # 无回退发明：canonical（前缀 realpath，缺失尾段原样）+ 词法规范化
        self.assertEqual(Path(svc.workspace_root),
                         Path(os.path.abspath(canonical_path(missing))))


class ResolveTest(unittest.TestCase):
    def _svc(self, config=None, session=None):
        ctx = Context(name="t")
        svc = SandboxPolicyService(ctx, config)
        return svc, session

    def test_agentless_uses_configured_root_and_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, _ = self._svc({"workspaceRoot": tmp})
            policy = svc.resolve()
            self.assertEqual(policy["mode"], "read-only")
            self.assertEqual(Path(policy["workspaceRoot"]), Path(os.path.realpath(tmp)))
            self.assertNotIn("sessionId", policy)

    def test_session_cwd_is_boundary_and_carries_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session("s9", meta={"cwd": tmp})
            svc = SandboxPolicyService(Context(name="t"), {"workspaceRoot": "/elsewhere"})
            policy = svc.resolve({"session": session})
            self.assertEqual(policy["sessionId"], "s9")
            self.assertEqual(Path(policy["workspaceRoot"]), Path(os.path.realpath(tmp)))

    def test_request_mode_beats_everything(self):
        session = Session("s1")
        set_sandbox_mode(session, "read-only")
        svc = SandboxPolicyService(Context(name="t"), {"mode": "workspace-write"})
        policy = svc.resolve({"session": session, "mode": "danger-full-access"})
        self.assertEqual(policy["mode"], "danger-full-access")

    def test_session_override_beats_default(self):
        session = Session("s1")
        set_sandbox_mode(session, "danger-full-access")
        svc = SandboxPolicyService(Context(name="t"))
        self.assertEqual(svc.resolve({"session": session})["mode"],
                         "danger-full-access")

    def test_override_of_is_none_without_events(self):
        svc = SandboxPolicyService(Context(name="t"))
        self.assertIsNone(svc.override_of(Session("s1")))


class RenderPolicyContextTest(unittest.TestCase):
    def test_read_only_verbatim(self):
        self.assertEqual(
            render_policy_context({"mode": "read-only", "workspaceRoot": "/x"}),
            "Current DSH file policy: read-only. Any available operation enforced by "
            "the DSH file sandbox cannot modify files in the standing mode. Do not "
            "refuse a required modification from this policy alone: try an available "
            "tool normally and follow any denial and escalation guidance it returns.")

    def test_workspace_write_quotes_root_json_style(self):
        text = render_policy_context({"mode": "workspace-write", "workspaceRoot": "/w"})
        self.assertIn('may modify files under the session workspace: "/w"', text)

    def test_danger_full_access_verbatim(self):
        text = render_policy_context({"mode": "danger-full-access", "workspaceRoot": "/w"})
        self.assertTrue(text.startswith(
            "Current DSH file policy: danger-full-access."))

    def test_unknown_mode_unreachable_guard(self):
        with self.assertRaises(SandboxUnavailableError):
            render_policy_context({"mode": "???", "workspaceRoot": "/w"})


class PromptSectionTest(unittest.TestCase):
    def _snapshot(self, config, session):
        """装配上下文快照（上游 PromptContext 经 renderContextSnapshot 呈现；
        loop 侧投影注入 mini 未实现——既有简化，见 verified-diffs §3.10）。"""
        ctx = Context(name="t")
        install_system_prompt(ctx)
        SandboxPolicyService(ctx, config)
        prompt = ctx.get("systemPrompt")
        return render_context_snapshot(prompt.assemble({"agent": None, "session": session}))

    def test_fresh_session_gets_read_only_section(self):
        text = self._snapshot({}, Session("s1"))
        self.assertIn("Current DSH file policy: read-only.", text)

    def test_workspace_write_mentions_resolved_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session("s1", meta={"cwd": tmp})
            text = self._snapshot({"mode": "workspace-write"}, session)
            root = os.path.realpath(tmp)
            self.assertIn(f'workspace: {json.dumps(root)}', text)

    def test_no_session_renders_empty_and_is_skipped(self):
        self.assertEqual(self._snapshot({}, None), "")


if __name__ == "__main__":
    unittest.main()
