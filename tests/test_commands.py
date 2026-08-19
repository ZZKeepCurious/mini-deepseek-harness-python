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

    def test_name_must_start_lowercase(self):
        # 对齐上游 parseCommand：首字符必须小写字母（不做大小写转换）
        self.assertEqual(parse_command("/Plan off"), None)
        self.assertEqual(parse_command("/1x y"), None)
        self.assertEqual(parse_command("/_x y"), None)
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

    def test_string_result_fails_loud(self):
        # 对齐上游 normalizeResult：非 CommandResult 返回 fail loud（注册表边界）
        _, registry = self._make()
        registry.register("s", "s", lambda a, r: "ok")
        with self.assertRaises(TypeError):
            registry.dispatch(_agent(), "/s")

    def test_route_command_without_service(self):
        ctx = Context(name="bare")
        self.assertIsNone(route_command("/plan off", _agent(), ctx))

    def test_route_command_hits(self):
        ctx, registry = self._make()
        registry.register("plan", "plan", lambda a, r: {"kind": "success", "text": "ok"})
        self.assertEqual(route_command("/plan off", _agent(), ctx), "ok")

    def test_command_id_format_monotonic(self):
        _, registry = self._make()
        registry.register("a", "a", lambda a, r: {"kind": "success"})
        agent = _agent()
        registry.dispatch(agent, "/a")
        registry.dispatch(agent, "/a")
        ids = [e["data"]["commandId"] for e in agent.session.events
               if e["type"] == "command/run"]
        # 上游 mintCommandId：cmd-<8位实例前缀>-<单调序号>
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0], f"cmd-{ids[0][4:12]}-1")
        self.assertEqual(ids[1], f"cmd-{ids[1][4:12]}-2")
        self.assertEqual(ids[0][4:12], ids[1][4:12])
        self.assertRegex(ids[0], r"^cmd-[0-9a-f]{8}-\d+$")
        # 同一 instance 前缀跨多次 dispatch 单调不重复
        self.assertNotEqual(ids[0], ids[1])

    def test_register_name_validation(self):
        _, registry = self._make()
        for bad in ("Plan", "1abc", "_x", "-x", "a b"):
            with self.assertRaises(TypeError):
                registry.register(bad, "d", lambda a, r: None)
        registry.register("ok_name-1", "d", lambda a, r: None)

    def test_register_description_validation(self):
        _, registry = self._make()
        with self.assertRaises(TypeError):
            registry.register("x", "", lambda a, r: None)
        with self.assertRaises(TypeError):
            registry.register("x", "   ", lambda a, r: None)
        with self.assertRaises(TypeError):
            registry.register("x", "ok", None)

    def test_record_input_false_omits_args(self):
        _, registry = self._make()
        registry.register("quiet", "q", lambda a, r: {"kind": "success"}, record_input=False)
        agent = _agent()
        registry.dispatch(agent, "/quiet secret")
        run = [e for e in agent.session.events if e["type"] == "command/run"][0]
        self.assertNotIn("args", run["data"])

    def test_success_source_event_seq_recorded(self):
        _, registry = self._make()
        registry.register("src", "s", lambda a, r: {"kind": "success", "text": "ok",
                                                    "sourceEventSeq": 7})
        agent = _agent()
        registry.dispatch(agent, "/src")
        done = [e for e in agent.session.events if e["type"] == "command/done"][0]
        self.assertEqual(done["data"]["sourceEventSeq"], 7)

    def test_commands_change_notification(self):
        ctx, registry = self._make()
        events = []
        ctx.on("commands/change", lambda payload: events.append(True))
        disposer = registry.register("c", "c", lambda a, r: {"kind": "success"})
        self.assertEqual(len(events), 1)
        disposer()
        self.assertEqual(len(events), 2)

    def test_bad_result_kind_fails_loud(self):
        _, registry = self._make()
        registry.register("badk", "b", lambda a, r: {"kind": "nope"})
        with self.assertRaises(TypeError):
            registry.dispatch(_agent(), "/badk")

    def test_error_result_requires_text(self):
        _, registry = self._make()
        registry.register("bade", "b", lambda a, r: {"kind": "error"})
        with self.assertRaises(TypeError):
            registry.dispatch(_agent(), "/bade")


if __name__ == "__main__":
    unittest.main()
