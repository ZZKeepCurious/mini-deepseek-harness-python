"""Windows 沙箱 ACL 诊断技能（对齐 packages/sandbox/sandbox-windows-acl/src/acl-skill.ts）。"""
import os
import unittest

from miniharness.core.scope import Context
from miniharness.seams.sandbox_windows_acl.acl_skill import (
    ACL_DIAGNOSIS_SKILL,
    AclDiagnosisSkillProvider,
    install_acl_diagnosis_skill,
)
from miniharness.skills import install_skills


class TestAclDiagnosisSkillProvider(unittest.TestCase):
    def test_lists_bundled_candidate(self):
        provider = AclDiagnosisSkillProvider()
        candidates = provider.list()
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["name"], ACL_DIAGNOSIS_SKILL)
        self.assertEqual(candidate["provider"], "dsh-windows-acl")
        self.assertEqual(candidate["source"], "bundled")
        self.assertTrue(candidate["description"])
        self.assertTrue(candidate["invocation"]["modelInvocable"])
        self.assertTrue(candidate["invocation"]["userInvocable"])
        self.assertEqual(candidate["resourceBase"]["kind"], "directory")
        self.assertTrue(candidate["resourceBase"]["path"].endswith(
            ACL_DIAGNOSIS_SKILL))
        # 脚本资产随包，可就地执行。
        script = os.path.join(candidate["resourceBase"]["path"], "scripts",
                              "diagnose-windows-sandbox-acl.ps1")
        self.assertTrue(os.path.isfile(script))

    def test_get_returns_content_without_frontmatter(self):
        provider = AclDiagnosisSkillProvider()
        skill = provider.get(provider.list()[0])
        self.assertIsNotNone(skill)
        self.assertIn("# Diagnose Windows sandbox ACL failures", skill["content"])
        self.assertNotIn("---", skill["content"].splitlines()[0])
        self.assertNotIn("rank", skill)
        self.assertNotIn("locator", skill)

    def test_get_unknown_provider_returns_none(self):
        provider = AclDiagnosisSkillProvider()
        self.assertIsNone(provider.get({"provider": "other", "locator": "x"}))

    def test_missing_assets_fail_loud(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "assets must contain"):
                AclDiagnosisSkillProvider(asset_root=tmp)

    def test_install_registers_provider(self):
        ctx = Context(name="acl-skill")
        try:
            install_skills(ctx)
            install_acl_diagnosis_skill(ctx)
            catalog = ctx.get("skills").snapshot()
            names = [entry["name"] for entry in catalog.get("skills", [])]
            self.assertIn(ACL_DIAGNOSIS_SKILL, names)
        finally:
            ctx.dispose()

    def test_install_without_skills_service_is_noop(self):
        ctx = Context(name="acl-skill-bare")
        try:
            self.assertIsNone(install_acl_diagnosis_skill(ctx))
        finally:
            ctx.dispose()

    @unittest.skipUnless(os.name == "nt", "win32 内建 runner 才注册该技能")
    def test_builtin_runner_registers_skill_on_win32(self):
        from miniharness.seams.sandbox_local import LocalSandboxProvider
        ctx = Context(name="acl-local")
        try:
            install_skills(ctx)
            LocalSandboxProvider(ctx=ctx)
            catalog = ctx.get("skills").snapshot()
            names = [entry["name"] for entry in catalog.get("skills", [])]
            self.assertIn(ACL_DIAGNOSIS_SKILL, names)
        finally:
            ctx.dispose()

    def test_operator_runner_does_not_register(self):
        from miniharness.seams.sandbox_local import LocalSandboxProvider
        ctx = Context(name="acl-operator")
        try:
            install_skills(ctx)
            LocalSandboxProvider(ctx=ctx, runner_command=["runner"],
                                 runner_failure_signatures=["runner: "])
            catalog = ctx.get("skills").snapshot()
            names = [entry["name"] for entry in catalog.get("skills", [])]
            self.assertNotIn(ACL_DIAGNOSIS_SKILL, names)
        finally:
            ctx.dispose()


if __name__ == "__main__":
    unittest.main()
