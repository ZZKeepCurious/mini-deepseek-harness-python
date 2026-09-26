"""第 5 章补：HMR 配置热重载命令面（对齐 vendor/hmr registerConfig/refreshConfig
+ app-boot watchUserPatches）。

测试载体：watcher_factory 注入假柄直接投递事件（确定性时序），另设一条
真 watchdog 集成测试验证 OS 事件链路。单飞/dirty 合并用阻塞事件精确断言。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from miniharness.boot import boot
from miniharness.boot.boot import load_optional_patches, watch_user_patches
from miniharness.core.hmr import CONFIG_UPDATE_FAILED, Hmr, find_watch_root
from miniharness.core.scope import CordisError, Context, INACTIVE_EFFECT


class _EventBus:
    """把事件路由到当前登记的 handler（模拟 Observer 分发）。"""

    def __init__(self) -> None:
        self.handler = None

    def emit(self, path: str) -> None:
        if self.handler is not None:
            self.handler({"src_path": path})


def _fake_factory(bus):
    created = []

    def factory(root, on_event):
        bus.handler = on_event
        watcher = type("FakeWatcher", (), {
            "closed": False,
            "root": root,
            "close": lambda self: setattr(self, "closed", True),
        })()
        created.append(watcher)
        return watcher
    factory.created = created
    return factory


class _BlockedRefresh:
    """可阻塞的刷新计数器：构造即阻塞首次刷新直至放行。"""

    def __init__(self):
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self):
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=10.0)


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class TestFindWatchRoot(unittest.TestCase):
    def test_existing_parent_depth_zero_and_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            canonical = os.path.realpath(tmp)
            name, root, depth = find_watch_root(os.path.join(tmp, "patches.yml"))
            self.assertEqual(depth, 0)
            self.assertEqual(root, canonical)
            self.assertTrue(name.startswith(canonical))
            self.assertIn("patches.yml", name)

    def test_missing_dirs_walk_up_counting_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep = os.path.join(tmp, "a", "b", "c")
            name, root, depth = find_watch_root(os.path.join(deep, "f.yml"))
            self.assertGreaterEqual(depth, 3)
            self.assertEqual(os.path.realpath(root), os.path.realpath(tmp))
            self.assertTrue(name.endswith("f.yml"))

    def test_no_existing_directory_above_fails_loud(self):
        # Windows 专属分支：盘符不存在时上溯到根仍无目录（POSIX 的 / 恒存在）
        missing_drive = os.path.abspath("Q:\\") if os.name == "nt" else None
        if missing_drive is None or os.path.exists(missing_drive):
            self.skipTest("需要不存在的盘符")
        with self.assertRaises(FileNotFoundError):
            find_watch_root(os.path.join(missing_drive, "missing", "f.yml"))


class TestLoadOptionalPatches(unittest.TestCase):
    def test_missing_file_is_empty_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_optional_patches(os.path.join(tmp, "nope.yml")), [])

    def test_broken_file_fails_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.yml")
            with open(path, "w", encoding="utf-8") as h:
                h.write("{not-an-array")
            with self.assertRaises(RuntimeError):
                load_optional_patches(path)

    def test_non_array_fails_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "obj.json")
            with open(path, "w", encoding="utf-8") as h:
                json.dump({"id": "x"}, h)
            with self.assertRaisesRegex(RuntimeError, "顶层必须是数组"):
                load_optional_patches(path)


class TestHmrRegisterConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.target = os.path.join(self.base, "cordis.patch.yml")
        self.bus = _EventBus()
        self.ctx = Context(name="root")
        self.hmr = Hmr(self.ctx, base_dir=self.base,
                       internals={"watcher_factory": _fake_factory(self.bus)})

    def tearDown(self):
        self.ctx.dispose()
        self._tmp.cleanup()

    def test_duplicate_registration_fails_loud(self):
        self.hmr.register_config(self.target, lambda: None)
        with self.assertRaisesRegex(RuntimeError, "already registered"):
            self.hmr.register_config(self.target, lambda: None)

    def test_register_after_owner_disposed_is_inactive_effect(self):
        ctx = Context(name="root")
        svc = Hmr(ctx, base_dir=self.base,
                  internals={"watcher_factory": _fake_factory(_EventBus())})
        ctx.dispose()
        with self.assertRaises(CordisError) as caught:
            svc.register_config(self.target, lambda: None)
        self.assertEqual(caught.exception.code, INACTIVE_EFFECT)

    def test_existing_file_refreshes_once_on_register(self):
        with open(self.target, "w", encoding="utf-8") as h:
            h.write("[]\n")
        counter = _BlockedRefresh()
        disposer = self.hmr.register_config(self.target, counter)
        try:
            self.assertTrue(counter.entered.wait(timeout=5.0))
        finally:
            counter.release.set()
            disposer()

    def test_change_event_triggers_refresh(self):
        seen = []
        self.hmr.register_config(self.target, lambda: seen.append(1))
        with open(self.target, "w", encoding="utf-8") as h:
            h.write("[]\n")
        self.bus.emit(os.path.abspath(self.target))
        self.assertTrue(_wait_until(lambda: len(seen) >= 1))

    def test_other_paths_do_not_trigger(self):
        seen = []
        self.hmr.register_config(self.target, lambda: seen.append(1))
        self.bus.emit(os.path.join(self.base, "unrelated.yml"))
        self.assertFalse(_wait_until(lambda: bool(seen), timeout=0.4))

    def test_single_flight_coalesces_dirty_bursts(self):
        counter = _BlockedRefresh()
        disposer = self.hmr.register_config(self.target, counter)
        try:
            # 初扫启动第一次刷新；阻塞期间连发两次变更 → 折叠为恰好一次补跑
            self.bus.emit(os.path.abspath(self.target))
            self.assertTrue(counter.entered.wait(timeout=5.0))
            self.bus.emit(os.path.abspath(self.target))
            self.bus.emit(os.path.abspath(self.target))
            counter.release.set()
            self.assertTrue(_wait_until(lambda: counter.calls >= 2))
            self.assertEqual(counter.calls, 2)
        finally:
            counter.release.set()
            disposer()

    def test_refresh_failure_emits_event_and_loop_survives(self):
        failures = []
        self.ctx.on(CONFIG_UPDATE_FAILED, lambda payload: failures.append(payload))
        state = {"n": 0}

        def refresh():
            state["n"] += 1
            if state["n"] == 1:
                raise ValueError("boom")

        # 目标文件注册时不存在 → 无初扫；创建后经事件触发首次刷新
        self.hmr.register_config(self.target, refresh)
        with open(self.target, "w", encoding="utf-8") as h:
            h.write("broken")
        self.bus.emit(os.path.abspath(self.target))
        self.assertTrue(_wait_until(lambda: len(failures) >= 1))
        self.assertEqual(failures[0]["filename"], os.path.abspath(self.target))
        self.assertIsInstance(failures[0]["error"], ValueError)
        # 循环未被毒化：后续变更照常刷新
        self.bus.emit(os.path.abspath(self.target))
        self.assertTrue(_wait_until(lambda: state["n"] >= 2))

    def test_disposer_stops_watching_and_joins_inflight(self):
        counter = _BlockedRefresh()
        disposer = self.hmr.register_config(self.target, counter)
        self.bus.emit(os.path.abspath(self.target))
        self.assertTrue(counter.entered.wait(timeout=5.0))
        done = threading.Event()

        def run():
            disposer()
            done.set()

        joiner = threading.Thread(target=run)
        joiner.start()
        time.sleep(0.05)
        self.assertFalse(done.is_set())  # 在飞刷新未结束前 disposer 不返回
        counter.release.set()
        self.assertTrue(done.wait(timeout=5.0))
        calls_after = counter.calls
        self.bus.emit(os.path.abspath(self.target))
        self.assertFalse(_wait_until(lambda: counter.calls != calls_after, timeout=0.4))


class TestServiceTeardown(unittest.TestCase):
    def test_owner_dispose_closes_all_watchers(self):
        with tempfile.TemporaryDirectory() as tmp:
            bus = _EventBus()
            factory = _fake_factory(bus)
            ctx = Context(name="root")
            hmr = Hmr(ctx, base_dir=tmp, internals={"watcher_factory": factory})
            hmr.register_config(os.path.join(tmp, "a.yml"), lambda: None)
            hmr.register_config(os.path.join(tmp, "b.yml"), lambda: None)
            ctx.dispose()
            self.assertTrue(all(w.closed for w in factory.created))


class TestRealWatchdogIntegration(unittest.TestCase):
    def test_real_file_edit_triggers_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "patch.yml")
            with open(target, "w", encoding="utf-8") as h:
                json.dump([], h)
            seen = []
            ctx = Context(name="root")
            hmr = Hmr(ctx, base_dir=tmp)
            disposer = hmr.register_config(target, lambda: seen.append(1))
            try:
                # 初扫即刷一次（chokidar ignoreInitial=false 语义）
                self.assertTrue(_wait_until(lambda: len(seen) >= 1))
                with open(target, "a", encoding="utf-8") as h:
                    h.write("# edit\n")
                self.assertTrue(_wait_until(lambda: len(seen) >= 2))
            finally:
                disposer()


class TestWatchUserPatches(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.abspath(self._tmp.name)
        self.patch_file = os.path.join(self.dir, "cordis.patch.yml")
        self.bus = _EventBus()
        self.config_file = os.path.join(self.dir, "cordis.yml")
        with open(self.config_file, "w", encoding="utf-8") as h:
            h.write(
                "plugins:\n"
                "  - id: greeter\n"
                "    module: miniharness.example_plugins\n"
                "    config:\n"
                "      greeting: hi\n")
        self.ctx, _ = boot(self.config_file)
        self._install_hmr()

    def tearDown(self):
        self.ctx.dispose()
        self._tmp.cleanup()

    def _install_hmr(self):
        return Hmr(self.ctx, base_dir=self.dir,
                   internals={"watcher_factory": _fake_factory(self.bus)})

    def _write_patch(self, text):
        with open(self.patch_file, "w", encoding="utf-8") as h:
            h.write(text)

    def test_requires_hmr_service(self):
        other = Context(name="root")
        try:
            with self.assertRaisesRegex(RuntimeError, "requires the Cordis HMR service"):
                watch_user_patches(other, self.patch_file)
        finally:
            other.dispose()

    def test_requires_root_include_entry(self):
        ctx = Context(name="root")
        try:
            Hmr(ctx, base_dir=self.dir, internals={"watcher_factory": _fake_factory(_EventBus())})
            with self.assertRaisesRegex(RuntimeError, "requires the root Include entry"):
                watch_user_patches(ctx, self.patch_file)
        finally:
            ctx.dispose()

    def test_patch_change_reapplies_to_root_include(self):
        watch_user_patches(self.ctx, self.patch_file)
        self._write_patch(
            "- replace:\n"
            "    id: greeter\n"
            "    config:\n"
            "      greeting: yo\n")
        self.bus.emit(self.patch_file)
        self.assertTrue(_wait_until(lambda: self.ctx.get("greeter")("x") == "yo, x!"))

    def test_removing_patch_file_reverts_layer(self):
        watch_user_patches(self.ctx, self.patch_file)
        self._write_patch(
            "- replace:\n"
            "    id: greeter\n"
            "    config:\n"
            "      greeting: yo\n")
        self.bus.emit(self.patch_file)
        self.assertTrue(_wait_until(lambda: self.ctx.get("greeter")("x") == "yo, x!"))
        os.remove(self.patch_file)
        self.bus.emit(self.patch_file)
        self.assertTrue(_wait_until(lambda: self.ctx.get("greeter")("x") == "hi, x!"))

    def test_broken_patch_file_routes_to_failed_event(self):
        failures = []
        self.ctx.on(CONFIG_UPDATE_FAILED, lambda p: failures.append(p))
        watch_user_patches(self.ctx, self.patch_file)
        self._write_patch("{not-an-array")
        self.bus.emit(self.patch_file)
        self.assertTrue(_wait_until(lambda: bool(failures)))
        self.assertEqual(self.ctx.get("greeter")("x"), "hi, x!")  # 失败不改动活树

    def test_inactive_effect_returns_noop_disposer(self):
        hmr = self.ctx.get("hmr")
        with mock.patch.object(hmr, "register_config",
                               side_effect=CordisError(INACTIVE_EFFECT)):
            disposer = watch_user_patches(self.ctx, self.patch_file)
        self.assertIsNone(disposer())


class TestHmrRunExclusive(unittest.TestCase):
    """`Hmr.run_exclusive` 串行事务队列（对齐 packages/boot/hmr runExclusive）。"""

    def setUp(self):
        self.ctx = Context(name="hmr-exclusive")
        self.addCleanup(self.ctx.dispose)
        self.hmr = Hmr(self.ctx, base_dir=".")

    def test_runs_operation_and_returns_value(self):
        self.assertEqual(self.hmr.run_exclusive(lambda: 42), 42)

    def test_serializes_concurrent_operations(self):
        order: list[str] = []
        gate = threading.Event()

        def first():
            order.append("first")
            gate.wait(timeout=5.0)
            order.append("first-done")

        def second():
            order.append("second")

        t = threading.Thread(target=lambda: self.hmr.run_exclusive(first))
        t.start()
        time.sleep(0.05)
        self.hmr.run_exclusive(second)
        self.assertEqual(order, ["first", "first-done", "second"])
        gate.set()
        t.join(timeout=5.0)

    def test_nested_transaction_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "cannot be nested"):
            self.hmr.run_exclusive(
                lambda: self.hmr.run_exclusive(lambda: None))

    def test_disposed_rejects(self):
        self.ctx.dispose()
        with self.assertRaisesRegex(RuntimeError, "HMR is disposed"):
            self.hmr.run_exclusive(lambda: None)


class TestReconcileProfilePatches(unittest.TestCase):
    """`reconcile_profile_patches` 对账（对齐 app-boot reconcileProfilePatches）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.abspath(self._tmp.name)
        self.config_file = os.path.join(self.dir, "cordis.yml")
        with open(self.config_file, "w", encoding="utf-8") as h:
            h.write(
                "plugins:\n"
                "  - id: greeter\n"
                "    module: miniharness.example_plugins\n"
                "    config:\n"
                "      greeting: hi\n")
        self.ctx, _ = boot(self.config_file)

    def tearDown(self):
        self.ctx.dispose()
        self._tmp.cleanup()

    def _reload(self, patches, required_ids=None):
        from miniharness.boot.boot import reconcile_profile_patches
        return reconcile_profile_patches(self.ctx, patches, bin_name="miniharness",
                                         required_ids=required_ids)

    def test_valid_patch_reconciles_and_emits_config_reload(self):
        reloaded = []
        self.ctx.on("app-boot/config-reload", lambda *args: reloaded.append(True))
        result = self._reload([
            {"replace": {"id": "greeter", "config": {"greeting": "yo"}}}])
        self.assertEqual(result, [])
        self.assertTrue(reloaded)
        self.assertEqual(self.ctx.get("greeter")("x"), "yo, x!")

    def test_unchanged_failure_returns_diagnostic_not_throw(self):
        # 先引入一个缺失模块条目（首次即 failure），重载时 unchanged
        # （same entry/fiber/options/diagnostic）不抛、返回诊断列表。
        with self.assertRaisesRegex(RuntimeError, "did not activate"):
            self._reload([
                {"insert": [{"id": "broken",
                             "module": "miniharness.does_not_exist"}]}])
        # 已存在的 broken 条目对账：unchanged → 返回诊断而非抛
        result = self._reload([
            {"insert": [{"id": "broken",
                         "module": "miniharness.does_not_exist"}]}])
        self.assertTrue(any("broken" in diagnostic for diagnostic in result))

    def test_unchanged_failure_with_required_id_rejects(self):
        with self.assertRaisesRegex(RuntimeError, "did not activate"):
            self._reload([
                {"insert": [{"id": "broken",
                             "module": "miniharness.does_not_exist"}]}])
        # requiredIds 点名该 broken 条目 → 既有失败也拒绝对账
        with self.assertRaisesRegex(RuntimeError, "did not activate"):
            self._reload(
                [{"insert": [{"id": "broken",
                              "module": "miniharness.does_not_exist"}]}],
                required_ids=["broken"])


if __name__ == "__main__":
    unittest.main()
