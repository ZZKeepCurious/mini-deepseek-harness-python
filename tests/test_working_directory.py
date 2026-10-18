"""working-directory 验收：ctx.workingDirectory 服务 + working_directory 工具。

上游对照：packages/session/working-directory/src/index.ts +
packages/session/tool-working-directory/src/index.ts +
tests/working-directory.spec.ts + tests/admission.spec.ts。

覆盖：目录会话日志 + 投影读面、相对/空/掩码路径校验、缺目录恢复、并发串行化、
取消不提交、装配期 ensure 与必需上下文写回（含变换无法吞掉）、工具读/改。
运行：python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import threading
import unittest

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.system_prompt import (
    install_system_prompt,
    render_context_snapshot,
    render_prompt,
)
from miniharness.core.tools import ToolExec, ToolRegistry, run_pipeline_async
from miniharness.fs.local import LocalFileSystem
from miniharness.llm import FakeLlmAdapter
from miniharness.session_projection import install_session_projections
from miniharness.working_directory import (
    CONTEXT_NAME,
    EVENT_TYPE,
    PROJECTION_KEY,
    SOURCE_KIND,
    effective_working_directory,
    install_working_directory,
    register_working_directory_tool,
)


def _run(coro):
    return asyncio.run(coro)


class _Env:
    def __init__(self, test, config=None):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="mini-wd-"))
        test.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.ctx = Context(name="wd-test")
        install_session_projections(self.ctx)
        LocalFileSystem(self.ctx, {"cwd": self.root})
        install_system_prompt(self.ctx)
        self.service = install_working_directory(self.ctx, config or {})
        self.registry = ToolRegistry(self.ctx)

    def agent(self, session_id="wd-agent", meta_cwd=True):
        meta = {"cwd": self.root} if meta_cwd else {}
        session = Session(session_id, meta=meta)
        loop = AgentLoop(session, FakeLlmAdapter(final_text="ok"),
                         self.registry, self.ctx)
        return loop


def _changes(session):
    return [e for e in session.events if e["type"] == EVENT_TYPE]


def _notices(session):
    out = []
    for event in session.events:
        if event["type"] != "user/message":
            continue
        source = (event.get("data") or {}).get("source") or {}
        if source.get("kind") == SOURCE_KIND:
            out.append(event["data"]["content"][0]["text"])
    return out


class WorkingDirectoryServiceTest(unittest.TestCase):
    """目录会话日志 + 投影 + 相对/校验 + 恢复 + 串行化 + 取消。"""

    def test_set_changes_effective_directory_and_logs_change(self):
        env = _Env(self)
        child = os.path.join(env.root, "child")
        os.mkdir(child)
        agent = env.agent()
        resolved = _run(env.service.set(agent, "child"))
        self.assertEqual(resolved, child)
        self.assertEqual(env.service.get(agent.session), child)
        self.assertEqual(agent.session.meta["cwd"], env.root)
        self.assertEqual([e["data"] for e in _changes(agent.session)], [{"cwd": child}])
        agent.followup("hi")
        notices = _notices(agent.session)
        self.assertEqual(
            notices,
            [f"The working directory changed from {json.dumps(env.root)} "
             f"to {json.dumps(child)}."])

    def test_other_sessions_are_independent(self):
        env = _Env(self)
        child = os.path.join(env.root, "child")
        os.mkdir(child)
        agent = env.agent("owner")
        sibling = env.agent("sibling")
        _run(env.service.set(agent, "child"))
        self.assertEqual(env.service.get(agent.session), child)
        self.assertEqual(env.service.get(sibling.session), env.root)

    def test_get_uses_header_cwd_before_any_change(self):
        env = _Env(self)
        agent = env.agent()
        self.assertEqual(env.service.get(agent.session), env.root)

    def test_restores_directory_state_from_log(self):
        env = _Env(self)
        child = os.path.join(env.root, "child")
        os.mkdir(child)
        agent = env.agent()
        _run(env.service.set(agent, child))
        restored = Session(
            "restored", seed=list(agent.session.events), meta={"cwd": env.root})
        self.assertEqual(env.service.get(restored), child)

    def test_projection_wire_view_without_agent(self):
        env = _Env(self)
        moved = os.path.join(env.root, "moved")
        os.mkdir(moved)
        agent = env.agent("with-origin")
        projections = env.ctx.get("sessionProjections")
        # 无 origin 元数据的会话投影初值为 null
        snapshot0 = projections.snapshot(Session("wire-without-origin"),
                                         [PROJECTION_KEY])
        self.assertIsNone(snapshot0["values"][PROJECTION_KEY])
        # 有 header cwd 的会话投影初值即 header
        self.assertEqual(
            projections.snapshot(agent.session, [PROJECTION_KEY])["values"][PROJECTION_KEY],
            env.root)
        _run(env.service.set(agent, moved))
        restored = Session(
            "wire-restored", seed=list(agent.session.events), meta={"cwd": env.root})
        snapshot = projections.snapshot(restored, [PROJECTION_KEY])
        self.assertEqual(snapshot["values"][PROJECTION_KEY], moved)

    def test_serializes_concurrent_relative_changes_and_recovers(self):
        env = _Env(self)
        os.makedirs(os.path.join(env.root, "a", "b"))
        agent = env.agent()

        async def scenario():
            first = env.service.set(agent, "a")
            second = env.service.set(agent, "b")
            return await asyncio.gather(first, second)

        results = _run(scenario())
        self.assertEqual(results, [os.path.join(env.root, "a"),
                                   os.path.join(env.root, "a", "b")])
        with self.assertRaisesRegex(ValueError, "directory does not exist"):
            _run(env.service.set(agent, "missing"))
        self.assertEqual(_run(env.service.set(agent, "..")),
                         os.path.join(env.root, "a"))

    def test_recovers_to_original_when_directory_disappears(self):
        env = _Env(self)
        child = os.path.join(env.root, "temporary")
        os.mkdir(child)
        agent = env.agent()
        _run(env.service.set(agent, child))
        agent.followup("hi")
        shutil.rmtree(child)
        restored = _run(env.service.ensure(agent))
        self.assertEqual(restored, env.root)
        agent.followup("again")
        self.assertIn(
            f"The working directory {json.dumps(child)} is unavailable. "
            f"The working directory is now {json.dumps(env.root)}.",
            _notices(agent.session))

    def test_missing_original_project_reports_and_recovers_explicitly(self):
        env = _Env(self)
        origin = os.path.join(env.root, "original")
        os.mkdir(origin)
        agent = env.agent("removed-origin")
        agent.session.meta["cwd"] = origin
        shutil.rmtree(origin)
        with self.assertRaisesRegex(ValueError, "directory does not exist"):
            _run(env.service.ensure(agent))
        self.assertEqual(_run(env.service.set(agent, env.root)), env.root)

    def test_rejects_files_empty_and_cancelled(self):
        env = _Env(self)
        with open(os.path.join(env.root, "file"), "w", encoding="utf-8") as handle:
            handle.write("")
        agent = env.agent()
        with self.assertRaisesRegex(ValueError, "directory does not exist"):
            _run(env.service.set(agent, "file"))
        with self.assertRaisesRegex(ValueError, "cd must not be empty"):
            _run(env.service.set(agent, ""))
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(RuntimeError):
            _run(env.service.set(agent, env.root, cancelled))
        self.assertEqual(env.service.get(agent.session), env.root)
        self.assertEqual(_changes(agent.session), [])

    def test_unchanged_reads_and_changes_free_of_notices(self):
        env = _Env(self)
        agent = env.agent()
        _run(env.service.ensure(agent))
        _run(env.service.set(agent, env.root))
        agent.followup("hi")
        self.assertEqual(_notices(agent.session), [])
        self.assertEqual(_changes(agent.session), [])

    def test_ensure_records_launch_fallback_for_originless_session(self):
        env = _Env(self)
        agent = env.agent("without-origin", meta_cwd=False)
        fallback = env.service.default_directory
        self.assertEqual(_run(env.service.ensure(agent)), fallback)
        self.assertEqual([e["data"] for e in _changes(agent.session)],
                         [{"cwd": fallback}])

    def test_rejects_relative_default_directory(self):
        ctx = Context(name="wd-bad")
        install_session_projections(ctx)
        LocalFileSystem(ctx, {"cwd": os.getcwd()})
        install_system_prompt(ctx)
        with self.assertRaisesRegex(ValueError, "defaultDirectory must be absolute"):
            install_working_directory(ctx, {"defaultDirectory": "relative/project"})

    def test_effective_working_directory_helper(self):
        env = _Env(self)
        child = os.path.join(env.root, "child")
        os.mkdir(child)
        agent = env.agent()
        self.assertEqual(effective_working_directory(agent), env.root)
        _run(env.service.set(agent, child))
        self.assertEqual(effective_working_directory(agent), child)
        self.assertEqual(effective_working_directory(agent.session, env.ctx), child)


class WorkingDirectoryAssembleTest(unittest.TestCase):
    """装配期 ensure + 必需上下文写回（变换无法吞掉；变量名目录字面呈现）。"""

    def test_required_context_instead_of_system_prompt_section(self):
        env = _Env(self)
        child = os.path.join(env.root, "temporary")
        os.mkdir(child)
        agent = env.agent()
        _run(env.service.set(agent, child))
        shutil.rmtree(child)
        prompt = env.ctx.get("systemPrompt")
        assembly = _run(prompt.assemble({"agent": agent, "scope": agent}))
        self.assertIn(f"Current working directory: {json.dumps(env.root)}.",
                      render_context_snapshot(assembly))
        self.assertNotIn(env.root, render_prompt(assembly))
        self.assertEqual(render_context_snapshot(_run(prompt.assemble())), "")

    def test_transform_cannot_drop_required_current_directory(self):
        env = _Env(self)
        agent = env.agent()
        prompt = env.ctx.get("systemPrompt")

        async def drop(cur, nxt):
            payload = await nxt()
            return {**payload, "assembly": {**payload["assembly"], "contexts": []}}
        env.ctx.on("system-prompt/assemble", drop)
        assembly = _run(prompt.assemble({"agent": agent, "scope": agent}))
        self.assertEqual(assembly["contexts"], [{
            "name": CONTEXT_NAME,
            "text": f"Current working directory: {json.dumps(env.root)}.",
            "interpolate": False,
        }])
        self.assertNotIn(env.root, render_prompt(assembly))

    def test_variable_looking_directory_names_render_literally(self):
        env = _Env(self)
        directory = os.path.join(env.root, "{{project}}-{{unknown}}")
        os.mkdir(directory)
        agent = env.agent()
        prompt = env.ctx.get("systemPrompt")
        prompt.variable("project", lambda ctx: "wrong-directory")
        _run(env.service.set(agent, directory))
        assembly = _run(prompt.assemble({"agent": agent, "scope": agent}))
        snapshot = render_context_snapshot(assembly)
        self.assertIn(f"Current working directory: {json.dumps(directory)}.", snapshot)
        self.assertNotIn("wrong-directory", snapshot)


class WorkingDirectoryToolTest(unittest.TestCase):
    """工作目录工具：读 / 改 / 无 agent 拒绝 / 注销。"""

    def test_tool_reads_and_changes_and_unregisters(self):
        env = _Env(self)
        child = os.path.join(env.root, "child")
        os.mkdir(child)
        agent = env.agent()
        registry = env.registry
        disposer = register_working_directory_tool(env.ctx, registry)
        tool = registry.resolve("working_directory")
        self.assertIsNotNone(tool)

        def execute(args, with_agent=True):
            exec_ = ToolExec(agent=agent if with_agent else None,
                             signal=threading.Event())
            return _run(run_pipeline_async(env.ctx, tool, args, exec_))

        self.assertFalse(execute({}).is_error)
        self.assertEqual(execute({}).value["cwd"], env.root)
        self.assertEqual(execute({"cd": "child"}).value["cwd"], child)
        self.assertTrue(execute({}, with_agent=False).is_error)
        disposer()
        self.assertIsNone(registry.resolve("working_directory"))


if __name__ == "__main__":
    unittest.main()
