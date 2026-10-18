"""loader 活树 / 条目隔离 / 组运行时 补充覆盖。

对齐上游 vendor/loader/src/config/{tree,isolate,group}.ts。经 boot 构建真实
条目树，再用 loader 服务的 create/remove/update/resolve 驱动未覆盖分支。
"""
import tempfile
import unittest
from pathlib import Path

from miniharness.boot import boot
from miniharness.loader.isolate import GlobalRealm, LocalRealm, Realm


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


CONFIG = (
    "plugins:\n"
    "  - id: grp\n"
    "    name: cordis:group\n"
    "    group: true\n"
    "    config:\n"
    "      - id: inner\n"
    "        module: miniharness.example_plugins\n"
    "        config:\n"
    "          greeting: in\n"
    "          service_name: inner_svc\n"
    "  - id: solo\n"
    "    module: miniharness.example_plugins\n"
    "    config:\n"
    "      greeting: so\n"
    "      service_name: solo_svc\n"
)


class RealmTest(unittest.TestCase):
    def test_access_delete_and_size(self):
        realm = Realm()
        self.assertIsNone(realm.access("greeter"))
        self.assertIsNone(realm.access("greeter", create=False))
        label = realm.access("greeter", create=True)
        self.assertIsNotNone(label)
        self.assertIs(realm.access("greeter"), label)
        self.assertEqual(realm.size, 1)
        realm.delete("greeter")
        self.assertEqual(realm.size, 0)
        realm.delete("already-gone")

    def test_local_and_global_realm_construct(self):
        local = LocalRealm(entry=object())
        self.assertIsNotNone(local.entry)
        self.assertEqual(local.size, 0)
        glob = GlobalRealm("shared")
        self.assertEqual(glob.label, "shared")


class EntryTreeMethodsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        _write(self.root / "cordis.yml", CONFIG)
        self.ctx, self.activations = boot(self.root / "cordis.yml")
        self.loader = self.ctx.get("loader")
        self.tree = self.loader.resolve("include").subtree

    def test_ensure_id_generates_and_keeps_existing(self):
        options: dict = {}
        generated = self.tree.ensure_id(options)
        self.assertTrue(generated)
        self.assertEqual(options["id"], generated)
        self.assertEqual(self.tree.ensure_id({"id": "keep"}), "keep")

    def test_resolve_nested_missing_part_raises(self):
        with self.assertRaisesRegex(RuntimeError, "cannot resolve entry nope:inner"):
            self.tree.resolve("nope:inner")

    def test_resolve_missing_leaf_raises(self):
        with self.assertRaisesRegex(RuntimeError, "cannot resolve entry missing"):
            self.tree.resolve("missing")

    def test_resolve_group_root_and_child(self):
        self.assertIs(self.tree.resolve_group(None), self.tree.root)
        group = self.tree.resolve_group("grp")
        self.assertIs(group, self.tree.resolve("grp").subgroup)

    def test_resolve_group_non_group_raises(self):
        with self.assertRaisesRegex(RuntimeError, "entry solo is not a group"):
            self.tree.resolve_group("solo")

    def test_create_top_level_and_execute_service(self):
        self.tree.create({
            "id": "extra",
            "module": "miniharness.example_plugins",
            "config": {"greeting": "ex", "service_name": "extra_svc"},
        })
        self.assertIsNotNone(self.tree.resolve("extra"))
        self.assertEqual(self.ctx.get("extra_svc")("名"), "ex, 名!")

    def test_create_into_group_at_position(self):
        self.tree.create({
            "id": "first",
            "module": "miniharness.example_plugins",
            "config": {"greeting": "f", "service_name": "first_svc"},
        }, parent="grp", position=0)
        self.assertIsNotNone(self.tree.resolve("first"))
        group = self.tree.resolve_group("grp")
        self.assertEqual([row.get("id") for row in group.data], ["first", "inner"])

    def test_update_without_parent_reconfigures(self):
        self.tree.update("solo", {"config": {"greeting": "again",
                                             "service_name": "solo_svc"}})
        self.assertEqual(self.ctx.get("solo_svc")("名"), "again, 名!")

    def test_update_moves_across_parent_with_position(self):
        self.tree.update("solo", {"config": {"greeting": "moved",
                                             "service_name": "moved_svc"}},
                         parent="grp", position=0)
        self.assertIsNotNone(self.tree.resolve("solo"))
        self.assertIs(self.tree.resolve("solo").parent, self.tree.resolve_group("grp"))

    def test_remove_entry(self):
        self.tree.create({"id": "temp", "module": "miniharness.example_plugins",
                          "config": {"service_name": "temp_svc"}})
        self.assertIsNotNone(self.ctx.get("temp_svc"))
        self.tree.remove("temp")
        with self.assertRaises(RuntimeError):
            self.tree.resolve("temp")

    def test_await_all_without_tasks_returns(self):
        self.tree.await_all()

    def test_group_stop_disposes_children(self):
        group = self.tree.resolve_group("grp")
        group.stop()
        self.assertNotIn("inner", self.tree.store)
        with self.assertRaises(RuntimeError):
            self.tree.resolve("inner")


class IsolatePartialDisposeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _tree(self, ctx):
        return ctx.get("loader").resolve("include").subtree

    def test_remove_isolated_entry_gc_global_realm(self):
        _write(self.root / "cordis.yml",
               "plugins:\n"
               "  - id: pa\n"
               "    module: miniharness.example_plugins\n"
               "    isolate:\n"
               "      greeter: sharedA\n"
               "    config:\n"
               "      greeting: A\n"
               "      service_name: greeter\n"
               "  - id: pb\n"
               "    module: miniharness.example_plugins\n"
               "    isolate:\n"
               "      greeter: sharedB\n"
               "    config:\n"
               "      greeting: B\n"
               "      service_name: greeter\n")
        ctx, activations = boot(self.root / "cordis.yml")
        self.assertEqual([n for n, _ in activations], ["pa", "pb"])
        tree = self._tree(ctx)
        tree.remove("pa")
        with self.assertRaises(RuntimeError):
            tree.resolve("pa")
        self.assertEqual(tree.resolve("pb").ctx.get("greeter")("x"), "B, x!")

    def test_remove_keeps_referenced_label(self):
        _write(self.root / "cordis.yml",
               "plugins:\n"
               "  - id: qa\n"
               "    module: miniharness.example_plugins\n"
               "    isolate:\n"
               "      greeter: shared\n"
               "    config:\n"
               "      greeting: A\n"
               "      service_name: greeter\n"
               "  - id: qb\n"
               "    module: miniharness.example_plugins\n"
               "    isolate:\n"
               "      greeter: shared\n"
               "    config:\n"
               "      greeting: B\n"
               "      service_name: greeter2\n")
        ctx, _ = boot(self.root / "cordis.yml")
        tree = self._tree(ctx)
        tree.remove("qa")
        # qb 仍声明同标签 → realm 不回收
        self.assertIsNotNone(tree.resolve("qb"))


if __name__ == "__main__":
    unittest.main()
