"""第 11 章测试：运行时自我修改 —— 动态插件生命周期。"""
import unittest

from miniharness.bus import Context
from miniharness.dynamic import DynamicPluginRegistry


class TestDynamicPlugin(unittest.TestCase):
    def setUp(self):
        self.host = Context(name="host")
        self.registry = DynamicPluginRegistry(self.host)

    def test_define_mints_pkg_and_run_activates(self):
        pkg = self.registry.define("new", "demo 插件",
                                   provides=["dynDoubler"],
                                   apply=lambda ctx: ctx.provide(
                                       "dynDoubler", lambda x: x * 2))
        self.assertEqual(pkg, "pkg-1")
        self.assertIn("pkg-1", self.registry.list())
        # define 不生效
        with self.assertRaises(KeyError):
            self.registry.invoke("run-1", "dynDoubler", 21)
        run = self.registry.run("pkg-1")
        self.assertEqual(run["runId"], "run-1")
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["pkgId"], "pkg-1")
        self.assertEqual(self.registry.invoke("run-1", "dynDoubler", 21), 42)

    def test_define_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.define("magic", "source")

    def test_run_unknown_pkg_fails_loud(self):
        with self.assertRaises(KeyError):
            self.registry.run("pkg-999")

    def test_run_twice_rejected(self):
        self.registry.define("new", "s", provides=["svc"], apply=lambda ctx: None)
        self.registry.run("pkg-1")
        with self.assertRaises(RuntimeError):
            self.registry.run("pkg-1")

    def test_stop_disposes_scope_and_service_vanishes(self):
        self.registry.define("new", "s", provides=["svc"],
                             apply=lambda ctx: ctx.provide("svc", lambda: "alive"))
        run = self.registry.run("pkg-1")
        self.assertEqual(self.registry.invoke(run["runId"], "svc"), "alive")
        self.registry.stop(run["runId"])
        with self.assertRaises(KeyError):
            self.registry.invoke(run["runId"], "svc")

    def test_undefine_requires_stop_first(self):
        self.registry.define("new", "s", provides=["svc"], apply=lambda ctx: None)
        self.registry.run("pkg-1")
        with self.assertRaises(RuntimeError):
            self.registry.undefine("pkg-1")
        self.registry.stop("run-1")
        self.registry.undefine("pkg-1")
        self.assertNotIn("pkg-1", self.registry.list())
        with self.assertRaises(KeyError):
            self.registry.run("pkg-1")

    def test_process_global_conflict_rejected(self):
        self.host.provide("session-persistence", object())
        self.registry.define("new", "s", provides=["session-persistence"],
                             apply=lambda ctx: None)
        with self.assertRaises(RuntimeError):
            self.registry.run("pkg-1")
        # host 服务未被覆盖
        self.assertIsNotNone(self.host.inject("session-persistence"))

    def test_new_registry_does_not_restore(self):
        # "重启"：新 registry 即全新进程，一切不恢复
        self.registry.define("new", "s", provides=["svc"], apply=lambda ctx: None)
        self.registry.run("pkg-1")
        fresh = DynamicPluginRegistry(Context(name="new-host"))
        self.assertEqual(fresh.list(), [])
        with self.assertRaises(KeyError):
            fresh.run("pkg-1")

    def test_inspect_self_snapshot(self):
        self.registry.define("new", "插件A", provides=["a"], apply=lambda ctx: None)
        self.registry.define("new", "插件B", provides=["b"], apply=lambda ctx: None)
        self.registry.run("pkg-1")
        snap = self.registry.inspect_self()
        self.assertEqual([d["pkgId"] for d in snap["defs"]], ["pkg-1", "pkg-2"])
        self.assertEqual(snap["runs"], ["run-1"])
        self.assertTrue(snap["defs"][0]["running"])
        self.assertFalse(snap["defs"][1]["running"])

    def test_query_details(self):
        self.registry.define("new", "演示插件", provides=["x"], apply=lambda ctx: None)
        q = self.registry.query("pkg-1")
        self.assertEqual(q["source"], "演示插件")
        self.assertEqual(q["provides"], ["x"])
        self.assertFalse(q["running"])


if __name__ == "__main__":
    unittest.main()