"""tool-terminal 六工具契约面（P3）。

* render 精确字符串各转移植自包内 render.spec.ts（render 函数 = 纯函数直读）。
* 六工具经 run_pipeline_async 全程运行（schema 校验 / finalize_content 收口 /
  前台上库后台作业同管线）；owner 用 StubAgent 精确实例登记 agents 服务。
* 后台 pty-send 用 jobs 家族：install_jobs + register_job_tools + 六工具同注册
  面装配，验收 job_output / job_kill / job_list 的形状与"增量读 + 终态状态行"。
* StubTtySession 镜像 test_terminal_service.py 的 StubSession：settle 签名
  (wait_reason, status, truncated)，append → consume 增量。
"""

import threading
import unittest
import asyncio

from miniharness.core import Context
from miniharness.core.agents import install_agents
from miniharness.core.tools import ToolExec, ToolRegistry, run_pipeline_async
from miniharness.jobs import install_jobs, register_job_tools
from miniharness.terminal.operation import LocalSendOperation
from miniharness.terminal.service import install_terminals
from miniharness.tool_terminal import (
    DEFAULT_MAX_RESULT_BYTES,
    INJECT,
    MIN_MAX_RESULT_BYTES,
    PLUGIN_NAME,
    TOOL_PTY_ORDER,
    TOOL_PTY_SECTION,
    resolve_config,
)
from miniharness.tool_terminal.render import (
    TRUNCATED,
    bound_terminal_text,
    render_list,
    render_read,
    render_send,
    render_send_read,
    render_spawn,
)
from miniharness.tool_terminal.tools import register_terminal_tools


def _run(ctx, tool, args, agent=None, signal=None) -> dict:
    exec_ = ToolExec(agent=agent, signal=signal or threading.Event()) if agent is not None else ToolExec(signal=threading.Event())
    return asyncio.run(run_pipeline_async(ctx, tool, args, exec_))


def _tool_by_name(ctx, name):
    tool = ctx.get("tools").resolve(name)
    if tool is None:
        raise AssertionError(f"missing tool {name}")
    return tool


class StubTtySession:
    motd = "stub ready"
    pid = 55

    def __init__(self, auto_settle=False, reject_send=False, appends=("viewport: ",), motd="stub ready"):
        self.motd = motd
        self.status_value = {"kind": "running"}
        self.operation = None
        self._settle_now = auto_settle
        self.reject_send = reject_send
        self.appends = list(appends)
        self.closed = []

    def start_send(self, request):
        status_value = self.status_value
        operation = LocalSendOperation(1024, 0)
        operation.set_on_cancel(lambda: operation.settle("stdin_read", status_value, False))
        for chunk in self.appends:
            operation.append(chunk)
        if request.get("text"):
            operation.append(request["text"])
        if self.reject_send:
            operation.fail(RuntimeError("send failed"))
        elif self._settle_now:
            operation.settle("stdin_read", status_value, False)
        self.operation = operation
        return operation

    def read(self, request):
        return {
            "text": f"{request.get('offset', 0) or 0}:{request.get('count', 0) or 0}",
            "totalLines": 1, "lineBegin": 0, "lineEnd": 1, "truncated": False,
        }

    def signal(self, signal):
        return {"delivered": True, "targetPgid": 12 if signal == "SIGINT" else 13}

    def status(self):
        return self.status_value

    def close(self, reason):
        self.closed.append(reason)
        self.status_value = {"kind": "exited", "exitCode": 0, "signal": None}
        if self.operation is not None and not self.operation.settled:
            self.operation.cancel()


class StubBackend:
    def __init__(self, type_, sessions):
        self.type = type_
        self.sessions = sessions

    def spawn(self, spec):
        session = StubTtySession()
        self.sessions.append(session)
        return session


class StubAgent:
    def __init__(self, id_, session_id=None):
        self.id = id_
        self.session = type("Session", (), {"session_id": session_id or id_, "meta": {}})()
        self.ctx = Context(name=f"agent-{id_}")
        self._carrier = None
        self.status = "idle"
        self.inbox = []
        self.wakes = []

    def followup(self, text, source=None):
        self.wakes.append(text)

    def inject(self, text, source=None):
        self.inbox.append(text)


class _Harness:
    """安装 agents + terminals + stub backend + 工具注册面的共享夹具。"""

    def __init__(self, config=None, with_jobs=False, settle_now=True, type_="stub", motd="stub prompt"):
        self.ctx = Context(name="tool-terminal-test")
        install_agents(self.ctx)
        self.terminals = install_terminals(self.ctx)
        self.sessions = []
        self.auto = settle_now
        self.motd = motd
        self.terminals.register_backend(
            _BackendWithPolicy(type_, self.sessions, settle_now=self.auto, motd=motd))
        self.registry = ToolRegistry(self.ctx)
        if with_jobs:
            install_jobs(self.ctx)
            register_job_tools(self.registry, self.ctx.get("jobs"))
        register_terminal_tools(self.registry, self.terminals, lambda: self.ctx.get("jobs"), config)
        self.agent = StubAgent("owner")
        self.ctx.get("agents").register(self.agent)

    def run(self, name, args, agent=None, signal=None):
        return _run(self.ctx, _tool_by_name(self.ctx, name), args,
                    agent=agent if agent is not None else self.agent, signal=signal)


class _BackendWithPolicy(StubBackend):
    def __init__(self, type_, sessions, settle_now=True, motd="stub prompt"):
        super().__init__(type_, sessions)
        self.settle_now = settle_now
        self.motd = motd

    def spawn(self, spec):
        session = StubTtySession(auto_settle=self.settle_now, motd=self.motd)
        self.sessions.append(session)
        return session


class TestRender(unittest.TestCase):
    def test_spawn_named_with_motd(self):
        self.assertEqual(
            render_spawn({"sessionId": "pty-1", "name": "main", "type": "shell",
                          "status": {"kind": "running"}, "motd": "stub prompt"}, 2**20),
            "started terminal session pty-1 (main) [type: shell]\nstub prompt",
        )

    def test_spawn_unnamed_no_motd(self):
        self.assertEqual(
            render_spawn({"sessionId": "pty-1", "type": "shell",
                          "status": {"kind": "running"}}, 2**20),
            "started terminal session pty-1 [type: shell]\n(no startup output)",
        )

    def test_spawn_bounds_keeps_prefix_and_marker(self):
        text = render_spawn({"sessionId": "pty-1", "type": "shell",
                             "status": {"kind": "running"}, "motd": "x" * 200}, 64)
        self.assertTrue(text.startswith("started terminal session pty-1 [type: shell]\n"))
        self.assertTrue(text.endswith(TRUNCATED))

    def test_send_running(self):
        self.assertEqual(
            render_send({"viewport": "hi", "waitReason": "stdin_read",
                         "sessionStatus": {"kind": "running"}, "truncated": False}, 2**20),
            "hi\n[wait: stdin_read]\n[session: running]",
        )

    def test_send_timeout_truncated(self):
        out = render_send({"viewport": "a" * 4000, "waitReason": "timeout",
                           "sessionStatus": {"kind": "running"}, "truncated": True}, 3072)
        self.assertLessEqual(len(out.encode("utf-8")), 3072)
        self.assertTrue(out.endswith("[session: running]\n[output truncated]"))

    def test_send_empty_viewport(self):
        self.assertEqual(
            render_send({"viewport": "", "waitReason": "stdin_read",
                         "sessionStatus": {"kind": "running"}, "truncated": False}, 2**20),
            "(no new output)\n[wait: stdin_read]\n[session: running]",
        )

    def test_send_exited_shapes(self):
        base = {"viewport": "done", "waitReason": "session_exit"}
        self.assertIn("exited code=null signal=null",
                      render_send({**base, "sessionStatus": {"kind": "exited", "exitCode": None, "signal": None},
                                   "truncated": False}, 2**20))
        self.assertIn("exited code=2 signal=null",
                      render_send({**base, "sessionStatus": {"kind": "exited", "exitCode": 2, "signal": None},
                                   "truncated": False}, 2**20))
        self.assertIn("exited code=null signal=SIGTERM",
                      render_send({**base, "sessionStatus": {"kind": "exited", "exitCode": None, "signal": "SIGTERM"},
                                   "truncated": False}, 2**20))

    def test_render_send_read(self):
        self.assertEqual(render_send_read({"delta": "", "truncated": True}), "[output truncated]")
        self.assertEqual(render_send_read({"delta": "x", "truncated": True}), "x\n[output truncated]")
        self.assertEqual(render_send_read({"delta": "x\n", "truncated": True}), "x\n[output truncated]")
        self.assertEqual(render_send_read({"delta": "x\n", "truncated": False}), "x\n")

    def test_read_empty_and_populated(self):
        self.assertEqual(
            render_read({"text": "", "totalLines": 0, "lineBegin": 0, "lineEnd": 0, "truncated": False}, 2**20),
            "(no retained output)\n[lines: 0-0 of 0]",
        )
        self.assertEqual(
            render_read({"text": "l1\nl2", "totalLines": 5, "lineBegin": 2, "lineEnd": 4, "truncated": False}, 2**20),
            "l1\nl2\n[lines: 2-4 of 5]",
        )

    def test_list_empty_and_rows(self):
        self.assertEqual(render_list([], 2**20), "(no terminal sessions)")
        rows = render_list([
            {"sessionId": "p1", "type": "stub", "status": {"kind": "running"}, "pid": 42},
            {"sessionId": "p2", "name": "main", "type": "shell",
             "status": {"kind": "exited", "exitCode": 0, "signal": None}, "pid": 43},
        ], 2**20)
        self.assertEqual(rows, "p1 [stub] running pid=42\np2 (main) [shell] exited code=0 signal=null pid=43")

    def test_bound_terminal_text(self):
        text = "a" * 200
        bounded = bound_terminal_text(text, 100)
        self.assertTrue(bounded.endswith(TRUNCATED))
        self.assertEqual(len(bounded.encode("utf-8")), 100)
        self.assertEqual(bound_terminal_text(text, 4), "ted]")
        self.assertEqual(bound_terminal_text("short", 100), "short")


class TestRegistryShape(unittest.TestCase):
    def test_six_tool_names_in_order(self):
        ctx = Context(name="shape")
        install_agents(ctx)
        terminals = install_terminals(ctx)
        terminals.register_backend(StubBackend("stub", []))
        registry = ToolRegistry(ctx)
        register_terminal_tools(registry, terminals, lambda: None, None)
        self.assertEqual(set(registry.names()),
                         {"terminal_open", "terminal_send", "terminal_read",
                          "terminal_signal", "terminal_close", "terminal_list"})

    def test_plugin_shape_and_constants(self):
        self.assertEqual(PLUGIN_NAME, "tool-terminal")
        self.assertEqual(INJECT, ("terminals", "tools", "systemPrompt"))
        self.assertEqual(TOOL_PTY_SECTION, "tool:pty")
        self.assertEqual(TOOL_PTY_ORDER, 1700)
        self.assertEqual(DEFAULT_MAX_RESULT_BYTES, 256 * 1024)
        self.assertEqual(MIN_MAX_RESULT_BYTES, 64)
        self.assertEqual(resolve_config(None),
                         {"enableRunInBackground": True, "maxResultBytes": 256 * 1024})
        with self.assertRaises(ValueError):
            resolve_config({"maxResultBytes": 32})
        with self.assertRaises(ValueError):
            resolve_config({"maxResultBytes": "big"})

    def test_background_config_schema_and_description(self):
        ctx = Context(name="shape")
        install_agents(ctx)
        terminals = install_terminals(ctx)
        terminals.register_backend(StubBackend("stub", []))
        registry = ToolRegistry(ctx)
        register_terminal_tools(registry, terminals, lambda: None, {"enableRunInBackground": False})
        send = _tool_by_name(ctx, "terminal_send")
        self.assertNotIn("run_in_background", send.parameters["properties"])
        self.assertTrue(send.description.endswith("timeout, or session exit."))

    def test_present_call_titles(self):
        ctx = Context(name="shape")
        install_agents(ctx)
        terminals = install_terminals(ctx)
        terminals.register_backend(StubBackend("stub", []))
        registry = ToolRegistry(ctx)
        register_terminal_tools(registry, terminals, lambda: None, None)
        self.assertEqual(_tool_by_name(ctx, "terminal_open").present_call({"type": "shell"}),
                         {"card": "generic", "title": "Open terminal shell", "kind": "execute"})
        self.assertEqual(_tool_by_name(ctx, "terminal_list").present_call({}),
                         {"card": "generic", "title": "List terminal sessions", "kind": "read"})
        self.assertEqual(
            _tool_by_name(ctx, "terminal_send").present_call(
                {"sessionId": "pty-1", "text": "hi", "run_in_background": True}),
            {"card": "generic", "title": "Send to terminal pty-1 in background",
             "kind": "execute", "rawInput": "hi"})
        self.assertEqual(
            _tool_by_name(ctx, "terminal_send").present_call({"sessionId": "pty-1", "text": "go"}),
            {"card": "terminal", "title": "go", "description": "Terminal pty-1"})


class TestToolsForeground(unittest.TestCase):
    def setUp(self):
        self.h = _Harness(config=None, with_jobs=False)

    def test_open_list_read_signal_close_lifecycle(self):
        opened = self.h.run("terminal_open", {"type": "stub", "name": "main"})
        self.assertTrue(opened.ok, opened.error)
        value = opened.value
        self.assertEqual(value["type"], "stub")
        self.assertEqual(value["name"], "main")
        self.assertEqual(value["motd"], "stub prompt")
        self.assertTrue(value["sessionId"].startswith("pty-"))
        sid = value["sessionId"]

        listing = self.h.run("terminal_list", {})
        self.assertEqual(len(listing.value), 1)
        self.assertEqual(listing.value[0]["sessionId"], sid)
        self.assertIn(f"{sid} (main) [stub] running pid=55", listing.content[0]["text"])

        sig = self.h.run("terminal_signal", {"sessionId": sid, "signal": "SIGINT"})
        self.assertEqual(sig.value, {"delivered": True, "targetPgid": 12})
        self.assertEqual(sig.content[0]["text"],
                         "delivered SIGINT to foreground process group 12")

        page = self.h.run("terminal_read", {"sessionId": sid, "offset": 2, "count": 5})
        self.assertEqual(page.value["text"], "2:5")
        self.assertEqual(page.content[0]["text"], "2:5\n[lines: 0-1 of 1]")

        closed = self.h.run("terminal_close", {"sessionId": sid})
        self.assertEqual(closed.value, {"sessionId": sid, "outcome": "closed"})
        self.assertEqual(closed.content[0]["text"], f"closed terminal session {sid}")
        self.assertEqual(self.h.run("terminal_list", {}).value, [])

    def test_open_rejects_empty_type(self):
        result = self.h.run("terminal_open", {"type": ""})
        self.assertTrue(result.is_error)
        self.assertEqual(result.error, "Error: type must be a non-empty string")

    def test_tools_require_initiating_agent(self):
        tool = _tool_by_name(self.h.ctx, "terminal_list")
        result = _run(self.h.ctx, tool, {}, agent=None)
        self.assertTrue(result.is_error)
        self.assertEqual(result.error, "Error: terminal tools require an initiating agent")

    def test_send_foreground_running(self):
        sid = self.h.run("terminal_open", {"type": "stub"}).value["sessionId"]
        sent = self.h.run("terminal_send", {"sessionId": sid, "text": "hi"})
        self.assertTrue(sent.ok, sent.error)
        self.assertEqual(sent.value["kind"], "foreground")
        self.assertEqual(sent.value["sessionStatus"], {"kind": "running"})
        self.assertEqual(sent.value["waitReason"], "stdin_read")
        self.assertEqual(sent.content[0]["text"],
                         "viewport: hi\n[wait: stdin_read]\n[session: running]")

    def test_send_foreground_aborted(self):
        sid = self.h.run("terminal_open", {"type": "stub"}).value["sessionId"]
        signal = threading.Event()
        signal.set()
        result = self.h.run("terminal_send", {"sessionId": sid, "text": "hi"}, signal=signal)
        self.assertTrue(result.is_error)
        self.assertEqual(result.error, "Error: terminal send aborted")

    def test_send_background_requires_jobs_service(self):
        sid = self.h.run("terminal_open", {"type": "stub"}).value["sessionId"]
        result = self.h.run("terminal_send",
                            {"sessionId": sid, "text": "hi", "run_in_background": True})
        self.assertTrue(result.is_error)
        self.assertIn("background terminal sends require", result.error)

    def test_background_disabled_by_config(self):
        h = _Harness(config={"enableRunInBackground": False}, with_jobs=False)
        sid = h.run("terminal_open", {"type": "stub"}).value["sessionId"]
        result = h.run("terminal_send",
                       {"sessionId": sid, "text": "hi", "run_in_background": True})
        self.assertTrue(result.is_error)
        self.assertIn("background terminal sends are disabled", result.error)

    def test_max_result_bytes_bounds_open(self):
        h = _Harness(config={"maxResultBytes": 64}, with_jobs=False, settle_now=True, motd="x" * 200)
        result = h.run("terminal_open", {"type": "stub"})
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.content[0]["text"].endswith(TRUNCATED))
        self.assertLessEqual(len(result.content[0]["text"].encode("utf-8")), 64)

    def test_present_result_foreground(self):
        send = _tool_by_name(self.h.ctx, "terminal_send")
        self.assertIsNone(send.present_result({}, {"kind": "background", "jobId": "pty-send-1"}))
        self.assertIsNone(send.present_result({}, {"kind": "foreground", "content": [{"type": "text", "text": "o"}], "error": "boom"}))
        card = send.present_result({}, {"kind": "foreground",
                                        "content": [{"type": "text", "text": "out"}]})
        self.assertEqual(card, {"card": "terminal", "output": "out"})


class TestToolsBackground(unittest.TestCase):
    def setUp(self):
        self.h = _Harness(config=None, with_jobs=True, settle_now=True)

    def test_completed_job_output_and_list(self):
        sid = self.h.run("terminal_open", {"type": "stub"}).value["sessionId"]
        sent = self.h.run("terminal_send",
                          {"sessionId": sid, "text": "hi", "run_in_background": True})
        self.assertTrue(sent.ok, sent.error)
        self.assertEqual(sent.value, {"kind": "background", "jobId": "pty-send-1"})
        self.assertEqual(sent.content[0]["text"], "started background job pty-send-1")

        out = self.h.run("job_output", {"job_id": "pty-send-1", "wait": True})
        self.assertEqual(out.content[0]["text"],
                         "viewport: hi\n[status: completed, wait: stdin_read]")
        self.assertEqual(out.value["job"]["label"], "pty-1: hi")

        listing = self.h.run("job_list", {})
        self.assertEqual(len(listing.value), 1)
        self.assertEqual(listing.value[0]["kind"], "pty-send")

    def test_killed_job(self):
        sid = self.h.run("terminal_open", {"type": "stub"}).value["sessionId"]
        self.h.sessions[-1]._settle_now = False
        self.h.run("terminal_send", {"sessionId": sid, "text": "hi", "run_in_background": True})
        kill = self.h.run("job_kill", {"job_id": "pty-send-1"})
        self.assertEqual(kill.value["outcome"], "cancellation-requested")
        out = self.h.run("job_output", {"job_id": "pty-send-1", "wait": True})
        self.assertEqual(out.content[0]["text"],
                         "viewport: hi\n[status: killed, wait: stdin_read]")

    def test_failed_job(self):
        sid = self.h.run("terminal_open", {"type": "stub"}).value["sessionId"]
        self.h.sessions[-1].reject_send = True
        self.h.run("terminal_send", {"sessionId": sid, "text": "hi", "run_in_background": True})
        out = self.h.run("job_output", {"job_id": "pty-send-1", "wait": True})
        self.assertEqual(out.content[0]["text"],
                         "viewport: hi\n[status: failed, send failed]")

    def test_incremental_read_reported(self):
        sid = self.h.run("terminal_open", {"type": "stub"}).value["sessionId"]
        self.h.run("terminal_send", {"sessionId": sid, "text": "hi", "run_in_background": True})
        self.h.run("job_output", {"job_id": "pty-send-1", "wait": True})
        second = self.h.run("job_output", {"job_id": "pty-send-1"})
        self.assertEqual(second.content[0]["text"],
                         "(no new output)\n[status: completed, wait: stdin_read]")
        self.assertEqual(second.value["job"]["status"], "completed")


class TestInstallToolTerminal(unittest.TestCase):
    def test_idempotent_and_section(self):
        from miniharness.tool_terminal import install_tool_terminal
        ctx = Context(name="install")
        install_agents(ctx)
        first = install_tool_terminal(ctx)
        second = install_tool_terminal(ctx)
        self.assertIs(first, second)  # 幂等复用
        self.assertEqual(set(ctx.get("tools").names()),
                         {"terminal_open", "terminal_send", "terminal_read",
                          "terminal_signal", "terminal_close", "terminal_list"})
        prompt = ctx.get("systemPrompt")
        pty = [s for s in prompt.render({}) if s["name"] == TOOL_PTY_SECTION]
        self.assertEqual(len(pty), 1)
        self.assertEqual(prompt._sections[0]["order"], TOOL_PTY_ORDER)
        self.assertIn("Use a terminal session only when work needs persistent terminal state",
                      pty[0]["text"])


if __name__ == "__main__":
    unittest.main()