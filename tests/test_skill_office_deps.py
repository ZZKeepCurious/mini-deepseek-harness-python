"""skill-office provider + load_workspace_dependencies 工具（对齐上游 P2 item 25）。"""
import json
import os
import sys
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.core.tools import ToolRegistry
from miniharness.skills import install_skills
from miniharness.skills.office import (
    OFFICE_SKILL_NAMES,
    OfficeSkillProvider,
    install_office_skills,
)
from miniharness.skills.workspace_dependencies import (
    parse_primary_runtime,
    register_workspace_dependencies_tool,
    resolve_primary_runtime,
    workspace_dependency_paths,
)


def _runtime_manifest(platform="win32", arch="x64", python="3.12.0",
                      packages=None, node=None, pnpm=None):
    manifest = {
        "desktopVersion": "0.1.7",
        "platform": platform, "arch": arch,
        "python": python,
        "pythonPackages": packages or {},
    }
    if node is not None:
        manifest["node"] = node
    if pnpm is not None:
        manifest["pnpm"] = pnpm
    return manifest


class TestOfficeSkillProvider(unittest.TestCase):
    def test_lists_three_office_skills(self):
        provider = OfficeSkillProvider()
        names = [c["name"] for c in provider.list()]
        self.assertEqual(names, list(OFFICE_SKILL_NAMES))
        for candidate in provider.list():
            self.assertEqual(candidate["provider"], "dsh-office")
            self.assertEqual(candidate["source"], "bundled")
            self.assertTrue(candidate["description"])
            self.assertTrue(candidate["resourceBase"]["path"].endswith(
                candidate["name"]))

    def test_get_returns_content_with_disabled_runtime(self):
        provider = OfficeSkillProvider()
        candidate = provider.list()[0]
        skill = provider.get(candidate)
        self.assertIsNotNone(skill)
        self.assertIn("LibreOffice Kit is disabled in this deployment.",
                      skill["content"])
        self.assertIn("## ", skill["content"])

    def test_get_unknown_provider_returns_none(self):
        provider = OfficeSkillProvider()
        self.assertIsNone(provider.get({"provider": "other", "locator": "x"}))

    def test_install_registers_provider(self):
        ctx = Context(name="office")
        try:
            install_skills(ctx)
            install_office_skills(ctx)
            skills = ctx.get("skills")
            catalog = skills.snapshot()
            names = [entry["name"] for entry in catalog.get("skills", [])]
            for name in OFFICE_SKILL_NAMES:
                self.assertIn(name, names)
        finally:
            ctx.dispose()


class TestParsePrimaryRuntime(unittest.TestCase):
    def test_valid_manifest(self):
        manifest = parse_primary_runtime(_runtime_manifest(packages={
            "numpy": "1.26.0", "openpyxl": "3.1.2"}))
        self.assertEqual(manifest["platform"], "win32")
        self.assertEqual(manifest["pythonPackages"]["numpy"], "1.26.0")

    def test_invalid_metadata_rejected(self):
        for value in (None, [], "x", {}, {"platform": "win32"},
                      _runtime_manifest(platform="other"),
                      _runtime_manifest(arch="other"),
                      _runtime_manifest(python="not-a-version"),
                      _runtime_manifest(packages={"bad name!": "1"})):
            with self.assertRaisesRegex(RuntimeError, "invalid metadata"):
                parse_primary_runtime(value)

    def test_duplicate_distribution_names_rejected(self):
        # 归一化键冲突：name 大小写/分隔符归一后相同 → invalid metadata
        with self.assertRaisesRegex(RuntimeError, "invalid metadata"):
            parse_primary_runtime(_runtime_manifest(packages={
                "numpy": "1.0", "Numpy": "2.0"}))

    def test_legacy_components_normalized(self):
        manifest = parse_primary_runtime({
            "desktopVersion": "0.1.7", "platform": "linux", "arch": "x64",
            "components": {"python": "3.12.0", "numpy": "1.26.0",
                           "pandas": "2.1.0"}})
        self.assertEqual(manifest["python"], "3.12.0")
        self.assertEqual(manifest["pythonPackages"], {})

    def test_pnpm_requires_node(self):
        # pnpm 声明但 node 缺席 → invalid metadata
        value = _runtime_manifest()
        value["pnpm"] = "9.0.0"
        with self.assertRaisesRegex(RuntimeError, "invalid metadata"):
            parse_primary_runtime(value)
        value["node"] = "20.0.0"
        self.assertEqual(parse_primary_runtime(value)["pnpm"], "9.0.0")


class TestWorkspaceDependencyPaths(unittest.TestCase):
    def test_windows_paths(self):
        manifest = parse_primary_runtime(
            _runtime_manifest(node="20.0.0", pnpm="9.0.0"))
        paths = workspace_dependency_paths(r"C:\payload", manifest)
        self.assertTrue(paths["python"].endswith("python.exe"))
        self.assertIn("Lib", paths["pythonPackages"])
        self.assertIn("node.exe", paths["node"])
        self.assertIn("pnpm.mjs", paths["pnpm"])
        self.assertEqual(paths["pythonDistributions"], {})

    def test_posix_paths(self):
        manifest = parse_primary_runtime(_runtime_manifest(
            platform="linux", python="3.11.5"))
        paths = workspace_dependency_paths("/payload", manifest)
        self.assertTrue(paths["python"].endswith("bin/python3"))
        self.assertIn("python3.11", paths["pythonPackages"])


class TestResolvePrimaryRuntime(unittest.TestCase):
    def test_resolves_platform_manifest(self):
        tag = "win32" if os.name == "nt" else (
            "darwin" if sys.platform == "darwin" else "linux")
        with tempfile.TemporaryDirectory() as tmp:
            deps = os.path.join(tmp, "dependencies")
            pydir = os.path.join(deps, "python")
            os.makedirs(os.path.join(pydir, "Lib") if tag == "win32"
                        else os.path.join(pydir, "bin"))
            with open(os.path.join(tmp, "runtime.json"), "w", encoding="utf-8") as h:
                json.dump(_runtime_manifest(platform=tag), h)
            if tag == "win32":
                os.makedirs(os.path.join(pydir, "Lib", "site-packages"))
                open(os.path.join(pydir, "python.exe"), "wb").close()
            else:
                os.makedirs(os.path.join(pydir, "lib", "python3.12",
                                         "site-packages"))
                open(os.path.join(pydir, "bin", "python3"), "wb").close()
            paths = resolve_primary_runtime(tmp)
            self.assertTrue(os.path.exists(paths["python"]))
            self.assertTrue(os.path.isdir(paths["pythonPackages"]))

    def test_incompatible_platform_rejected(self):
        tag = "win32" if os.name == "nt" else (
            "darwin" if sys.platform == "darwin" else "linux")
        other = next(p for p in ("win32", "darwin", "linux") if p != tag)
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "runtime.json"), "w", encoding="utf-8") as h:
                json.dump(_runtime_manifest(platform=other), h)
            with self.assertRaisesRegex(RuntimeError, "incompatible"):
                resolve_primary_runtime(tmp)


class TestWorkspaceDependenciesTool(unittest.TestCase):
    def test_registers_and_executes_native(self):
        ctx = Context(name="ws-deps")
        try:
            reg = ToolRegistry(ctx)
            register_workspace_dependencies_tool(reg)
            tool = reg.resolve("load_workspace_dependencies")
            self.assertIsNotNone(tool)
            value = tool.execute({}, None)
            self.assertTrue(value["python"])
            self.assertTrue(value["pythonPackages"])
            self.assertIsInstance(value["pythonDistributions"], dict)
        finally:
            ctx.dispose()

    def test_registers_with_payload(self):
        ctx = Context(name="ws-deps-payload")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                deps = os.path.join(tmp, "dependencies")
                pydir = os.path.join(deps, "python")
                os.makedirs(os.path.join(pydir, "bin"))
                with open(os.path.join(tmp, "runtime.json"), "w",
                          encoding="utf-8") as h:
                    json.dump(_runtime_manifest(
                        platform="linux" if os.name != "nt" else "win32"), h)
                if os.name == "nt":
                    os.makedirs(os.path.join(pydir, "Lib", "site-packages"))
                    open(os.path.join(pydir, "python.exe"), "wb").close()
                else:
                    os.makedirs(os.path.join(pydir, "lib", "python3.12",
                                             "site-packages"))
                    open(os.path.join(pydir, "bin", "python3"), "wb").close()
                reg = ToolRegistry(ctx)
                register_workspace_dependencies_tool(reg, source=tmp)
                tool = reg.resolve("load_workspace_dependencies")
                value = tool.execute({}, None)
                self.assertTrue(os.path.exists(value["python"]))
        finally:
            ctx.dispose()

    def test_relative_source_rejected(self):
        ctx = Context(name="ws-deps-rel")
        try:
            reg = ToolRegistry(ctx)
            with self.assertRaisesRegex(RuntimeError, "absolute"):
                register_workspace_dependencies_tool(reg, source="relative")
        finally:
            ctx.dispose()


if __name__ == "__main__":
    unittest.main()