"""skills watcher 验收：事件过滤、去抖、SkillWatchManager 集成、FileSystemSkillProvider watch。

覆盖：is_relevant_watch_event 过滤规则（根/一级/二级/跳过 .system）、
_Debouncer 去抖合并、SkillWatchManager update_roots 启停与 LRU 淘汰、
FileSystemSkillProvider watch 集成（临时目录写 skill → 自动检测）。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from miniharness.skills.filesystem import FileSystemSkillProvider
from miniharness.skills.watcher import (
    SkillWatchManager,
    _contained_segments,
    _Debouncer,
    is_relevant_watch_event,
)


# ---------- _contained_segments ----------

class TestContainedSegments(unittest.TestCase):
    """路径段提取：root 内/外/恰好 root。"""

    def test_outside_root(self):
        self.assertIsNone(_contained_segments("/a/b", "/x/y/z"))

    def test_exact_root(self):
        self.assertEqual(_contained_segments("/a/b", "/a/b"), [])

    def test_one_level(self):
        self.assertEqual(_contained_segments("/a/b", "/a/b/c.md"), ["c.md"])

    def test_two_levels(self):
        self.assertEqual(_contained_segments("/a/b", "/a/b/sub/SKILL.md"), ["sub", "SKILL.md"])

    def test_windows_relative(self):
        # 相对路径场景（pathlib 处理）
        segments = _contained_segments("root", "root/sub/file.md")
        self.assertEqual(segments, ["sub", "file.md"])


# ---------- is_relevant_watch_event ----------

class TestIsRelevantWatchEvent(unittest.TestCase):
    """事件过滤规则（对齐 upstream isRelevantWatchEvent）。"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        # 创建目录结构
        os.makedirs(os.path.join(self.root, "my-skill"))
        Path(os.path.join(self.root, "my-skill", "SKILL.md")).write_text("x")
        Path(os.path.join(self.root, "flat.md")).write_text("x")
        Path(os.path.join(self.root, "not-md.txt")).write_text("x")
        os.makedirs(os.path.join(self.root, ".system"))
        Path(os.path.join(self.root, ".system", "secret.md")).write_text("x")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def _rel(self, path):
        return os.path.join(self.root, path) if not os.path.isabs(path) else path

    def test_outside_root_irrelevant(self):
        self.assertFalse(is_relevant_watch_event(self.root, "modified", "/tmp/nope.md"))

    def test_root_dir_created_relevant(self):
        # 根目录本身被创建（理论上很少见）
        self.assertTrue(is_relevant_watch_event(self.root, "created", self.root))

    def test_root_dir_deleted_relevant(self):
        self.assertTrue(is_relevant_watch_event(self.root, "deleted", self.root))

    def test_root_file_modified_md_relevant(self):
        self.assertTrue(is_relevant_watch_event(self.root, "modified", self._rel("flat.md")))

    def test_root_file_modified_not_md(self):
        self.assertFalse(is_relevant_watch_event(self.root, "modified", self._rel("not-md.txt")))

    def test_root_dir_created_relevant(self):
        new_dir = os.path.join(self.root, "new-skill")
        os.makedirs(new_dir)
        self.assertTrue(is_relevant_watch_event(self.root, "created", new_dir))

    def test_bundle_skill_md_relevant(self):
        self.assertTrue(is_relevant_watch_event(
            self.root, "modified", self._rel("my-skill/SKILL.md"),
        ))

    def test_bundle_dir_not_relevant(self):
        # 子目录创建/删除（非 SKILL.md）不相关
        sub = os.path.join(self.root, "my-skill", "sub")
        os.makedirs(sub)
        self.assertFalse(is_relevant_watch_event(self.root, "created", sub))

    def test_deeply_nested_irrelevant(self):
        deep = os.path.join(self.root, "a", "b", "c.md")
        os.makedirs(os.path.dirname(deep))
        Path(deep).write_text("x")
        self.assertFalse(is_relevant_watch_event(self.root, "modified", deep))

    def test_dot_system_skipped(self):
        self.assertFalse(is_relevant_watch_event(
            self.root, "modified", self._rel(".system/secret.md"),
            skip_system=True,
        ))

    def test_dot_system_not_skipped_when_flag_false(self):
        # .system/secret.md 虽然 skip_system=False，但 segments 为 [".system","secret.md"]
        # 第二级非 SKILL.md → 仍不相关
        self.assertFalse(is_relevant_watch_event(
            self.root, "modified", self._rel(".system/secret.md"),
            skip_system=False,
        ))

    def test_dot_system_skill_md_relevant_when_not_skipped(self):
        # .system/SKILL.md 在 skip_system=False 时应相关（二级 SKILL.md）
        skill_md = os.path.join(self.root, ".system", "SKILL.md")
        Path(skill_md).write_text("x")
        self.assertTrue(is_relevant_watch_event(
            self.root, "modified", skill_md,
            skip_system=False,
        ))


# ---------- _Debouncer ----------

class TestDebouncer(unittest.TestCase):
    """去抖器：多次 call 合并为一次回调。"""

    def test_single_call(self):
        called = threading.Event()
        d = _Debouncer(lambda: called.set(), delay=0.05)
        d.schedule()
        self.assertTrue(called.wait(timeout=2.0))
        d.cancel()

    def test_rapid_calls_coalesced(self):
        count = []
        d = _Debouncer(lambda: count.append(1), delay=0.05)
        for _ in range(10):
            d.schedule()
        time.sleep(0.3)
        self.assertEqual(len(count), 1)
        d.cancel()

    def test_cancel_prevents_fire(self):
        called = threading.Event()
        d = _Debouncer(lambda: called.set(), delay=0.1)
        d.schedule()
        d.cancel()
        time.sleep(0.2)
        self.assertFalse(called.is_set())


# ---------- SkillWatchManager ----------

class TestSkillWatchManager(unittest.TestCase):
    """SkillWatchManager 集成测试：真实文件系统 + watchdog。"""

    def test_update_roots_starts_watcher(self):
        invalidated = threading.Event()
        mgr = SkillWatchManager(
            invalidate_callback=lambda: invalidated.set(),
            config={"watch": True},
        )
        root = tempfile.mkdtemp()
        try:
            mgr.update_roots([{"path": root, "source": "test"}])
            # 写一个 .md 文件应该触发 invalidation
            Path(os.path.join(root, "test.md")).write_text("hello")
            self.assertTrue(invalidated.wait(timeout=3.0))
        finally:
            mgr.dispose()
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_non_md_file_no_invalidation(self):
        invalidated = threading.Event()
        mgr = SkillWatchManager(
            invalidate_callback=lambda: invalidated.set(),
            config={"watch": True},
        )
        root = tempfile.mkdtemp()
        try:
            mgr.update_roots([{"path": root, "source": "test"}])
            Path(os.path.join(root, "test.txt")).write_text("hello")
            time.sleep(0.3)
            self.assertFalse(invalidated.is_set())
        finally:
            mgr.dispose()
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_lru_eviction(self):
        mgr = SkillWatchManager(
            invalidate_callback=lambda: None,
            config={"watch": True, "watchMaxProjects": 2},
        )
        root_dirs = []
        root_specs = []
        try:
            for i in range(3):
                root = tempfile.mkdtemp()
                root_dirs.append(root)
                root_specs.append({"path": root, "source": f"test-{i}"})
            mgr.update_roots(root_specs[:2])
            self.assertEqual(len(mgr._roots), 2)
            mgr.update_roots(root_specs)
            self.assertEqual(len(mgr._roots), 2)
            self.assertNotIn(root_specs[0]["path"], mgr._roots)
        finally:
            mgr.dispose()
            import shutil
            for d in root_dirs:
                shutil.rmtree(d, ignore_errors=True)

    def test_dispose_stops_all(self):
        mgr = SkillWatchManager(
            invalidate_callback=lambda: None,
            config={"watch": True},
        )
        root = tempfile.mkdtemp()
        try:
            mgr.update_roots([{"path": root, "source": "test"}])
            self.assertEqual(len(mgr._roots), 1)
            mgr.dispose()
            self.assertEqual(len(mgr._roots), 0)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_disabled_noop(self):
        mgr = SkillWatchManager(
            invalidate_callback=lambda: None,
            config={"watch": False},
        )
        root = tempfile.mkdtemp()
        try:
            mgr.update_roots([{"path": root, "source": "test"}])
            self.assertEqual(len(mgr._roots), 0)
        finally:
            mgr.dispose()
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_missing_root_watches_parent(self):
        invalidated = threading.Event()
        mgr = SkillWatchManager(
            invalidate_callback=lambda: invalidated.set(),
            config={"watch": True},
        )
        parent = tempfile.mkdtemp()
        child = os.path.join(parent, "missing-root")
        try:
            mgr.update_roots([{"path": child, "source": "test"}])
            # 创建 missing-root 目录 → addDir 事件应触发 invalidation
            os.makedirs(child)
            self.assertTrue(invalidated.wait(timeout=3.0))
        finally:
            mgr.dispose()
            import shutil
            shutil.rmtree(parent, ignore_errors=True)


# ---------- FileSystemSkillProvider watch 集成 ----------

class TestFileSystemProviderWatch(unittest.TestCase):
    """FileSystemSkillProvider 与 watcher 的集成。"""

    def test_provider_watcher_lazy_init(self):
        root = tempfile.mkdtemp()
        try:
            provider = FileSystemSkillProvider(
                {"invalidate": lambda: None},
                {"customSkillDirs": [root], "watch": True},
            )
            self.assertIsNone(provider._watcher)
            provider.list()
            self.assertIsNotNone(provider._watcher)
            provider.dispose()
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_provider_watch_false_no_watcher(self):
        provider = FileSystemSkillProvider(
            {"invalidate": lambda: None},
            {"watch": False},
        )
        provider.list()
        self.assertIsNone(provider._watcher)
        provider.dispose()

    def test_provider_dispose_stops_watcher(self):
        root = tempfile.mkdtemp()
        try:
            provider = FileSystemSkillProvider(
                {"invalidate": lambda: None},
                {"customSkillDirs": [root], "watch": True},
            )
            provider.list()
            self.assertIsNotNone(provider._watcher)
            provider.dispose()
            self.assertIsNone(provider._watcher)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_provider_watch_detects_new_skill(self):
        root = tempfile.mkdtemp()
        invalidated = threading.Event()
        try:
            provider = FileSystemSkillProvider(
                {"invalidate": lambda: invalidated.set()},
                {"customSkillDirs": [root], "watch": True},
            )
            # 首次 list 触发 watcher 初始化
            provider.list()
            # 创建一个合法 skill 文件
            skill_dir = os.path.join(root, "my-skill")
            os.makedirs(skill_dir)
            Path(os.path.join(skill_dir, "SKILL.md")).write_text(
                "---\nname: my-skill\ndescription: test\n---\nBody\n"
            )
            self.assertTrue(invalidated.wait(timeout=3.0))
            provider.dispose()
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_provider_watch_detects_skill_edit(self):
        root = tempfile.mkdtemp()
        invalidated = threading.Event()
        try:
            skill_dir = os.path.join(root, "my-skill")
            os.makedirs(skill_dir)
            Path(os.path.join(skill_dir, "SKILL.md")).write_text(
                "---\nname: my-skill\ndescription: v1\n---\nBody v1\n"
            )
            provider = FileSystemSkillProvider(
                {"invalidate": lambda: invalidated.set()},
                {"customSkillDirs": [root], "watch": True},
            )
            provider.list()
            # 编辑 skill 文件
            Path(os.path.join(skill_dir, "SKILL.md")).write_text(
                "---\nname: my-skill\ndescription: v2\n---\nBody v2\n"
            )
            self.assertTrue(invalidated.wait(timeout=3.0))
            provider.dispose()
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
