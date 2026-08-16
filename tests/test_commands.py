"""议题 8 验收：轻量命令服务（command/run、command/done 配对与生命周期事件）。

覆盖：parse_command 文法、注册/分发、事件配对、未知命令、handler 抛错结算、
重复注册冲突、命令服务可选性。
"""

import unittest
from types import SimpleNamespace

from miniharness.commands import (
    CommandRegistry,
    install_commands,
    parse_command,
    route_command,
)
from miniharness.core.scope import Context
from miniharness.core.session import KNOWN_TYPES, Session


def _agent():
    return SimpleNamespace(session=Session("c1"))


class ParseCommandTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_command("hi there"), None)

    def test_bare_slash(self):
        self.assertEqual(parse_command("/"), None)

    def test_slash_then_blank(self):
        self.assertEqual(parse_command("/ "), None)

    def test_name_lowercased(self):
        self.assertEqual(parse_command("/Plan off"), ("plan", " off"))

    def test_args_verbatim(self):
        self.assertEqual(parse_command("/plan  hello   "), ("plan", "  hello   "))

    def test_name_with_dash(self):
        self.assertEqual(parse_command("/my-cmd x"), ("my-cmd", " x"))

    def test_name_must_start_alnum(self):
        self.assertEqual(parse_command("/-x"), None)


class CommandRegistryTest(unittest.TestCase):
    def _make(self):
        ctx = Context(name="commands")
        registry = install_commands(ctx)
        return ctx, registry

    def test_known_types(self):
        self.assertIn("command/run", KNOWN_TYPES)
        self.assertIn("command/done", KNOWN_TYPES)

    def test_dispatch_events_pairing(self):
        _, registry = self._make()
        agent = _agent()

        def handler(agent, raw):
            return {"kind": "success", "text": f"got:{raw}"}

        registry.register("echo", "echo back", handler)
        result = registry.dispatch(agent, "/echo hi")
        self.assertEqual(result, {"kind": "success", "text": "got: hi"})

        events = agent.session.events
        runs = [e for e in events if e["type"] == "command/run"]
        dones = [e for e in events if e["type"] == "command/done"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(dones), 1)
        self.assertEqual(runs[0]["data"]["name"], "echo")
        self.assertEqual(runs[0]["data"]["args"], " hi")
        self.assertEqual(runs[0]["data"]["commandId"], dones[0]["data"]["commandId"])
        self.assertEqual(dones[0]["data"]["kind"], "success")
        self.assertEqual(dones[0]["data"]["text"], "got: hi")

    def test_handler_error_settles_as_error(self):
        _, registry = self._make()
        agent = _agent()

        def handler(agent, raw):
            raise ValueError("boom")

        registry.register("fail", "fails", handler)
        result = registry.dispatch(agent, "/fail")
        self.assertEqual(result["kind"], "error")
        self.assertEqual(result["text"], "boom")
        done = [e for e in agent.session.events if e["type"] == "command/done"][-1]
        self.assertEqual(done["data"]["kind"], "error")

    def test_unknown_command_is_plain_text(self):
        _, registry = self._make()
        agent = _agent()
        self.assertIsNone(registry.dispatch(agent, "/nope x"))
        self.assertNotIn("command/run", [e["type"] for e in agent.session.events])

    def test_non_command_is_none(self):
        _, registry = self._make()
        self.assertIsNone(registry.dispatch(_agent(), "hello"))

    def test_duplicate_registration_fails(self):
        _, registry = self._make()
        registry.register("dup", "a", lambda a, r: None)
        with self.assertRaises(RuntimeError):
            registry.register("dup", "b", lambda a, r: None)

    def test_disposer(self):
        _, registry = self._make()
        agent = _agent()
        disposer = registry.register("x", "x", lambda a, r: None)
        self.assertIn("x", registry.names())
        disposer()
        self.assertNotIn("x", registry.names())

    def test_string_result_normalized(self):
        _, registry = self._make()
        registry.register("s", "s", lambda a, r: "ok")
        result = registry.dispatch(_agent(), "/s")
        self.assertEqual(result, {"kind": "success", "text": "ok"})

    def test_route_command_without_service(self):
        ctx = Context(name="bare")
        self.assertIsNone(route_command("/plan off", _agent(), ctx))

    def test_route_command_hits(self):
        ctx, registry = self._make()
        registry.register("plan", "plan", lambda a, r: {"kind": "success", "text": "ok"})
        self.assertEqual(route_command("/plan off", _agent(), ctx), "ok")


if __name__ == "__main__":
    unittest.main()
