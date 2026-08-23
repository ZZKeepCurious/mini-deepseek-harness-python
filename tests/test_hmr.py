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

from miniharness.boot.boot import load_optional_patches, watch_user_patches
from miniharness.core.hmr import CONFIG_UPDATE_FAILED, Hmr, find_watch_root
from miniharness.core.scope import CordisError, Context, FiberState, INACTIVE_EFFECT


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
        self.patch_file = os.path.join(self._tmp.name, "cordis.patch.yml")
        self.bus = _EventBus()
        self.ctx = Context(name="root")
        self.remounted = []

    def tearDown(self):
        self.ctx.dispose()
        self._tmp.cleanup()

    def _install_hmr(self):
        return Hmr(self.ctx, base_dir=self._tmp.name,
                   internals={"watcher_factory": _fake_factory(self.bus)})

    def _write_patch_file(self, entries):
        with open(self.patch_file, "w", encoding="utf-8") as h:
            json.dump(entries, h)

    def test_requires_hmr_service(self):
        with self.assertRaisesRegex(RuntimeError, "requires the Cordis HMR service"):
            watch_user_patches(self.ctx, self.patch_file, self.remounted.append)

    def test_missing_patch_file_means_empty_layer(self):
        self._install_hmr()
        # 注册时文件不存在 → 无初扫（chokidar 只对已存在文件发 'add'）
        watch_user_patches(self.ctx, self.patch_file, self.remounted.append)
        self.assertEqual(self.remounted, [])
        with open(self.patch_file, "w", encoding="utf-8") as h:
            h.write("[]")
        self.bus.emit(os.path.abspath(self.patch_file))
        self.assertTrue(_wait_until(lambda: self.remounted == [[]]))

    def test_broken_patch_file_routes_to_failed_event(self):
        self._install_hmr()
        failures = []
        self.ctx.on(CONFIG_UPDATE_FAILED, lambda p: failures.append(p))
        watch_user_patches(self.ctx, self.patch_file, self.remounted.append)
        with open(self.patch_file, "w", encoding="utf-8") as h:
            h.write("{not-an-array")
        self.bus.emit(os.path.abspath(self.patch_file))
        self.assertTrue(_wait_until(lambda: bool(failures)))
        self.assertEqual(self.remounted, [])  # 刷新失败绝不误触 remount

    def test_change_delivers_new_patch_list_to_remount(self):
        self._install_hmr()
        watch_user_patches(self.ctx, self.patch_file, self.remounted.append)
        self._write_patch_file([{"id": "x", "config": {"k": 1}}])
        self.bus.emit(os.path.abspath(self.patch_file))
        self.assertTrue(_wait_until(lambda: len(self.remounted) >= 1))
        self.assertEqual(self.remounted[-1], [{"id": "x", "config": {"k": 1}}])

    def test_inactive_effect_returns_noop_disposer(self):
        hmr = self._install_hmr()
        with mock.patch.object(hmr, "register_config",
                               side_effect=CordisError(INACTIVE_EFFECT)):
            disposer = watch_user_patches(self.ctx, self.patch_file, self.remounted.append)
        self.assertIsNone(disposer())

    def test_end_to_end_epoch_reload_via_remount(self):
        """文件变更 → HMR 单飞 → remount 调 fiber.update → internal/update
        waterfall → epoch 卸载重装（动态 reload 触发源全链）。"""
        plugin_calls = []

        def apply(ctx, config=None):
            # Fiber 直接以位置实参传 resolved config（dict）
            plugin_calls.append((config or {}).get("value"))

        fiber = self.ctx.plugin({"name": "echo", "inject": [], "apply": apply},
                                {"value": "v1"})
        self.assertEqual(plugin_calls, ["v1"])
        self._install_hmr()

        def remount(patches):
            for patch in patches:
                if patch.get("replace", {}).get("id") == "echo":
                    fiber.update(patch["replace"].get("config", {}))

        watch_user_patches(self.ctx, self.patch_file, remount)
        self._write_patch_file(
            [{"replace": {"id": "echo", "config": {"value": "v2"}}}])
        self.bus.emit(os.path.abspath(self.patch_file))
        self.assertTrue(_wait_until(lambda: plugin_calls[-1:] == ["v2"]))
        self.assertEqual(fiber.state, FiberState.ACTIVE)


if __name__ == "__main__":
    unittest.main()
