"""TerminalSessionService 确定性契约面。

对应 terminal/tests/service.spec.ts 中不依赖异步门控 / 真实 shell 载体
（LocalPtySession/Timeout/PassThrough）的契约，逐条移植；涉及「spawn 与 owner
等并发 / agent 失去 live 即触发清理」等异步竞态面属 P2 会话嵌套集成，另行补。
错误行为用 TerminalError.code 断言。
"""

import unittest

from miniharness.core import Context
from miniharness.core.agents import install_agents
from miniharness.terminal.operation import LocalSendOperation
from miniharness.terminal.service import install_terminals
from miniharness.terminal.types import TerminalError


class StubSession:
    motd = "stub ready"
    pid = 123

    def __init__(self):
        self.closed = []
        self.status_value = {"kind": "running"}
        self.operation = None
        self.reject_send = False
        self.reject_close = False

    def start_send(self, request):
        status_value = self.status_value

        def on_cancel():
            operation.settle("stdin_read", status_value, False)

        operation = LocalSendOperation(1024, 0, on_cancel)
        operation.append("delta")
        if self.reject_send:
            operation.fail(RuntimeError("send failed"))
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
        if self.reject_close:
            raise RuntimeError("close failed")
        self.status_value = {"kind": "exited", "exitCode": 0, "signal": None}
        if self.operation is not None and not self.operation.settled:
            self.operation.cancel()


class StubBackend:
    def __init__(self, type_, sessions):
        self.type = type_
        self.sessions = sessions

    def spawn(self, spec):
        session = StubSession()
        self.sessions.append(session)
        return session


class StubOwner:
    def __init__(self, id_, session_id):
        self.id = id_
        self.session = type("Session", (), {"session_id": session_id})()
        self.ctx = Context(name="stub-owner-" + id_)
        self._carrier = None


class TestTerminalSessionService(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        install_agents(self.ctx)
        self.service = install_terminals(self.ctx)
        self.sessions = []
        self.owner = None
        self.foreign = None

    def _register_owners(self):
        self.owner = StubOwner("owner", "owner")
        self.foreign = StubOwner("foreign", "foreign")
        self.ctx.get("agents").register(self.owner)
        self.ctx.get("agents").register(self.foreign)
        self.service.register_backend(StubBackend("stub", self.sessions))

    def _spawn(self, type_="stub", name="main", owner_foreign=False):
        owner = self.foreign if owner_foreign else self.owner
        return self.service.spawn(owner, {"type": type_, "name": name, "cwd": "/tmp"})

    def test_rejects_empty_and_duplicate_backend_types(self):
        with self.assertRaisesRegex(RuntimeError, "must be non-empty"):
            self.service.register_backend(StubBackend("", []))
        self.assertEqual(list(self.service.list_backends()), [])
        self.service.register_backend(StubBackend("stub", []))
        self.assertEqual(list(self.service.list_backends()), ["stub"])
        with self.assertRaises(TerminalError) as raised:
            self.service.register_backend(StubBackend("stub", []))
        self.assertEqual(raised.exception.code, "DUPLICATE_BACKEND")

    def test_dispose_backend_removes_type(self):
        self.service.register_backend(StubBackend("stub", []))
        dispose = self.service.register_backend(StubBackend("extra", []))
        self.assertEqual(list(self.service.list_backends()), ["stub", "extra"])
        dispose()
        self.assertEqual(list(self.service.list_backends()), ["stub"])
        self.service.register_backend(StubBackend("extra", []))
        self.assertEqual(list(self.service.list_backends()), ["stub", "extra"])

    def test_publishes_only_after_spawn_and_fences_every_operation(self):
        self._register_owners()
        created = self._spawn()
        self.assertEqual(created["name"], "main")
        self.assertEqual(created["type"], "stub")
        self.assertEqual(created["motd"], "stub ready")
        self.assertEqual(created["pid"], 123)
        self.assertEqual(created["status"], {"kind": "running"})
        self.assertTrue(created["sessionId"].startswith("pty-"))
        self.assertTrue(self.service.has_owner_activity(self.owner))
        self.assertEqual(len(self.service.list(self.owner)), 1)
        self.assertEqual(len(self.service.list(self.foreign)), 0)
        self.assertEqual(self.service.list(self.owner)[0]["sessionId"],
                         created["sessionId"])

    def test_read_delegates_to_session(self):
        self._register_owners()
        created = self._spawn()
        result = self.service.read(self.owner, created["sessionId"], {})
        self.assertEqual(result["text"], "0:0")
        with self.assertRaises(TerminalError) as raised:
            self.service.read(self.foreign, created["sessionId"], {})
        self.assertEqual(raised.exception.code, "FOREIGN_SESSION")

    def test_signal_delegates(self):
        self._register_owners()
        created = self._spawn()
        self.assertEqual(self.service.signal(self.owner, created["sessionId"], "SIGINT")
                         ["targetPgid"], 12)
        with self.assertRaises(TerminalError) as raised:
            self.service.signal(self.foreign, created["sessionId"], "SIGINT")
        self.assertEqual(raised.exception.code, "FOREIGN_SESSION")

    def test_rejects_spawns_for_missing_backend_and_invalid_name(self):
        self._register_owners()
        with self.assertRaises(TerminalError) as raised:
            self._spawn(type_="missing")
        self.assertEqual(raised.exception.code, "NO_BACKEND")
        with self.assertRaisesRegex(RuntimeError, "PTY session name must be non-empty"):
            self._spawn(name="")

    def test_rejects_spawn_before_owner_registered(self):
        owner = StubOwner("owner", "owner")
        self.service.register_backend(StubBackend("stub", []))
        with self.assertRaises(TerminalError) as raised:
            self.service.spawn(owner, {"type": "stub", "name": "main", "cwd": "/tmp"})
        self.assertEqual(raised.exception.code, "OWNER_NOT_LIVE")

    def test_send_is_single_flight_and_owner_fenced(self):
        self._register_owners()
        created = self._spawn()
        operation = self.service.start_send(self.owner, created["sessionId"],
                                            {"text": "echo hi", "submit": True})
        with self.assertRaises(TerminalError) as raised:
            self.service.start_send(self.owner, created["sessionId"], {"text": "again"})
        self.assertEqual(raised.exception.code, "SEND_ACTIVE")
        with self.assertRaises(TerminalError) as raised:
            self.service.start_send(self.foreign, created["sessionId"], {"text": "hi"})
        self.assertEqual(raised.exception.code, "FOREIGN_SESSION")
        self.assertEqual(operation.read_output(), {"delta": "delta", "truncated": False})
        self.assertTrue(operation.cancel())
        self.assertEqual(operation.done["waitReason"], "stdin_read")
        self.assertEqual(operation.done["sessionStatus"], {"kind": "running"})
        next_op = self.service.start_send(self.owner, created["sessionId"],
                                          {"text": "echo again", "submit": True})
        self.assertTrue(next_op.cancel())
        self.assertEqual(next_op.done["waitReason"], "stdin_read")

    def test_send_failure_is_reported(self):
        self._register_owners()
        created = self._spawn()
        self.sessions[0].reject_send = True
        operation = self.service.start_send(self.owner, created["sessionId"],
                                            {"text": "echo", "submit": True})
        with self.assertRaisesRegex(RuntimeError, "send failed"):
            operation.done

    def test_spawn_failure_before_publication(self):
        self.owner = StubOwner("owner", "owner")
        self.ctx.get("agents").register(self.owner)

        class FailingBackend:
            type = "stub"

            def spawn(self, spec):
                raise RuntimeError("provider failed")
        self.service.register_backend(FailingBackend())
        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            self.service.spawn(self.owner, {"type": "stub", "name": "main", "cwd": "/tmp"})
        self.assertFalse(self.service.has_owner_activity(self.owner))
        self.assertEqual(self.service.list(self.owner), [])

    def test_kill_closes_with_model_request_and_removes(self):
        self._register_owners()
        created = self._spawn()
        self.assertTrue(self.service.kill(self.owner, created["sessionId"], "model request"))
        self.assertEqual(self.sessions[0].closed, ["model request"])
        self.assertEqual(self.service.list(self.owner), [])
        with self.assertRaises(TerminalError) as raised:
            self.service.kill(self.owner, created["sessionId"], "model request")
        self.assertEqual(raised.exception.code, "NO_SESSION")

    def test_kill_reports_close_failure_without_dropping(self):
        self._register_owners()
        created = self._spawn()
        self.sessions[0].reject_close = True
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            self.service.kill(self.owner, created["sessionId"], "model request")
        self.assertEqual(len(self.service.list(self.owner)), 1)
        self.sessions[0].reject_close = False
        self.assertTrue(self.service.kill(self.owner, created["sessionId"], "retry"))
        self.assertEqual(self.sessions[0].closed, ["model request", "retry"])

    def test_spawn_rejects_duplicate_names_for_owner(self):
        self._register_owners()
        self._spawn(name="main")
        with self.assertRaises(TerminalError) as raised:
            self._spawn(name="main")
        self.assertEqual(raised.exception.code, "DUPLICATE_NAME")
        self._spawn(name="second")
        self.assertEqual(len(self.service.list(self.owner)), 2)

    def test_owner_disposal_closes_all_sessions(self):
        self._register_owners()
        self._spawn()
        self._spawn(name="second")
        self.assertEqual(len(self.service.list(self.owner)), 2)
        self.owner.ctx.dispose()
        self.assertEqual(self.sessions[0].closed, ["PTY owner disposed"])
        self.assertEqual(self.sessions[1].closed, ["PTY owner disposed"])
        self.assertEqual(self.service.list(self.owner), [])
        self.assertFalse(self.service.has_owner_activity(self.owner))

    def test_service_disposal_closes_all_and_seals(self):
        self._register_owners()
        self._spawn()
        self._spawn(name="second")
        killed_id = self.service.list(self.owner)[0]["sessionId"]
        self.service.kill(self.owner, killed_id, "model request")
        self.service._dispose_all()
        self.assertEqual(self.sessions[0].closed, ["model request"])
        self.assertEqual(self.sessions[1].closed, ["PTY service disposed"])
        self.assertEqual(self.service.list(self.owner), [])
        self.assertEqual(self.service.list_backends(), [])
        self.assertEqual(self.service._owner_cleanups, {})
        with self.assertRaises(TerminalError) as raised:
            self.service.spawn(self.owner, {"type": "stub", "name": "late"})
        self.assertEqual(raised.exception.code, "SERVICE_DISPOSING")

    def test_owner_context_cleanup_does_not_cross_service_scopes(self):
        self._register_owners()
        self._spawn()
        owner_ctx = self.owner.ctx
        owner_ctx.dispose()
        self.assertEqual(self.sessions[0].closed, ["PTY owner disposed"])
        # 独立 owner scope 不影响其他已注册 backend / service 契约
        self.assertEqual(list(self.service.list_backends()), ["stub"])


if __name__ == "__main__":
    unittest.main()