"""P2-2 测试：凭据文件主动感知（watchdog watchdog watch + 逐 entry fan-out 事件）。

语义对齐上游 credentials-local chokidar watch + reconcileFromDisk 文本比对 +
per-entry diff fan-out（credentials/reference-updated / credentials/record-updated）。

mini 缺省 watch=False（与 ACP persistence 同构，显式开启避免默认起线程）；
开启时外部编辑由 watchdog 事件主动折叠，并经装配的 CredentialsService 发事件。
自写 echo 抑制：写路径写完 `_text == 磁盘文本` → reconcile 早退，不发事件。
"""
import os
import tempfile
import time
import unittest

from miniharness.core.scope import Context
from miniharness.seams import install_credentials
from miniharness.seams.credentials_local import LocalCredentialProvider

WAIT = 5.0
POLL = 0.02


def _wait_until(pred, timeout=WAIT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(POLL)
    return False


class TestCredentialsWatchProvider(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dsh_home = os.path.join(self._tmp.name, "dsh")
        self._filename = os.path.join(self._dsh_home, ".credentials.json")
        self._events = []
        self._provider = LocalCredentialProvider(
            filename=self._filename, dsh_home=self._dsh_home,
            project_dir=self._tmp.name, read_env=False)

    def tearDown(self):
        try:
            self._provider.stop_watch()
        except Exception:
            pass
        self._tmp.cleanup()

    def _watched(self, watch=True):
        p = LocalCredentialProvider(
            filename=self._filename, dsh_home=self._dsh_home,
            project_dir=self._tmp.name, read_env=False, watch=watch)
        p.on_change = lambda subject, kind: self._events.append((kind, subject))
        return p

    def _write_external(self, text):
        os.makedirs(self._dsh_home, exist_ok=True)
        with open(self._filename, "w", encoding="utf-8") as handle:
            handle.write(text)

    # ---- 缺省关闭 ----

    def test_watch_off_by_default_no_observer(self):
        self.assertIsNone(self._provider._observer)
        self.assertFalse(self._provider._watch)

    def test_watch_false_no_background_thread(self):
        p = self._watched(watch=False)
        self.assertIsNone(p._observer)

    # ---- 开启 + 初始状态 ----

    def test_watch_true_starts_observer_and_initial_noop(self):
        self._write_external('{"version": 1, "refs": {"api_key": "sk-1"}}\n')
        p = self._watched(watch=True)
        try:
            self.assertIsNotNone(p._observer)
            self.assertEqual(p.resolve("api_key"), ("sk-1", "file"))
            # 初始 reconcile 文本已等于磁盘 → 无事件
            self.assertEqual(self._events, [])
        finally:
            p.stop_watch()

    # ---- 外部编辑推进折叠 + 事件 ----

    def test_external_edit_folds_and_emits_reference(self):
        p = self._watched(watch=True)
        try:
            self._write_external('{"version": 1, "refs": {"api_key": "sk-new"}}\n')
            self.assertTrue(_wait_until(lambda: p.resolve("api_key") == ("sk-new", "file")),
                            "external ref edit not folded")
            self.assertIn(("reference", "api_key"), self._events)
        finally:
            p.stop_watch()

    def test_external_record_edit_emits_record(self):
        p = self._watched(watch=True)
        try:
            self._write_external(
                '{"version": 1, "refs": {}, "records": {"a/b": {"kind": "grant", "payload": 1}}}\n')
            self.assertTrue(_wait_until(lambda: p.read_record("a/b") is not None))
            self.assertIn(("record", "a/b"), self._events)
        finally:
            p.stop_watch()

    def test_external_delete_clears_and_emits(self):
        p = self._watched(watch=True)
        try:
            p.set("api_key", "sk-1")
            self._events.clear()
            os.remove(self._filename)
            self.assertTrue(_wait_until(lambda: p.resolve("api_key") is None),
                            "external delete not cleared")
            self.assertIn(("reference", "api_key"), self._events)
        finally:
            p.stop_watch()

    def test_external_edit_when_nonexistent_file_created(self):
        p = self._watched(watch=True)
        try:
            # 文件本不存在（空存储），外部进程创建它 → watch 推进
            self._write_external('{"version": 1, "refs": {"k": "v"}}\n')
            self.assertTrue(_wait_until(lambda: p.resolve("k") == ("v", "file")))
        finally:
            p.stop_watch()

    # ---- 自写 echo 抑制 ----

    def test_self_write_does_not_emit(self):
        p = self._watched(watch=True)
        try:
            p.set("api_key", "sk-1")
            # 自写后 text == 磁盘 → reconcile 早退，无 on_change 事件
            time.sleep(0.4)
            self.assertEqual(self._events, [])
        finally:
            p.stop_watch()

    def test_reconcile_after_self_write_keeps_diffable(self):
        p = self._watched(watch=True)
        try:
            p.set("api_key", "sk-1")
            self._events.clear()
            # 外部编辑同一文件 → 事后仍能折叠 diff
            self._write_external('{"version": 1, "refs": {"api_key": "sk-2"}}\n')
            self.assertTrue(_wait_until(lambda: p.resolve("api_key") == ("sk-2", "file")))
            self.assertIn(("reference", "api_key"), self._events)
        finally:
            p.stop_watch()

    # ---- 关闭幂等 ----

    def test_stop_watch_idempotent_and_clears(self):
        p = self._watched(watch=True)
        self.assertIsNotNone(p._observer)
        p.stop_watch()
        self.assertIsNone(p._observer)
        p.stop_watch()  # 二次关闭 no-op


class TestCredentialsWatchService(unittest.TestCase):
    """经 CredentialsService 装配：外部编辑 → ctx 事件外发。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dsh_home = os.path.join(self._tmp.name, "dsh")
        self._filename = os.path.join(self._dsh_home, ".credentials.json")
        self.ctx = Context(name="cred-watch")
        self.provider = LocalCredentialProvider(
            filename=self._filename, dsh_home=self._dsh_home,
            project_dir=self._tmp.name, read_env=False, watch=True)
        self.creds = install_credentials(self.ctx, self.provider)
        self.reference_updated = []
        self.record_updated = []
        self.ctx.on("credentials/reference-updated", lambda p: self.reference_updated.append(p))
        self.ctx.on("credentials/record-updated", lambda p: self.record_updated.append(p))

    def tearDown(self):
        try:
            self.provider.stop_watch()
        except Exception:
            pass
        self._tmp.cleanup()

    def _write_external(self, text):
        os.makedirs(self._dsh_home, exist_ok=True)
        with open(self._filename, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_reference_edit_emits_ctx_event(self):
        self._write_external('{"version": 1, "refs": {"api_key": "sk-new"}}\n')
        self.assertTrue(
            _wait_until(lambda: ("api_key" in self.reference_updated) and
                        self.provider.resolve("api_key") == ("sk-new", "file")))
        self.assertIn("api_key", self.reference_updated)

    def test_record_edit_emits_ctx_event(self):
        self._write_external(
            '{"version": 1, "refs": {}, "records": {"a/b": {"kind": "grant", "payload": 1}}}\n')
        self.assertTrue(_wait_until(lambda: "a/b" in self.record_updated))
        self.assertIn("a/b", self.record_updated)

    def test_service_self_write_no_double_emit(self):
        # 自写触发 1 次 record-updated（modify_record 路径）；watch 不回放（echo 抑制）
        self.creds.modify_record("a/b", lambda _: {"kind": "grant", "payload": 1})
        self.assertEqual(self.record_updated.count("a/b"), 1)
        time.sleep(0.4)
        self.assertEqual(self.record_updated.count("a/b"), 1)
