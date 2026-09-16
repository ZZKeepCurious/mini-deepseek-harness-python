"""匿名用户 id 测试（identity 族，对照上游 anonymous-user-id.spec.ts）。"""
import os
import re
import shutil
import tempfile
import unittest

from miniharness.identity import ANONYMOUS_USER_ID_FILE_NAME, get_or_create_anonymous_user_id

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


class TestAnonymousUserId(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="mini-userid-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def home(self, *parts):
        return os.path.join(self._tmp, *parts) if parts else self._tmp

    def read_file(self, home):
        with open(os.path.join(home, ANONYMOUS_USER_ID_FILE_NAME), "r", encoding="utf-8") as f:
            return f.read()

    def test_creates_persists_and_returns_bare_uuid_on_first_use(self):
        home = self.home()
        user_id = get_or_create_anonymous_user_id(env={"DSH_HOME": home})
        self.assertRegex(user_id, UUID)
        self.assertEqual(self.read_file(home), f"{user_id}\n")

    def test_creates_home_directory_when_missing(self):
        home = self.home("nested", "home")
        user_id = get_or_create_anonymous_user_id(env={"DSH_HOME": home})
        self.assertEqual(self.read_file(home), f"{user_id}\n")

    def test_returns_persisted_id_tolerating_surrounding_whitespace(self):
        home = self.home()
        existing = "01234567-89ab-4cde-8f01-23456789abcd"
        with open(os.path.join(home, ANONYMOUS_USER_ID_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"  {existing}\n\n")
        self.assertEqual(get_or_create_anonymous_user_id(env={"DSH_HOME": home}), existing)

    def test_overwrites_corrupt_file_with_fresh_id(self):
        home = self.home()
        with open(os.path.join(home, ANONYMOUS_USER_ID_FILE_NAME), "w", encoding="utf-8") as f:
            f.write("not-a-uuid\n")
        user_id = get_or_create_anonymous_user_id(env={"DSH_HOME": home})
        self.assertRegex(user_id, UUID)
        self.assertEqual(self.read_file(home), f"{user_id}\n")

    def test_adopts_concurrent_winner_via_exclusive_create(self):
        home = self.home()
        winner = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        file = os.path.join(home, ANONYMOUS_USER_ID_FILE_NAME)
        # 生成器钩子运行于初始读（缺失）与独占创建之间，此时植入胜者文件，
        # 模拟并发首启。
        def generator():
            with open(file, "w", encoding="utf-8") as f:
                f.write(f"{winner}\n")
            return "ffffffff-0000-4000-8000-000000000000"

        user_id = get_or_create_anonymous_user_id(env={"DSH_HOME": home}, random_uuid=generator)
        self.assertEqual(user_id, winner)

    def test_returns_usable_id_when_home_cannot_contain_files(self):
        home = self.home()
        blocked = os.path.join(home, "blocked")
        with open(blocked, "w", encoding="utf-8") as f:
            f.write("occupied\n")
        user_id = get_or_create_anonymous_user_id(env={"DSH_HOME": blocked})
        self.assertRegex(user_id, UUID)
        self.assertFalse(os.path.exists(os.path.join(blocked, ANONYMOUS_USER_ID_FILE_NAME)))

    def test_memoizes_per_resolved_home_module_lifetime(self):
        home = self.home()
        first = get_or_create_anonymous_user_id(env={"DSH_HOME": home})
        os.remove(os.path.join(home, ANONYMOUS_USER_ID_FILE_NAME))
        self.assertEqual(get_or_create_anonymous_user_id(env={"DSH_HOME": home}), first)

    def test_keeps_distinct_homes_on_distinct_ids(self):
        home_a = self.home("a")
        home_b = self.home("b")
        a = get_or_create_anonymous_user_id(env={"DSH_HOME": home_a})
        b = get_or_create_anonymous_user_id(env={"DSH_HOME": home_b})
        self.assertNotEqual(a, b)

    def test_reads_environment_by_default(self):
        home = self.home()
        previous = os.environ.get("DSH_HOME")
        os.environ["DSH_HOME"] = home
        try:
            user_id = get_or_create_anonymous_user_id()
            self.assertEqual(self.read_file(home), f"{user_id}\n")
        finally:
            if previous is None:
                os.environ.pop("DSH_HOME", None)
            else:
                os.environ["DSH_HOME"] = previous


class TestResolveDshHome(unittest.TestCase):
    def test_resolve_dsh_home_prefers_env(self):
        from miniharness.core.home_paths import resolve_dsh_home

        self.assertEqual(
            resolve_dsh_home(env={"DSH_HOME": "C:/harness"}), "C:\\harness" if os.name == "nt" else "/harness"
        )

    def test_resolve_dsh_home_tilde_expansion(self):
        from miniharness.core.home_paths import expand_home_path

        self.assertEqual(expand_home_path("~"), os.path.expanduser("~"))
        self.assertEqual(expand_home_path("~/x"), os.path.join(os.path.expanduser("~"), "x"))
        self.assertEqual(expand_home_path("~\\y"), os.path.join(os.path.expanduser("~"), "y"))
        self.assertEqual(expand_home_path("plain"), "plain")


if __name__ == "__main__":
    unittest.main()