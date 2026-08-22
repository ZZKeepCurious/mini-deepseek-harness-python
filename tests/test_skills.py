"""skills 模块验收：分层注册表 / 文件系统 provider / 渲染与 digest / tool_skill 注入。

覆盖：SKILL_NAME 校验、register/list/snapshot/get、rank 与 scope 层决胜、
incomplete 不缓存、revision 抖动重试、invalidate 清缓存与 emit、filesystem
六类根发现与 fail-closed、frontmatter（pyyaml 或子集解析器）、invocation
kebab-case 策略、渲染精确文本、digest 确定性、skill 工具三态错误、present_call、
手势注入（user 源过滤/去重/未知名忽略）、catalog 注入（可见性、digest 幂等、
首次空目录不注入、replacement 与退役）、install_skills 幂等与工具收编。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.tools import ToolRegistry
from miniharness.skills import (
    BUNDLED_SKILL_RANK,
    FileSystemSkillProvider,
    SkillRegistry,
    digest_catalog_entries,
    escape_attr,
    escape_text,
    install_badge_skill,
    install_skills,
    is_model_invocable,
    is_skill_name,
    register_skill_tools,
    render_skill_content,
)
from miniharness.skills.filesystem import (
    find_project_root,
    parse_invocation_policy,
    parse_skill_file,
)
from miniharness.skills.registry import (
    RUNTIME_PROVIDER,
    RUNTIME_RANK,
    validate_candidate,
    validate_definition,
    validate_runtime_skill,
)
from miniharness.skills.tool_skill import (
    SKILL_GESTURE,
    SkillTool,
    catalog_source_entries,
    invoked_skill_names,
)

SIMPLE_SKILL = {
    "name": "bun",
    "description": "Bundle JavaScript for the browser.",
    "whenToUse": "When the user wants to build a browser bundle.",
    "invocation": {"modelInvocable": True, "userInvocable": True},
    "source": "test",
    "rank": 10,
    "provider": "test",
    "content": "1. Run `bun build ./index.ts --outdir=dist`.",
}


def _make_ctx() -> Context:
    ctx = Context(name="skills-test")
    registry = SkillRegistry(ctx)
    return ctx, registry


def _fake_agent(ctx: Context, session: Session, tools: ToolRegistry):
    return SimpleNamespace(session=session, tools=tools, ctx=ctx, cwd=None)


# ---------- 名字与校验 ----------

class SkillNameTest(unittest.TestCase):
    def test_valid(self):
        for name in ("git", "docker-compose", "a", "x1-y2"):
            self.assertTrue(is_skill_name(name), name)

    def test_invalid(self):
        for name in ("Bun", "bun build", "-bun", "bun-", "bun--x", "a/b", "a_b", ""):
            self.assertFalse(is_skill_name(name), name)


class ValidateCandidateTest(unittest.TestCase):
    def test_valid(self):
        validate_candidate(SIMPLE_SKILL, "test")

    def test_bad_name(self):
        with self.assertRaises(ValueError):
            validate_candidate({**SIMPLE_SKILL, "name": "Not-Kebab"}, "test")

    def test_missing_description(self):
        with self.assertRaises(ValueError):
            validate_candidate({**SIMPLE_SKILL, "description": ""}, "test")

    def test_missing_invocation_flags(self):
        with self.assertRaises(TypeError):
            validate_candidate({**SIMPLE_SKILL, "invocation": {"modelInvocable": "yes"}}, "test")

    def test_provider_mismatch(self):
        with self.assertRaises(ValueError):
            validate_candidate({**SIMPLE_SKILL, "provider": "other"}, "test")


# ---------- registry：分层与缓存 ----------

class RegistryLayersTest(unittest.TestCase):
    def test_runtime_register_and_summary(self):
        ctx, registry = _make_ctx()
        registry.register({**SIMPLE_SKILL, "provider": RUNTIME_PROVIDER, "rank": RUNTIME_RANK})
        summary = registry.list({})
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["name"], "bun")
        self.assertEqual(summary[0]["provider"], RUNTIME_PROVIDER)
        self.assertIn("whenToUse", summary[0])

    def test_runtime_invocation_defaults_open(self):
        ctx, registry = _make_ctx()
        skill = {k: v for k, v in SIMPLE_SKILL.items() if k not in ("invocation", "provider", "rank")}
        registry.register(skill)
        summary = registry.list({})[0]
        self.assertTrue(is_model_invocable(summary))
        self.assertTrue(summary["invocation"]["userInvocable"])

    def test_runtime_duplicate_first_wins(self):
        ctx, registry = _make_ctx()
        registry.register({**SIMPLE_SKILL, "description": "first"})
        registry.register({**SIMPLE_SKILL, "description": "second"})
        self.assertEqual(registry.list({})[0]["description"], "first")

    def test_rank_winner(self):
        ctx, registry = _make_ctx()
        low = dict(SIMPLE_SKILL, rank=50, provider="low")
        high = dict(SIMPLE_SKILL, description="higher rank", rank=10, provider="high")
        registry.register_provider(_provider_factory([low], name="low"))
        registry.register_provider(_provider_factory([high], name="high"))
        self.assertEqual(registry.list({})[0]["description"], "higher rank")

    def test_scoped_shadow_wins(self):
        ctx, registry = _make_ctx()
        parent = Context(ctx, name="parent")
        child = Context(parent, name="child")
        registry.register({**SIMPLE_SKILL, "description": "global"})
        registry.register({**SIMPLE_SKILL, "description": "scoped"}, scope=child)
        self.assertEqual(registry.list({})[0]["description"], "global")
        self.assertEqual(registry.list({"scope": child})[0]["description"], "scoped")
        self.assertEqual(registry.list({"scope": parent})[0]["description"], "global")

    def test_register_provider_reserved_name(self):
        ctx, registry = _make_ctx()
        with self.assertRaises(ValueError):
            registry.register_provider(lambda c: {"name": RUNTIME_PROVIDER, "list": lambda o: [], "get": lambda c2, o: None})

    def test_register_provider_duplicate(self):
        ctx, registry = _make_ctx()
        registry.register_provider(lambda c: {"name": "p", "list": lambda o: [], "get": lambda c2, o: None})
        with self.assertRaises(ValueError):
            registry.register_provider(lambda c: {"name": "p", "list": lambda o: [], "get": lambda c2, o: None})

    def test_provider_error_keeps_complete_flag(self):
        ctx, registry = _make_ctx()
        good = {"name": "g", "description": "good", "invocation": {"modelInvocable": True, "userInvocable": True},
                "source": "p", "rank": 1, "provider": "good"}
        registry.register_provider(_provider_factory([good], error=True))
        snapshot = registry.snapshot({})
        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["skills"], [])

    def test_get_returns_definition(self):
        ctx, registry = _make_ctx()
        registry.register({**SIMPLE_SKILL, "provider": RUNTIME_PROVIDER})
        definition = registry.get("bun", {})
        self.assertEqual(definition["content"], SIMPLE_SKILL["content"])
        self.assertEqual(definition["provider"], RUNTIME_PROVIDER)

    def test_get_invalid_name_none(self):
        ctx, registry = _make_ctx()
        registry.register({**SIMPLE_SKILL, "provider": RUNTIME_PROVIDER})
        self.assertIsNone(registry.get("Bun", {}))

    def test_invalidate_revision_and_cache(self):
        ctx, registry = _make_ctx()
        calls = []
        provider = {"name": "count", "list": lambda o: calls.append(1) or [], "get": lambda c, o: None}
        registry.register_provider(lambda c: provider)
        registry.list({})
        registry.list({})
        self.assertEqual(len(calls), 1)  # 缓存命中
        registry.invalidate_cache()
        registry.list({})
        self.assertEqual(len(calls), 2)


class Provider:
    """可复用测试 provider：固定候选 + 可选抛错/不稳定观察。"""

    def __init__(self, candidates, error=False, incomplete=False, unstable=False):
        self.candidates = candidates
        self.error = error
        self.incomplete = incomplete
        self.unstable = unstable
        self._control = None

    def list(self, options):
        if self.error:
            raise RuntimeError("boom")
        if self.unstable and self._control is not None:
            self._control["invalidate"]()
        if self.incomplete:
            return {"candidates": self.candidates, "complete": False}
        return self.candidates

    def get(self, candidate, options):
        return {**candidate, "content": "loaded"}


def _provider_factory(candidates, name="p", **kw):
    def make(control):
        provider = Provider(candidates, **kw)
        provider._control = control
        return {"name": name, "list": provider.list, "get": provider.get}
    return make


class RegistryCollectTest(unittest.TestCase):
    def test_incomplete_not_cached(self):
        ctx, registry = _make_ctx()
        seen = []
        def make(control):
            def list(options):
                seen.append(1)
                return {"candidates": [], "complete": False}
            return {"name": "p", "list": list, "get": lambda c, o: None}
        registry.register_provider(make)
        registry.snapshot({})
        registry.snapshot({})
        self.assertEqual(len(seen), 2)

    def test_revision_jitter_retries(self):
        ctx, registry = _make_ctx()
        calls = {"n": 0}
        def make(control):
            def list(options):
                calls["n"] += 1
                if calls["n"] == 1:
                    control["invalidate"]()
                return []
            return {"name": "p", "list": list, "get": lambda c, o: None}
        registry.register_provider(make)
        snapshot = registry.snapshot({})
        self.assertTrue(snapshot["complete"])
        self.assertEqual(calls["n"], 2)

    def test_provider_identity_in_merge(self):
        ctx, registry = _make_ctx()
        cand = {"name": "x", "description": "x", "invocation": {"modelInvocable": True, "userInvocable": True},
                "source": "p", "rank": 1, "provider": "p"}
        registry.register_provider(_provider_factory([cand]))
        entry = registry._collect({})["entries"]["x"]
        self.assertEqual(entry["candidate"]["provider"], "p")
        definition = registry.get("x", {})
        self.assertEqual(definition["content"], "loaded")


# ---------- 渲染与 digest ----------

class RenderSkillContentTest(unittest.TestCase):
    def test_directory_hint(self):
        skill = {**SIMPLE_SKILL, "resourceBase": {"kind": "directory", "path": "/proj"}}
        text = render_skill_content(skill)
        self.assertIn('<skill_content name="bun">', text)
        self.assertIn("<skill_instructions>", text)
        self.assertIn("Base directory for this skill: /proj", text)
        self.assertIn(SIMPLE_SKILL["content"], text)

    def test_escape_attr(self):
        self.assertEqual(escape_attr('a"<&'), "a&quot;&lt;&amp;")

    def test_escape_text(self):
        self.assertEqual(escape_text("a<b>&c"), "a&lt;b&gt;&amp;c")

    def test_digest_deterministic(self):
        entries = [{"name": "a", "description": "x"}, {"name": "b", "description": "y"}]
        self.assertEqual(digest_catalog_entries(entries), digest_catalog_entries(entries))
        changed = [{"name": "a", "description": "x2"}, {"name": "b", "description": "y"}]
        self.assertNotEqual(digest_catalog_entries(entries), digest_catalog_entries(changed))


# ---------- 手势 ----------

class InvokedSkillNamesTest(unittest.TestCase):
    def test_gesture_token(self):
        names = invoked_skill_names([
            {"source": {"kind": "user"}, "content": [{"type": "text", "text": "please /bun it"}]},
        ])
        self.assertEqual(names, ["bun"])

    def test_dedupe_first_seen(self):
        names = invoked_skill_names([
            {"source": {"kind": "user"}, "content": [{"type": "text", "text": "/a /b /a"}]},
        ])
        self.assertEqual(names, ["a", "b"])

    def test_only_user_source(self):
        names = invoked_skill_names([
            {"source": {"kind": "skill-catalog"}, "content": [{"type": "text", "text": "/a"}]},
        ])
        self.assertEqual(names, [])

    def test_boundary_no_false_positives(self):
        for text in ("/usr/bin", "5/8", "a/b", "bun/", "user", "bun/x", "/"):
            names = invoked_skill_names([{"source": {"kind": "user"}, "content": [{"type": "text", "text": text}]}])
            self.assertEqual(names, [], text)


# ---------- filesystem ----------

class FileSystemProviderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="miniharness-skills-")
        self.root = Path(self._tmp).resolve()

    def _write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _provider(self, **config) -> FileSystemSkillProvider:
        base = {"customSkillDirs": [str(self.root / "custom")]}
        return FileSystemSkillProvider({"invalidate": lambda: None}, {**base, **config})

    def test_directory_bundle_and_flat(self):
        self._write("custom/hello/SKILL.md",
                    "---\nname: hello\ndescription: Say hi\n---\n\nBody text here.\n")
        self._write("custom/flat.md",
                    "---\nname: flat\ndescription: Flat file\n---\n\nFlat body.\n")
        provider = self._provider()
        skills = {s["name"]: s for s in provider.list({})}
        self.assertEqual(set(skills), {"hello", "flat"})
        self.assertEqual(skills["hello"]["source"], "custom")
        self.assertEqual(skills["hello"]["rank"], 300)
        definition = provider.get(skills["hello"], {})
        self.assertEqual(definition["content"], "Body text here.")
        self.assertEqual(definition["resourceBase"], {"kind": "directory", "path": str(self.root / "custom" / "hello")})

    def test_missing_root_empty(self):
        provider = self._provider()
        self.assertEqual(provider.list({}), [])

    def test_invalid_frontmatter_skipped(self):
        self._write("custom/bad/SKILL.md", "no frontmatter here\n")
        self._write("custom/good/SKILL.md",
                    "---\nname: good\ndescription: ok\n---\n\nBody.\n")
        provider = self._provider()
        names = [s["name"] for s in provider.list({})]
        self.assertEqual(names, ["good"])

    def test_invalid_skill_name_skipped(self):
        self._write("custom/bad/SKILL.md", "---\nname: Not-Kebab\ndescription: x\n---\n\nBody.\n")
        provider = self._provider()
        self.assertEqual(provider.list({}), [])

    def test_missing_description_skipped(self):
        self._write("custom/bad/SKILL.md", "---\nname: ok\ndescription:\n---\n\nBody.\n")
        provider = self._provider()
        self.assertEqual(provider.list({}), [])

    def test_project_roots_require_cwd(self):
        project = self.root / "proj"
        (project / ".git").mkdir(parents=True, exist_ok=True)
        self._write("proj/.dsh/skills/projskill/SKILL.md",
                    "---\nname: projskill\ndescription: project skill\n---\n\nBody.\n")
        self._write("proj/.agents/skills/agentskill.md",
                    "---\nname: agentskill\ndescription: agent skill\n---\n\nBody.\n")
        provider = self._provider()
        self.assertEqual(provider.list({}), [])  # 无 cwd 不扫项目根
        by_source = {}
        for s in provider.list({"cwd": str(project)}):
            by_source[s["source"]] = s
        self.assertEqual(by_source["project-dsh"]["rank"], 100)
        self.assertEqual(by_source["project-agents"]["rank"], 200)

    def test_dot_system_skipped_in_user_root(self):
        self._write("home/skills/.system/x/SKILL.md",
                    "---\nname: x\ndescription: system\n---\n\nBody.\n")
        self._write("home/skills/y.md", "---\nname: y\ndescription: user\n---\n\nBody.\n")
        provider = FileSystemSkillProvider({"invalidate": lambda: None}, {
            "includeDefaultRoots": True,
            "dshHome": str(self.root / "home"),
            "customSkillDirs": [],
        })
        names = [s["name"] for s in provider.list({})]
        self.assertEqual(names, ["y"])

    def test_bundled_root_rank(self):
        self._write("bundle/b1/SKILL.md", "---\nname: b1\ndescription: bundled\n---\n\nBody.\n")
        provider = FileSystemSkillProvider({"invalidate": lambda: None}, {
            "includeDefaultRoots": False,
            "customSkillDirs": [],
            "bundledSkillDir": str(self.root / "bundle"),
        })
        skills = provider.list({})
        self.assertEqual(skills[0]["source"], "bundled")
        self.assertEqual(skills[0]["rank"], BUNDLED_SKILL_RANK)

    def test_find_project_root(self):
        project = self.root / "a" / "b"
        (project / ".git").mkdir(parents=True, exist_ok=True)
        self.assertEqual(find_project_root(str(project)), project.resolve())

    def test_parse_invocation_kebab_case(self):
        self.assertEqual(parse_invocation_policy({"user-invocable": False}),
                         {"modelInvocable": True, "userInvocable": False})
        self.assertEqual(parse_invocation_policy({"disable-model-invocation": True}),
                         {"modelInvocable": False, "userInvocable": True})
        self.assertEqual(parse_invocation_policy({}),
                         {"modelInvocable": True, "userInvocable": True})
        with self.assertRaises(ValueError):
            parse_invocation_policy({"modelInvocable": True})

    def test_parse_skill_file_frontmatter(self):
        parsed = parse_skill_file("---\nname: git\ndescription: Git ops\nwhenToUse: branch work\n---\n\n# Body\n")
        self.assertEqual(parsed["name"], "git")
        self.assertEqual(parsed["description"], "Git ops")
        self.assertEqual(parsed["content"], "# Body")
        self.assertEqual(parsed["invocation"], {"modelInvocable": True, "userInvocable": True})

    def test_parse_skill_file_no_frontmatter(self):
        self.assertIsNone(parse_skill_file("just body"))

    def test_frontmatter_yaml_subset(self):
        # 无 pyyaml 时子集解析器也必须能吃常见 frontmatter（含 metadata 嵌套）
        parsed = parse_skill_file(
            "---\n"
            "name: tool\n"
            "description: \"Quoted: desc\"\n"
            "metadata:\n"
            "  owner: team\n"
            "  enabled: true\n"
            "---\n"
            "\n"
            "Body.\n"
        )
        self.assertEqual(parsed["description"], "Quoted: desc")
        self.assertEqual(parsed["metadata"], {"owner": "team", "enabled": True})


# ---------- 装配 ----------

class InstallSkillsTest(unittest.TestCase):
    def test_install_idempotent(self):
        ctx = Context(name="a")
        registry = install_skills(ctx)
        registry.register({**SIMPLE_SKILL, "provider": RUNTIME_PROVIDER})
        again = install_skills(ctx)
        self.assertIs(again, registry)
        self.assertEqual([s["name"] for s in registry.list({})], ["bun"])

    def test_register_skill_tools(self):
        ctx = Context(name="b")
        registry = install_skills(ctx)
        reg = ToolRegistry(ctx)
        register_skill_tools(reg, registry)
        tool = reg.resolve("skill")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.name, "skill")

    def test_config_bad_max_length(self):
        ctx = Context(name="c")
        with self.assertRaises(ValueError):
            install_skills(ctx, {"catalogDescriptionMaxLength": 2})


# ---------- tool_skill ----------

class SkillToolExecuteTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="t")
        self.registry = install_skills(self.ctx)
        self.registry.register({**SIMPLE_SKILL, "provider": RUNTIME_PROVIDER})
        self.reg = ToolRegistry(self.ctx)
        register_skill_tools(self.reg, self.registry)
        self.tool = self.reg.resolve("skill")

    def _exec(self):
        return SimpleNamespace(agent=SimpleNamespace(ctx=self.ctx, cwd=None))

    def _call(self, args):
        """执行 skill 工具（async 契约 → asyncio.run 包装）。"""
        return asyncio.run(self.tool.execute(args, self._exec()))

    def test_success_renders_content(self):
        result = self._call({"name": "bun"})
        self.assertIn('<skill_content name="bun">', result)
        self.assertIn(SIMPLE_SKILL["content"], result)

    def test_invalid_name_error(self):
        with self.assertRaises(ValueError) as cm:
            self._call({"name": "Bun"})
        self.assertIn('invalid skill name "Bun"', str(cm.exception))

    def test_unknown_skill_error(self):
        with self.assertRaises(ValueError) as cm:
            self._call({"name": "nope"})
        self.assertEqual(str(cm.exception), 'skill "nope" is unknown or no longer available')

    def test_model_disabled_error(self):
        self.registry.register({**SIMPLE_SKILL, "name": "locked",
                                "description": "locked", "provider": RUNTIME_PROVIDER,
                                "invocation": {"modelInvocable": False, "userInvocable": True}})
        with self.assertRaises(ValueError) as cm:
            self._call({"name": "locked"})
        self.assertEqual(str(cm.exception), 'skill "locked" is not available for model invocation')

    def test_present_call(self):
        card = self.tool.present_call({"name": "bun"})
        self.assertEqual(card, {"card": "generic", "title": "Load skill bun", "kind": "read", "rawInput": "bun"})


class GestureListenerTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="g")
        self.registry = install_skills(self.ctx)
        self.registry.register({**SIMPLE_SKILL, "provider": RUNTIME_PROVIDER})
        self.registry.register({**SIMPLE_SKILL, "name": "model-only", "description": "mo",
                                "provider": RUNTIME_PROVIDER,
                                "invocation": {"modelInvocable": True, "userInvocable": False}})
        self.reg = ToolRegistry(self.ctx)
        register_skill_tools(self.reg, self.registry)
        self.session = Session("g")

    def _pre_step(self, messages):
        agent = _fake_agent(self.ctx, self.session, self.reg)
        return asyncio.run(self.ctx.awaterfall(
            "agent/pre-step", {"agent": agent, "signal": None, "messages": messages}))

    def test_gesture_injects_instructions(self):
        decision = self._pre_step([
            {"id": "m1", "source": {"kind": "user"}, "content": [{"type": "text", "text": "run /bun"}]},
        ])
        injected = [m for m in decision["messages"] if m["source"]["kind"] == "skill-invocation"]
        self.assertEqual(len(injected), 1)
        self.assertEqual(injected[0]["source"]["name"], "bun")
        self.assertEqual(injected[0]["source"]["form"], "instructions")
        self.assertIn("<skill_instructions>", injected[0]["content"][0]["text"])

    def test_unknown_gesture_stays_prose(self):
        # 未知名手势不注入任何 skill-invocation（catalog 注入与否与本断言无关）
        decision = self._pre_step([
            {"id": "m1", "source": {"kind": "user"}, "content": [{"type": "text", "text": "/nope"}]},
        ])
        injected = [m for m in decision["messages"] if m["source"]["kind"] == "skill-invocation"]
        self.assertEqual(len(injected), 0)
        self.assertEqual(decision["messages"][0]["id"], "m1")

    def test_user_disabled_gesture_stays_prose(self):
        # 无 skill 工具可见：目录不注入，只剩原文 → 手势禁用边界保持普通散文
        agent = _fake_agent(self.ctx, self.session,
                            SimpleNamespace(resolve=lambda name: None))
        decision = asyncio.run(self.ctx.awaterfall("agent/pre-step", {
            "agent": agent, "signal": None,
            "messages": [{"id": "m1", "source": {"kind": "user"},
                          "content": [{"type": "text", "text": "/model-only"}]}],
        }))
        self.assertEqual(len(decision["messages"]), 1)
        self.assertEqual(decision["messages"][0]["id"], "m1")

    def test_reject_short_circuits(self):
        # 下游先返回 reject：gesture/catalog 都不注入任何消息
        self.ctx.on("agent/pre-step", lambda payload, next_fn: {"kind": "reject"})
        decision = asyncio.run(self.ctx.awaterfall("agent/pre-step", {
            "agent": _fake_agent(self.ctx, self.session, self.reg),
            "signal": None,
            "messages": [{"id": "m1", "source": {"kind": "user"},
                          "content": [{"type": "text", "text": "/bun"}]}],
        }))
        self.assertEqual(decision["kind"], "reject")
        self.assertNotIn("messages", decision)


class CatalogListenerTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="c")
        self.registry = install_skills(self.ctx)
        self.registry.register({**SIMPLE_SKILL, "provider": RUNTIME_PROVIDER})
        self.reg = ToolRegistry(self.ctx)
        register_skill_tools(self.reg, self.registry)
        self.session = Session("c")
        self.agent = _fake_agent(self.ctx, self.session, self.reg)

    def _pre_step(self, messages):
        return asyncio.run(self.ctx.awaterfall(
            "agent/pre-step", {"agent": self.agent, "signal": None, "messages": messages}))

    def test_first_publish_catalog(self):
        decision = self._pre_step([])
        catalog = [m for m in decision["messages"] if m["source"]["kind"] == "skill-catalog"]
        self.assertEqual(len(catalog), 1)
        source = catalog[0]["source"]
        self.assertEqual(source["form"], "catalog")
        self.assertNotIn("update", source)
        self.assertEqual(source["entries"], [{"name": "bun", "description": SIMPLE_SKILL["description"]}])
        text = catalog[0]["content"][0]["text"]
        self.assertIn("A skill is a reusable set of task-specific instructions", text)
        self.assertIn("- `bun`: Bundle JavaScript for the browser.", text)
        self.assertNotIn("The available skill catalog changed", text)

    def test_description_truncation(self):
        self.registry.register({**SIMPLE_SKILL, "name": "long", "description": "x" * 600,
                                "provider": RUNTIME_PROVIDER})
        decision = self._pre_step([])
        catalog = next(m for m in decision["messages"] if m["source"]["kind"] == "skill-catalog")
        entry = next(e for e in catalog["source"]["entries"] if e["name"] == "long")
        self.assertEqual(entry["description"], "x" * 497 + "...")

    def test_idempotent_same_digest(self):
        first = self._pre_step([])
        catalog = next(m for m in first["messages"] if m["source"]["kind"] == "skill-catalog")
        # 已可见目录（模拟 durable 落日志后再次 pre-step）：内存中无残留 → 原样返回
        again = self._pre_step(first["messages"])
        self.assertEqual(len(again["messages"]), 1)
        # 持久化目录（带 skill-catalog source）后再次注入 → 移除内存残留、不重复发布
        self.session.append("user/message", catalog, surfaceOp="append")
        third = self._pre_step([catalog])
        self.assertNotIn(catalog["id"], [m["id"] for m in third["messages"]])

    def test_empty_catalog_first_time_not_published(self):
        ctx = Context(name="e")
        registry = install_skills(ctx)
        reg = ToolRegistry(ctx)
        register_skill_tools(reg, registry)
        session = Session("e")
        agent = _fake_agent(ctx, session, reg)
        decision = asyncio.run(ctx.awaterfall(
            "agent/pre-step", {"agent": agent, "signal": None, "messages": []}))
        self.assertEqual(decision["messages"], [])

    def test_retirement_after_publish(self):
        # 先发布非空目录（durable），随后 registry 空 → replacement update 退役
        decision = self._pre_step([])
        catalog = next(m for m in decision["messages"] if m["source"]["kind"] == "skill-catalog")
        self.session.append("user/message", catalog, surfaceOp="append")
        self.registry.invalidate_cache()
        self.registry._global.runtime.clear()
        second = self._pre_step([])
        updates = [m for m in second["messages"] if m["source"]["kind"] == "skill-catalog"]
        self.assertEqual(len(updates), 1)
        self.assertTrue(updates[0]["source"].get("update"))
        self.assertIn("No skills are currently available", updates[0]["content"][0]["text"])

    def test_tool_not_registered_no_catalog(self):
        ctx = Context(name="nt")
        registry = install_skills(ctx)
        reg = ToolRegistry(ctx)  # 不注册 skill 工具
        session = Session("nt")
        agent = _fake_agent(ctx, session, reg)
        decision = asyncio.run(ctx.awaterfall(
            "agent/pre-step", {"agent": agent, "signal": None, "messages": []}))
        self.assertEqual(decision["messages"], [])

    def test_model_disabled_skills_excluded(self):
        self.registry.register({**SIMPLE_SKILL, "name": "locked", "description": "locked",
                                "provider": RUNTIME_PROVIDER,
                                "invocation": {"modelInvocable": False, "userInvocable": True}})
        decision = self._pre_step([])
        catalog = next(m for m in decision["messages"] if m["source"]["kind"] == "skill-catalog")
        names = [e["name"] for e in catalog["source"]["entries"]]
        self.assertEqual(names, ["bun"])


class CatalogLoopIntegrationTest(unittest.TestCase):
    """真实 AgentLoop：catalog 经 pre-step 落 durable user/message。"""

    def test_catalog_appears_in_log(self):
        from miniharness.core.agent_loop.agent import AgentLoop
        from miniharness.llm import FakeLlmAdapter
        ctx = Context(name="loop")
        install_skills(ctx)
        registry = ctx.get("skills")
        registry.register({**SIMPLE_SKILL, "provider": RUNTIME_PROVIDER})
        reg = ToolRegistry(ctx)
        register_skill_tools(reg, registry)
        session = Session("loop")
        loop = AgentLoop(session, FakeLlmAdapter(final_text="完成。"), reg, ctx)
        loop.followup("你好")
        catalog_events = [ev for ev in session.events
                          if ev["type"] == "user/message"
                          and ev["data"]["source"]["kind"] == "skill-catalog"]
        self.assertEqual(len(catalog_events), 1)
        # catalog 是 surface 事件（append）
        self.assertEqual(catalog_events[0]["surfaceOp"], "append")
        # 模型历史里能投影出渲染文本
        from miniharness.core.session import derive_messages
        texts = [b.get("text", "") for m in derive_messages(session.events) for b in m["content"]]
        self.assertTrue(any("A skill is a reusable set of task-specific instructions" in t for t in texts))


class GestureLoopIntegrationTest(unittest.TestCase):
    def test_gesture_durable_after_catalog(self):
        from miniharness.core.agent_loop.agent import AgentLoop
        from miniharness.llm import FakeLlmAdapter
        ctx = Context(name="gl")
        install_skills(ctx)
        registry = ctx.get("skills")
        registry.register({**SIMPLE_SKILL, "provider": RUNTIME_PROVIDER})
        reg = ToolRegistry(ctx)
        register_skill_tools(reg, registry)
        session = Session("gl")
        loop = AgentLoop(session, FakeLlmAdapter(final_text="完成。"), reg, ctx)
        loop.followup("运行 /bun")
        # 手势注入的 skill-invocation 消息（不含 catalog，catalog 无 skill）
        gesture_events = [ev for ev in session.events
                          if ev["type"] == "user/message"
                          and ev["data"]["source"]["kind"] == "skill-invocation"]
        self.assertEqual(len(gesture_events), 1)
        self.assertEqual(gesture_events[0]["data"]["source"]["name"], "bun")


class FrontmatterSubsetTest(unittest.TestCase):
    """pyyaml 缺失时子集解析器的核心行为（直接测解析器本体）。"""

    def _parse(self, text: str):
        from miniharness.skills.filesystem import _parse_yaml_subset
        return _parse_yaml_subset(text)

    def test_scalars(self):
        data = self._parse("name: bun\ndesc: 'x'\nnum: 42\nflag: true\nnone: null\n")
        self.assertEqual(data, {"name": "bun", "desc": "x", "num": 42, "flag": True, "none": None})

    def test_nested_mapping(self):
        data = self._parse("metadata:\n  owner: team\n  nested:\n    deep: yes\n")
        self.assertEqual(data["metadata"], {"owner": "team", "nested": {"deep": "yes"}})

    def test_block_scalar_literal(self):
        data = self._parse("desc: |\n  line one\n  line two\n")
        self.assertEqual(data["desc"], "line one\nline two")

    def test_block_scalar_fold(self):
        data = self._parse("desc: >\n  line one\n  line two\n")
        self.assertEqual(data["desc"], "line one line two")

    def test_unsupported_flow_raises(self):
        with self.assertRaises(ValueError):
            self._parse("metadata: {a: 1}\n")

    def test_list_in_mapping(self):
        data = self._parse("items:\n  - a\n  - b\n")
        self.assertEqual(data["items"], ["a", "b"])

    def test_tab_indent_raises(self):
        with self.assertRaises(ValueError):
            self._parse("a:\n\tb: 1\n")


# ---------- 内置 dsh-badge provider（对齐 packages/skill/skill-badge） ----------

class BadgeProviderTest(unittest.TestCase):
    def _make(self):
        ctx = Context(name="badge")
        registry = install_skills(ctx)
        install_badge_skill(ctx)
        return ctx, registry

    def test_provider_registered(self):
        ctx, registry = self._make()
        names = [s["name"] for s in registry.list({})]
        self.assertIn("dsh-badge", names)

    def test_badge_content_loadable(self):
        ctx, registry = self._make()
        definition = registry.get("dsh-badge")
        self.assertIsNotNone(definition)
        self.assertEqual(definition["provider"], "dsh-badge")
        self.assertEqual(definition["source"], "bundled")
        self.assertIn("Powered by DeepSeek Harness", definition["content"])

    def test_badge_invocation_both(self):
        ctx, registry = self._make()
        candidate = next(s for s in registry.list({}) if s["name"] == "dsh-badge")
        self.assertTrue(candidate["invocation"]["modelInvocable"])
        self.assertTrue(candidate["invocation"]["userInvocable"])


if __name__ == "__main__":
    unittest.main()
