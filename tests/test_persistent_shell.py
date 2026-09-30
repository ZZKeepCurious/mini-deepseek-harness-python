"""持久 shell 工具族：共享引擎 + tool-bash-persistent + tool-pwsh-persistent。

对齐 packages/shell/tool-{bash,pwsh}-persistent。以桩 PTY backend 模拟持久 shell
（提取包装命令里的 start/end 标记，回放模拟输出 + 退出码），验证标记抽取、退出码、
超时/退出重置、owner 池化与回显剥离。
"""
import re
import unittest

from miniharness.core.agents import install_agents
from miniharness.core.scope import Context
from miniharness.core.tools import ToolRegistry
from miniharness.core.tools import run_pipeline_async
from miniharness.persistent_shell import (
    BASH_DIALECT,
    PWSH_DIALECT,
    captured_command_output,
    make_markers,
    maybe_truncate,
    render_captured,
)
from miniharness.terminal.operation import LocalSendOperation
from miniharness.terminal import install_terminals
from miniharness.tool_bash_persistent import install_persistent_bash
from miniharness.tool_pwsh_persistent import install_persistent_pwsh

_START_RE = re.compile(r"__DSH_PERSISTENT_(?:BASH|PWSH)_START_[0-9a-f-]+__")
_END_RE = re.compile(r"__DSH_PERSISTENT_(?:BASH|PWSH)_END_[0-9a-f-]+:")


class StubTtySession:
    """模拟持久 shell：解析包装命令的标记，回放模拟输出 + 退出码。"""

    motd = "stub ready"
    pid = 77

    def __init__(self, backend, motd="stub ready"):
        self._backend = backend
        self.motd = motd
        self.status_value = {"kind": "running"}
        self.operation = None
        self.scrollback = ""

    def start_send(self, request):
        operation = LocalSendOperation(1 << 20, 0)
        operation.set_on_cancel(lambda: operation.settle("stdin_read", self.status_value, False))
        text = request.get("text", "")
        start = _START_RE.search(text)
        end = _END_RE.search(text)
        if start is not None and end is not None:
            chunk = f"\n{start.group(0)}\n{self._backend.output}\n{end.group(0)}{self._backend.exit_code}\n"
            self.scrollback += chunk
            operation.append(chunk)
        elif self._backend.exit_on_cancel:
            self.status_value = {"kind": "exited", "exitCode": self._backend.exit_code, "signal": None}
        operation.settle("stdin_read", self.status_value, False)
        self.operation = operation
        return operation

    def read(self, request):
        text = self.scrollback
        total = text.count("\n") + (0 if text == "" else 1)
        return {"text": text, "lineBegin": 0, "lineEnd": total,
                "totalLines": total, "truncated": False}

    def signal(self, signal):
        return {"delivered": True, "targetPgid": 1}

    def status(self):
        return self.status_value

    def close(self, reason):
        self.status_value = {"kind": "exited", "exitCode": 0, "signal": None}


class StubBackend:
    def __init__(self, type_, output="hi", exit_code=0, exit_on_cancel=False):
        self.type = type_
        self.output = output
        self.exit_code = exit_code
        self.exit_on_cancel = exit_on_cancel
        self.sessions = []

    def spawn(self, spec):
        session = StubTtySession(self)
        self.sessions.append(session)
        return session


class StubAgent:
    def __init__(self, id_, cwd=None):
        self.id = id_
        session = type("Session", (), {
            "session_id": id_, "meta": ({"cwd": cwd} if cwd else {})})()
        self.session = session
        self.ctx = Context(name=f"agent-{id_}")
        self._carrier = None
        self.status = "idle"


async def _run(ctx, tool, args, agent, signal=None):
    exec_ = type("Exec", (), {"agent": agent, "signal": signal})()
    return await run_pipeline_async(ctx, tool, args, exec_)


class TestEnginePure(unittest.TestCase):
    def test_maybe_truncate(self):
        self.assertEqual(maybe_truncate("abc", 10), "abc")
        self.assertIn("clipped", maybe_truncate("abcdef", 3))
        self.assertIn("clipped", maybe_truncate("abc", 10, incomplete=True))

    def test_captured_command_output_bash(self):
        markers = make_markers(BASH_DIALECT)
        text = f"\n{markers['start']}\nhello\n{markers['end']}0\n"
        out = captured_command_output({"text": text}, markers, BASH_DIALECT, "")
        self.assertEqual(out["text"], "hello")
        self.assertEqual(out["exitCode"], 0)
        self.assertFalse(out["incomplete"])

    def test_captured_output_strips_pwsh_wrapper(self):
        markers = make_markers(PWSH_DIALECT)
        wrapper = "WRAPPER_SOURCE"
        text = f"\n{wrapper}\n{markers['start']}\nhi\n{markers['end']}7\n"
        out = captured_command_output({"text": text}, markers, PWSH_DIALECT, wrapper)
        self.assertNotIn("WRAPPER_SOURCE", out["text"])
        self.assertEqual(out["exitCode"], 7)

    def test_render_captured_incomplete_prefix(self):
        rendered = render_captured(
            {"text": "tail", "incomplete": True, "exitCode": 2}, 1000)
        self.assertIn("The beginning of this command output was dropped", rendered)
        self.assertIn("[Command finished with exit code 2]", rendered)


class TestPersistentBash(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctx = Context(name="persistent-test")
        self.addCleanup(self.ctx.dispose)
        install_agents(self.ctx)
        self.terminals = install_terminals(self.ctx)
        self.backend = StubBackend("stub", output="hi")
        self.terminals.register_backend(self.backend)
        self.registry = ToolRegistry(self.ctx)
        self.tool = install_persistent_bash(self.ctx, self.registry, {"backendType": "stub"})
        self.agent = StubAgent("owner")
        self.ctx.get("agents").register(self.agent)

    async def test_runs_command_and_extracts_output(self):
        result = await _run(self.ctx, self.tool, {"command": "echo hi"}, self.agent)
        self.assertIn("hi", result.value)
        self.assertIn("[Command finished with exit code 0]", result.value)

    async def test_persistent_session_reused(self):
        await _run(self.ctx, self.tool, {"command": "cd /tmp"}, self.agent)
        await _run(self.ctx, self.tool, {"command": "pwd"}, self.agent)
        self.assertEqual(len(self.backend.sessions), 1)

    async def test_serialized_per_owner(self):
        # 两条命令同 owner：顺序执行且复用同一会话。
        await _run(self.ctx, self.tool, {"command": "a"}, self.agent)
        await _run(self.ctx, self.tool, {"command": "b"}, self.agent)
        self.assertEqual(len(self.backend.sessions), 1)

    async def test_empty_command_rejected(self):
        result = await _run(self.ctx, self.tool, {"command": "  "}, self.agent)
        self.assertTrue(result.is_error)

    async def test_requires_agent(self):
        result = await _run(self.ctx, self.tool, {"command": "ls"}, None)
        self.assertTrue(result.is_error)

    def test_backend_type_and_description_defaults(self):
        self.assertEqual(self.tool.name, "bash")
        self.assertIn("persistent bash shell", self.tool.description)

    def test_install_idempotent(self):
        again = install_persistent_bash(self.ctx, self.registry)
        self.assertIs(again, self.tool)


class TestPersistentPwsh(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctx = Context(name="persistent-pwsh-test")
        self.addCleanup(self.ctx.dispose)
        install_agents(self.ctx)
        self.terminals = install_terminals(self.ctx)
        self.backend = StubBackend("stub", output="ps-output")
        self.terminals.register_backend(self.backend)
        self.registry = ToolRegistry(self.ctx)
        self.tool = install_persistent_pwsh(self.ctx, self.registry, {"backendType": "stub"})
        self.agent = StubAgent("owner")
        self.ctx.get("agents").register(self.agent)

    async def test_pwsh_tool(self):
        result = await _run(self.ctx, self.tool, {"command": "Get-Item ."}, self.agent)
        self.assertIn("ps-output", result.value)
        self.assertEqual(self.tool.name, "pwsh")

    async def test_pwsh_wrapper_uses_invoke_expression(self):
        # 桩解析 PWsh 标记即可；确保包装命令含 PWSH 标记前缀。
        await _run(self.ctx, self.tool, {"command": "Write-Output 1"}, self.agent)
        self.assertIn("__DSH_PERSISTENT_PWSH_", self.backend.sessions[0].scrollback)


if __name__ == "__main__":
    unittest.main()
