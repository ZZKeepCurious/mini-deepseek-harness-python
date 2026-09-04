"""第 6 章扩展验收：真沙箱后端 / 凭据多来源 / 子 agent 远程三通道。

运行：python -m unittest discover -s tests -t .
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from miniharness.seams.credentials_local import (
    CredentialWriteLocked,
    LocalCredentialProvider,
    _assert_owner_only,
    credential_key,
    credential_key_id,
    credential_key_scope,
    credential_ref,
    is_credential_key_segment,
    is_credential_ref_name,
    parse_credentials_document,
    parse_credential_key,
    parse_dotenv,
    render_flat_layout_migration,
    resolve_dsh_home,
)
from miniharness.seams import credentials_local
from filelock import FileLock
from miniharness.seams.subprocess_env import (
    DSH_ENV_PREFIX,
    SENSITIVE_ENV_PATTERN,
    scrubbed_parent_env,
)
from miniharness.seams.sandbox_local import (
    DENIAL_SIGNATURES,
    RUNNER_FAILURE_RULES,
    SANDBOX_UNAVAILABLE,
    WINDOWS_ACL_RUNNER_MODULE,
    LocalSandboxProvider,
    SandboxUnavailableError,
    bwrap_profile_args,
    canonical_path,
    landlock_profile_args,
    probe_windows_acl,
    seatbelt_profile_args,
    windows_acl_runner_args,
    writable_roots,
)
from miniharness.core.session import Session, thaw
from miniharness.seams.subagent.providers import (
    AcpSubAgentProvider,
    ForkSubAgentProvider,
    SdkSubAgentProvider,
    completed_turn_prefix,
)
from miniharness.seams.subagent.worker import _SdkWorkerRuntime
from miniharness.core.scope import Context
from miniharness.llm import FakeLlmAdapter
from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.tools import Tool, ToolRegistry


# ==================== 1) 真沙箱后端 ====================

class TestSandboxProfiles(unittest.TestCase):
    def test_bwrap_read_only(self):
        args = bwrap_profile_args({"mode": "read-only", "workspaceRoot": "/ws"})
        self.assertEqual(args, ["--ro-bind", "/", "/", "--dev", "/dev",
                                "--proc", "/proc", "--die-with-parent"])

    def test_bwrap_workspace_write(self):
        args = bwrap_profile_args({"mode": "workspace-write", "workspaceRoot": "/ws"})
        self.assertEqual(args[-5:], ["--tmpfs", "/tmp", "--bind", "/ws", "/ws"])

    def test_landlock_grants(self):
        grants = landlock_profile_args({"mode": "read-only", "workspaceRoot": "/ws"})
        self.assertEqual(grants, {"readOnly": ["/"], "readWrite": ["/dev/null"]})
        grants = landlock_profile_args({"mode": "workspace-write", "workspaceRoot": "/ws"})
        self.assertEqual(grants["readWrite"], ["/dev/null", "/tmp", "/ws"])

    def test_seatbelt_read_only_no_writable_roots(self):
        args = seatbelt_profile_args({"mode": "read-only", "workspaceRoot": "/ws"})
        self.assertEqual(args[0], "-p")
        self.assertNotIn("subpath", args[1])
        self.assertIn("(deny file-write*)", args[1])

    def test_seatbelt_workspace_write_has_subpaths(self):
        args = seatbelt_profile_args({"mode": "workspace-write", "workspaceRoot": "/ws"})
        self.assertIn("(allow file-write* (subpath", args[1])

    def test_seatbelt_escapes_quotes_and_backslashes(self):
        # C:\a"b 在非 Windows 上不是绝对路径，realpath 会拼 cwd 前缀；
        # 用平台绝对路径保留"引号 + 反斜杠转义"的测试意图
        root = os.path.abspath(r'C:\a"b')
        args = seatbelt_profile_args({"mode": "workspace-write",
                                      "workspaceRoot": root})
        escaped = root.replace("\\", "\\\\").replace('"', '\\"')
        self.assertIn(f'(subpath "{escaped}")', args[1])

    def test_writable_roots_empty_under_read_only(self):
        self.assertEqual(writable_roots({"mode": "read-only", "workspaceRoot": "/ws"}), [])

    def test_writable_roots_dedup_canonical(self):
        roots = writable_roots({"mode": "workspace-write", "workspaceRoot": "/tmp"})
        self.assertIn(canonical_path("/tmp"), roots)
        self.assertEqual(len(set(roots)), len(roots))


class TestSandboxProvider(unittest.TestCase):
    def test_runner_command_override_skips_selection(self):
        provider = LocalSandboxProvider(
            runner_command=["runner"], runner_failure_signatures=["denied!"])
        out = provider.confine(["echo", "hi"], {"mode": "read-only", "workspaceRoot": "/ws"})
        self.assertEqual(out["argv"], ["runner", "--ro-bind", "/", "/", "--dev", "/dev",
                                       "--proc", "/proc", "--die-with-parent", "--", "echo", "hi"])
        self.assertEqual(out["enforcement"], "full")
        self.assertEqual(out["denialSignatures"], DENIAL_SIGNATURES["runnerCommand"])
        self.assertEqual(out["runnerFailureRules"], [{"fatalSignatures": ["denied!"]}])

    def test_config_validation(self):
        with self.assertRaises(ValueError):
            LocalSandboxProvider(runner_command=["runner"])
        with self.assertRaises(ValueError):
            LocalSandboxProvider(runner_failure_signatures=["x"])
        with self.assertRaises(ValueError):
            LocalSandboxProvider(runner_command=["r"], runner_failure_signatures=["", "ok"])
        with self.assertRaises(ValueError):
            LocalSandboxProvider(runner_command=["r"], runner_failure_signatures=["a\nb"])
        with self.assertRaises(ValueError):
            LocalSandboxProvider(probe_timeout_ms=0)

    def test_linux_chain_bwrap_probe_wins(self):
        provider = LocalSandboxProvider(internals={
            "platform": "linux",
            "probeBwrap": lambda: True,
            "probeLandlock": lambda launcher: "unusable",
        })
        out = provider.confine(["true"], {"mode": "read-only", "workspaceRoot": "/ws"})
        self.assertEqual(out["argv"][0], "bwrap")
        self.assertEqual(out["enforcement"], "full")

    def test_linux_chain_falls_back_to_landlock(self):
        provider = LocalSandboxProvider(internals={
            "platform": "linux",
            "probeBwrap": lambda: False,
            "probeLandlock": lambda launcher: "full",
            "landlockLauncher": "/fake/landlock-run",
        })
        out = provider.confine(["true"], {"mode": "read-only", "workspaceRoot": "/ws"})
        # 上游 entry/index.ts:96-97：`--ro <path>` / `--rw <path>`
        self.assertEqual(out["argv"][0], "/fake/landlock-run")
        self.assertIn("--ro", out["argv"])
        self.assertIn("--rw", out["argv"])
        self.assertEqual(out["enforcement"], "full")

    def test_landlock_partial_enforcement(self):
        provider = LocalSandboxProvider(internals={
            "platform": "linux",
            "probeBwrap": lambda: False,
            "probeLandlock": lambda launcher: "partial",
        })
        out = provider.confine(["true"], {"mode": "read-only", "workspaceRoot": "/ws"})
        self.assertEqual(out["enforcement"], "partial")
        self.assertEqual(out["runnerFailureRules"], RUNNER_FAILURE_RULES["landlock"])

    def test_linux_chain_all_unusable_fails_closed(self):
        provider = LocalSandboxProvider(internals={
            "platform": "linux",
            "probeBwrap": lambda: False,
            "probeLandlock": lambda launcher: "unusable",
        })
        with self.assertRaises(SandboxUnavailableError) as caught:
            provider.confine(["true"], {"mode": "read-only", "workspaceRoot": "/ws"})
        self.assertEqual(caught.exception.code, SANDBOX_UNAVAILABLE)

    def test_darwin_sole_candidate_selected_without_probe(self):
        provider = LocalSandboxProvider(internals={"platform": "darwin"})
        out = provider.confine(["true"], {"mode": "read-only", "workspaceRoot": "/ws"})
        self.assertEqual(out["argv"][0], "sandbox-exec")
        self.assertEqual(out["enforcement"], "full")
        self.assertEqual(out["denialSignatures"], DENIAL_SIGNATURES["seatbelt"])

    def test_win32_sole_candidate_partial(self):
        provider = LocalSandboxProvider(internals={"platform": "win32"})
        out = provider.confine(["echo", "hi"], {"mode": "read-only", "workspaceRoot": "C:\\ws"})
        # 缺省 invocation：python -m miniharness.seams.sandbox_windows_acl.runner（Phase C）
        self.assertEqual(out["argv"][0], sys.executable)
        self.assertEqual(out["argv"][1:3], ["-m", WINDOWS_ACL_RUNNER_MODULE])
        self.assertEqual(out["argv"][3:5], ["--workspace", "C:\\ws"])
        self.assertIn("--mode", out["argv"])
        self.assertEqual(out["enforcement"], "partial")
        self.assertEqual(out["runnerFailureRules"], RUNNER_FAILURE_RULES["windows-acl"])

    def test_windows_acl_runner_args_basic(self):
        # 基础形态恒为三参数（agentless/read-only 分支；会话授权走 provider）
        args = windows_acl_runner_args(["node", "runner.js"], {
            "mode": "workspace-write", "workspaceRoot": "C:\\ws",
            "sessionId": "sess-1", })
        self.assertEqual(args[:2], ["node", "runner.js"])
        self.assertNotIn("--write-sid", args)
        self.assertNotIn("--temp-write-sid", args)

    def test_windows_acl_runner_invocation_override(self):
        provider = LocalSandboxProvider(
            internals={"platform": "win32",
                       "windowsAclRunnerArgs": ["node", "runner.js"]})
        out = provider.confine(["true"], {"mode": "read-only", "workspaceRoot": "C:\\ws"})
        self.assertEqual(out["argv"][:4],
                         ["node", "runner.js", "--workspace", "C:\\ws"])

    def test_unknown_platform_fails_closed(self):
        provider = LocalSandboxProvider(internals={"platform": "plan9"})
        with self.assertRaises(SandboxUnavailableError):
            provider.confine(["true"], {"mode": "read-only", "workspaceRoot": "/ws"})

    def test_windows_acl_probe_without_runner_is_false(self):
        self.assertFalse(probe_windows_acl([]))

    def test_verdict_cached(self):
        calls = {"n": 0}
        provider = LocalSandboxProvider(internals={
            "platform": "linux",
            "probeBwrap": lambda: (calls.__setitem__("n", calls["n"] + 1) or True),
        })
        provider.confine(["true"], {"mode": "read-only", "workspaceRoot": "/ws"})
        provider.confine(["true"], {"mode": "read-only", "workspaceRoot": "/ws"})
        self.assertEqual(calls["n"], 1)


# ==================== 2) 凭据多来源 ====================

class TestCredentialsParsing(unittest.TestCase):
    def test_parse_document_strict(self):
        # version-1 布局（rc.2）：refs 段准入规则不变
        self.assertEqual(parse_credentials_document(
            '{"version": 1, "refs": {"api_key": "sk-1"}}', "f"),
            {"refs": {"api_key": "sk-1"}, "records": {}})
        with self.assertRaises(TypeError):
            parse_credentials_document('["a"]', "f")
        with self.assertRaises(ValueError):
            parse_credentials_document(
                '{"version": 1, "refs": {"bad key!": "v"}}', "f")
        with self.assertRaises(TypeError):
            parse_credentials_document(
                '{"version": 1, "refs": {"api_key": 42}}', "f")
        with self.assertRaises(ValueError):
            parse_credentials_document(
                '{"version": 1, "refs": {"api_key": ""}}', "f")
        # 重复键是解析错误（上游 uniqueKeys:true，credentials-local index.ts:160）
        with self.assertRaisesRegex(ValueError, "duplicate key"):
            parse_credentials_document(
                '{"version": 1, "refs": {"api_key": "a", "api_key": "b"}}', "f")

    def test_parse_document_versioning(self):
        # 空（或纯空白）文档 = 空存储，无需 version
        self.assertEqual(parse_credentials_document("", "f"),
                         {"refs": {}, "records": {}})
        self.assertEqual(parse_credentials_document("   \n", "f"),
                         {"refs": {}, "records": {}})
        # 非空无 version = pre-release flat 布局 → 拒读并指路（措辞逐字）
        with self.assertRaisesRegex(
                ValueError,
                r"uses the pre-release flat layout\. Add `version: 1` and nest "
                r"the existing 2 entries under `refs:`\. No values need to change\."):
            parse_credentials_document('{"a": "1", "b": "2"}', "f")
        # 单条目时 entry 用单数
        with self.assertRaisesRegex(ValueError, "the existing 1 entry under"):
            parse_credentials_document('{"a": "1"}', "f")
        # version 不符 fail-closed
        with self.assertRaisesRegex(ValueError,
                                    r'declares version 2; this build reads version 1'):
            parse_credentials_document('{"version": 2, "refs": {}}', "f")
        # 未知顶层键 fail-closed
        with self.assertRaisesRegex(ValueError, 'unknown top-level key "extra"'):
            parse_credentials_document('{"version": 1, "refs": {}, "extra": {}}', "f")

    def test_parse_records_admission(self):
        # 合法 api-key / grant 记录原样保留
        document = parse_credentials_document(json.dumps({
            "version": 1,
            "refs": {},
            "records": {
                "openai/route-a": {"kind": "api-key", "key": "sk-x",
                                   "env": {"AWS_PROFILE": "prod"}},
                "my-plugin/cache": {"kind": "grant", "payload": {"nested": [1, True, None]}},
            },
        }), "f")
        self.assertEqual(document["records"]["openai/route-a"],
                         {"kind": "api-key", "key": "sk-x", "env": {"AWS_PROFILE": "prod"}})
        self.assertEqual(document["records"]["my-plugin/cache"],
                         {"kind": "grant", "payload": {"nested": [1, True, None]}})
        # 坏键语法 / 缺 kind / 未知 kind / 未知字段 / 无 payload 全部拒绝
        for bad in ('{"version": 1, "records": {"noseparator": {"kind": "grant", "payload": 1}}}',
                    '{"version": 1, "records": {"a/b": {}}}',
                    '{"version": 1, "records": {"a/b": {"kind": "mystery", "payload": 1}}}',
                    '{"version": 1, "records": {"a/b": {"kind": "grant", "payload": 1, "x": 2}}}',
                    '{"version": 1, "records": {"a/b": {"kind": "grant"}}}',
                    '{"version": 1, "records": {"a/b": {"kind": "api-key", "key": ""}}}'):
            with self.assertRaises(Exception):
                parse_credentials_document(bad, "f")

    def test_render_flat_layout_migration(self):
        # 可识别：非空映射、无 version、可寻址键、非空字符串值 → 换布局保值
        migrated = render_flat_layout_migration('{"api_key": "sk-1", "other": "v"}')
        self.assertEqual(json.loads(migrated),
                         {"version": 1, "refs": {"api_key": "sk-1", "other": "v"}})
        # 不可识别一律 None（响亮拒绝继续成立）
        self.assertIsNone(render_flat_layout_migration('{}'))
        self.assertIsNone(render_flat_layout_migration('{"version": 1, "refs": {}}'))
        self.assertIsNone(render_flat_layout_migration('{"bad key!": "v"}'))
        self.assertIsNone(render_flat_layout_migration('{"a": ""}'))
        self.assertIsNone(render_flat_layout_migration('not json'))

    def test_parse_dotenv(self):
        text = "# comment\n\nAPI_KEY=sk-123\nEMPTY=\nQUOTED='hello world'\n"
        self.assertEqual(parse_dotenv(text),
                         {"API_KEY": "sk-123", "QUOTED": "hello world"})


class TestLocalCredentialProvider(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dsh_home = os.path.join(self._tmp.name, "dsh")
        self._project = os.path.join(self._tmp.name, "proj")
        os.makedirs(self._project, exist_ok=True)
        self._filename = os.path.join(self._dsh_home, ".credentials.json")
        self._provider = LocalCredentialProvider(
            filename=self._filename, dsh_home=self._dsh_home,
            project_dir=self._project)
        self._saved_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        self._tmp.cleanup()

    def _write_dotenv(self, path, entries):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            for key, value in entries.items():
                handle.write(f"{key}={value}\n")

    def test_resolve_none_when_unconfigured(self):
        self.assertIsNone(self._provider.resolve("api_key"))

    def test_env_wins_over_everything(self):
        self._write_dotenv(os.path.join(self._project, ".env"), {"api_key": "proj-key"})
        os.environ["api_key"] = "env-key"
        value, source = self._provider.resolve("api_key")
        self.assertEqual((value, source), ("env-key", "env"))

    def test_file_beats_project_env(self):
        self._write_dotenv(os.path.join(self._project, ".env"), {"api_key": "proj-key"})
        self._provider.set("api_key", "stored-key")
        value, source = self._provider.resolve("api_key")
        self.assertEqual((value, source), ("stored-key", "file"))

    def test_project_env_beats_user_env(self):
        self._write_dotenv(os.path.join(self._project, ".env"), {"api_key": "proj-key"})
        self._write_dotenv(os.path.join(self._dsh_home, ".env"), {"api_key": "user-key"})
        value, source = self._provider.resolve("api_key")
        self.assertEqual((value, source), ("proj-key", "project-env"))

    def test_user_env_last_resort(self):
        self._write_dotenv(os.path.join(self._dsh_home, ".env"), {"api_key": "user-key"})
        value, source = self._provider.resolve("api_key")
        self.assertEqual((value, source), ("user-key", "user-env"))

    def test_describe(self):
        self.assertEqual(self._provider.describe("api_key"),
                         {"configured": False, "writable": True})
        os.environ["api_key"] = "env-key"
        self.assertEqual(self._provider.describe("api_key"),
                         {"configured": True, "source": "env", "writable": False})
        del os.environ["api_key"]
        self._provider.set("api_key", "stored-key")
        self.assertEqual(self._provider.describe("api_key"),
                         {"configured": True, "source": "file", "writable": True})

    def test_set_unset_lifecycle(self):
        self._provider.set("api_key", "sk-1")
        self.assertEqual(self._provider.resolve("api_key"), ("sk-1", "file"))
        self._provider.unset("api_key")
        self.assertIsNone(self._provider.resolve("api_key"))
        self._provider.unset("never-existed")  # no-op

    def test_set_rejects_empty(self):
        with self.assertRaises(ValueError):
            self._provider.set("api_key", "")

    def test_set_rejected_when_env_shadows(self):
        os.environ["api_key"] = "env-key"
        with self.assertRaises(ValueError):
            self._provider.set("api_key", "stored-key")
        with self.assertRaises(ValueError):
            self._provider.unset("api_key")

    def test_write_preserves_external_edits(self):
        self._provider.set("a", "1")
        with open(self._filename, "w", encoding="utf-8") as handle:
            json.dump({"version": 1,
                       "refs": {"a": "1", "external": "kept"}}, handle)
        self._provider.set("b", "2")
        self.assertEqual(self._provider.resolve("external"), ("kept", "file"))
        self.assertEqual(self._provider.resolve("b"), ("2", "file"))

    def test_invalid_document_at_boot_fails_loud(self):
        os.makedirs(self._dsh_home, exist_ok=True)
        with open(self._filename, "w", encoding="utf-8") as handle:
            handle.write('{"api_key": 42}')
        # CI umask 022 会让新建文件 0644，先收紧为 0600 再测文档解析失败
        os.chmod(self._filename, 0o600)
        # 值类型不可识别 → 迁移器拒绝改写 → 落回 flat 布局响亮拒绝
        with self.assertRaisesRegex(ValueError, "pre-release flat layout"):
            LocalCredentialProvider(filename=self._filename, dsh_home=self._dsh_home,
                                    project_dir=self._project)

    def test_legacy_flat_document_migrated_at_boot(self):
        # rc.2：可识别 flat 文档启动时自动迁移（持锁重读换布局，值逐字保留）
        os.makedirs(self._dsh_home, exist_ok=True)
        with open(self._filename, "w", encoding="utf-8") as handle:
            handle.write('{"api_key": "sk-old"}')
        os.chmod(self._filename, 0o600)
        provider = LocalCredentialProvider(filename=self._filename, dsh_home=self._dsh_home,
                                           project_dir=self._project)
        self.assertEqual(provider.resolve("api_key"), ("sk-old", "file"))
        with open(self._filename, encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk, {"version": 1, "refs": {"api_key": "sk-old"}})

    def test_persisted_document_survives_reload(self):
        self._provider.set("api_key", "sk-1")
        reloaded = LocalCredentialProvider(filename=self._filename, dsh_home=self._dsh_home,
                                           project_dir=self._project)
        self.assertEqual(reloaded.resolve("api_key"), ("sk-1", "file"))

    def test_external_records_preserved_through_ref_writes(self):
        # 外部写入的合法记录在 refs 写路径中原样保留（写 refs 时 records 不丢）
        self._provider.set("api_key", "sk-1")
        with open(self._filename, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "refs": {"api_key": "sk-1"},
                       "records": {"my-plugin/cache":
                                       {"kind": "grant", "payload": {"n": 1}}}}, handle)
        os.chmod(self._filename, 0o600)
        self._provider.set("other", "v")
        with open(self._filename, encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk["records"]["my-plugin/cache"],
                         {"kind": "grant", "payload": {"n": 1}})
        self.assertEqual(on_disk["refs"], {"api_key": "sk-1", "other": "v"})

    def test_world_readable_rejected_on_posix(self):
        import unittest.mock as mock
        with mock.patch("miniharness.seams.credentials_local.os.name", "posix"):
            with self.assertRaises(ValueError):
                _assert_owner_only("missing-file", lambda p: type("S", (), {
                    "st_mode": 0o644})( ))

    def test_owner_only_passes(self):
        import unittest.mock as mock
        with mock.patch("miniharness.seams.credentials_local.os.name", "posix"):
            _assert_owner_only("f", lambda p: type("S", (), {"st_mode": 0o600})())


# ==================== 2b) 凭据跨进程写锁（对齐 dsh-atomic-write withFileLock） ====================

class TestCredentialWriterLock(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dsh_home = os.path.join(self._tmp.name, "dsh")
        self._filename = os.path.join(self._dsh_home, ".credentials.json")
        self._lock_path = self._filename + ".lock"

    def tearDown(self):
        self._tmp.cleanup()

    def _provider(self):
        return LocalCredentialProvider(filename=self._filename, dsh_home=self._dsh_home,
                                       project_dir=os.path.join(self._tmp.name, "proj"))

    def test_lock_is_sibling_and_released_after_write(self):
        provider = self._provider()
        provider.set("api_key", "sk-1")
        # 兄弟锁路径形状（上游 atomic-write index.ts:97 `${filename}.lock`）
        self.assertEqual(self._lock_path,
                         os.path.join(self._dsh_home, ".credentials.json.lock"))
        # 写完即释放：另一把锁可立即获得
        FileLock(self._lock_path, timeout=0.5).acquire(timeout=0.5)

    def test_contended_write_fails_loud_with_upstream_wording(self):
        provider = self._provider()
        holder = FileLock(self._lock_path)
        holder.acquire()
        try:
            with mock.patch.object(credentials_local, "DOCUMENT_LOCK_WAIT_SECONDS", 0.2):
                with self.assertRaises(CredentialWriteLocked) as caught:
                    provider.set("api_key", "sk-1")
            self.assertIn("atomic-write: timed out waiting for the writer lock at",
                          str(caught.exception))
            self.assertIn(".credentials.json.lock", str(caught.exception))
            # 超时的竞争者绝不移除既有锁（上游 atomic-write index.ts:87）
            self.assertTrue(holder.is_locked)
        finally:
            holder.release()

    def test_interleaved_writers_fold_instead_of_clobber(self):
        pa, pb = self._provider(), self._provider()
        pa.set("a", "1")
        pb.set("b", "2")
        pa.set("c", "3")
        reloaded = self._provider()
        self.assertEqual(reloaded.values, {"a": "1", "b": "2", "c": "3"})


class TestCredentialRecords(unittest.TestCase):
    """P2-20 服务侧记录 API：键语法 + 记录五件套（上游 credentials-local
    index.ts modifyRecord/deleteRecord/readRecord/describeRecord/listRecords）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dsh_home = os.path.join(self._tmp.name, "dsh")
        self._project = os.path.join(self._tmp.name, "proj")
        os.makedirs(self._project, exist_ok=True)
        self._filename = os.path.join(self._dsh_home, ".credentials.json")
        self._saved_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        self._tmp.cleanup()

    def _provider(self):
        return LocalCredentialProvider(filename=self._filename, dsh_home=self._dsh_home,
                                       project_dir=os.path.join(self._tmp.name, "proj"))

    def test_credential_key_grammar(self):
        # 品牌/解析：合法两段往返；段非法与非两段都 TypeError 逐字对齐上游
        key = "llm-pi-ai/openai-codex"
        self.assertEqual(credential_key("llm-pi-ai", "openai-codex"), key)
        self.assertEqual(parse_credential_key(key), key)
        self.assertEqual(credential_key_scope(key), "llm-pi-ai")
        self.assertEqual(credential_key_id(key), "openai-codex")
        with self.assertRaisesRegex(TypeError, r'credential key "x/y/z" must be "<scope>/<id>"'):
            parse_credential_key("x/y/z")
        with self.assertRaisesRegex(TypeError, r'^credential key segment "OpenAI" must match'):
            credential_key("OpenAI", "codex")
        self.assertTrue(is_credential_key_segment("llm-pi-ai"))
        self.assertFalse(is_credential_key_segment("OpenAI"))

    def test_credential_ref_grammar(self):
        # 引用名走 POSIX 标识符（REF_PATTERN）
        self.assertTrue(is_credential_ref_name("AWS_PROFILE"))
        self.assertFalse(is_credential_ref_name("not a name"))
        self.assertEqual(credential_ref("PROVIDER"), "PROVIDER")
        with self.assertRaisesRegex(TypeError, r'^credential ref "not a name" must match'):
            credential_ref("not a name")

    def test_read_record_roundtrip_and_persistence(self):
        provider = self._provider()
        provider.modify_record("my-plugin/cache", lambda _: {"kind": "grant",
                                                            "payload": {"nested": [1, True, None]}})
        provider.modify_record("openai/route-a", lambda _: {
            "kind": "api-key", "key": "sk-x", "env": {"AWS_PROFILE": "prod"}})
        # 读：原样返回存储 dict
        self.assertEqual(provider.read_record("my-plugin/cache"),
                         {"kind": "grant", "payload": {"nested": [1, True, None]}})
        self.assertEqual(provider.read_record("openai/route-a"),
                         {"kind": "api-key", "key": "sk-x", "env": {"AWS_PROFILE": "prod"}})
        self.assertIsNone(provider.read_record("never/seen"))
        # 持久化：重新加载文档后仍在（写 refs 也不丢 records）
        reloaded = self._provider()
        self.assertEqual(reloaded.read_record("my-plugin/cache"),
                         {"kind": "grant", "payload": {"nested": [1, True, None]}})
        reloaded.set("api_key", "sk-1")
        again = self._provider()
        self.assertEqual(again.read_record("openai/route-a"),
                         {"kind": "api-key", "key": "sk-x", "env": {"AWS_PROFILE": "prod"}})
        # records 只读视图
        self.assertEqual(reloaded.records["openai/route-a"],
                         {"kind": "api-key", "key": "sk-x", "env": {"AWS_PROFILE": "prod"}})

    def test_describe_record_presence_semantics(self):
        provider = self._provider()
        # 未存 = configured False；writable 恒真（记录没有更高分层）
        self.assertEqual(provider.describe_record("never/seen"),
                         {"configured": False, "writable": True})
        provider.modify_record("my-plugin/cache", lambda _: {"kind": "grant", "payload": 1})
        self.assertEqual(provider.describe_record("my-plugin/cache"),
                         {"configured": True, "kind": "grant", "writable": True})
        provider.modify_record("openai/route-a", lambda _: {
            "kind": "api-key", "key": "sk-x", "env": {}})
        self.assertEqual(provider.describe_record("openai/route-a"),
                         {"configured": True, "kind": "api-key", "writable": True})

    def test_list_records_enumerates_keys_and_kinds_only(self):
        provider = self._provider()
        self.assertEqual(provider.list_records(), [])
        provider.modify_record("openai/route-a", lambda _: {
            "kind": "api-key", "key": "sk-x", "env": {}})
        provider.modify_record("my-plugin/cache", lambda _: {"kind": "grant", "payload": 1})
        self.assertEqual(provider.list_records(),
                         [{"key": "openai/route-a", "kind": "api-key"},
                          {"key": "my-plugin/cache", "kind": "grant"}])

    def test_modify_record_decline_returns_current_without_writing(self):
        provider = self._provider()
        # 未存记录上的拒绝：不产生记录、返回 None
        self.assertIsNone(provider.modify_record("a/b", lambda _: None))
        self.assertEqual(provider.list_records(), [])
        provider.modify_record("a/b", lambda _: {"kind": "grant", "payload": 1})
        # 已存记录上的拒绝：返回当前、磁盘不写
        current = provider.modify_record("a/b", lambda _: None)
        self.assertEqual(current, {"kind": "grant", "payload": 1})
        reloaded = self._provider()
        self.assertEqual(reloaded.read_record("a/b"), {"kind": "grant", "payload": 1})

    def test_modify_record_updates_existing_with_mutate_snapshot(self):
        provider = self._provider()
        provider.modify_record("a/b", lambda _: {"kind": "grant", "payload": 1})
        updated = provider.modify_record("a/b", lambda _: {"kind": "grant", "payload": 2})
        self.assertEqual(updated, {"kind": "grant", "payload": 2})
        self.assertEqual(provider.read_record("a/b"), {"kind": "grant", "payload": 2})
        self.assertEqual(self._provider().read_record("a/b"), {"kind": "grant", "payload": 2})

    def test_modify_record_refuses_unstorable_records(self):
        provider = self._provider()
        bad_mutations = [
            # 未知 kind
            (lambda _: {"kind": "mystery"}, TypeError),
            # grant payload 不可 JSON 化（循环引用）
            (lambda _: {"kind": "grant", "payload": fixture_cyclic()}, (TypeError, ValueError)),
            # api-key 空 key
            (lambda _: {"kind": "api-key", "key": ""}, TypeError),
            # api-key env 名非法
            (lambda _: {"kind": "api-key", "env": {"not a name": "v"}}, TypeError),
            # api-key env 值空
            (lambda _: {"kind": "api-key", "key": "sk", "env": {"PROVIDER": ""}}, TypeError),
        ]
        for mutate, exc in bad_mutations:
            with self.assertRaises(exc):
                provider.modify_record("a/b", mutate)
        # 全部拒绝后什么都没写
        self.assertEqual(provider.list_records(), [])
        self.assertEqual(self._provider().list_records(), [])

    def test_modify_record_invalid_kind_payload_never_written(self):
        # 与 .refuses 视角互补：拒绝发生在锁内 reconcile 之外，磁盘仍完好
        provider = self._provider()
        provider.modify_record("a/b", lambda _: {"kind": "grant", "payload": 1})
        payload = fixture_cyclic()
        with self.assertRaises(Exception):
            provider.modify_record("a/b", lambda _: {"kind": "grant", "payload": payload})
        self.assertEqual(self._provider().read_record("a/b"), {"kind": "grant", "payload": 1})

    def test_modify_record_reconciles_external_edits_under_lock(self):
        # 另一个 provider 落盘后，本 provider 的 mutate 看到的是磁盘现状（reconcile）
        pa, pb = self._provider(), self._provider()
        pb.modify_record("a/b", lambda _: {"kind": "grant", "payload": 1})
        seen = []
        pa.modify_record("c/d", lambda current: (seen.append(dict(current) if current else None),
                                                 {"kind": "grant", "payload": 2})[1])
        # mutate(current) 收到磁盘折叠后的 None？不——c/d 并不存在；验证刷新后再写
        self.assertEqual(pa.read_record("c/d"), {"kind": "grant", "payload": 2})
        self.assertEqual(pa.read_record("a/b"), {"kind": "grant", "payload": 1})

    def test_delete_record(self):
        provider = self._provider()
        provider.modify_record("a/b", lambda _: {"kind": "grant", "payload": 1})
        provider.delete_record("a/b")
        self.assertIsNone(provider.read_record("a/b"))
        self.assertEqual(self._provider().list_records(), [])
        # 删除不存在的记录是 no-op
        provider.delete_record("never/seen")
        self.assertEqual(provider.list_records(), [])

    def test_record_write_requires_valid_key(self):
        provider = self._provider()
        for method in ("read_record", "describe_record", "modify_record", "delete_record"):
            with self.assertRaisesRegex(TypeError, r'credential key "x/y/z" must be "<scope>/<id>"'):
                getattr(provider, method)("x/y/z", lambda _: None) if method == "modify_record" \
                    else getattr(provider, method)("x/y/z")

    # ---- B 档（2026-08-31）：读侧 mtime 热重载（无文件 watch：外部编辑即时感知） ----

    def test_external_ref_edit_hot_reloaded_on_read(self):
        # 本 provider 不写任何东西，仅读——读侧 mtime 探测应折叠进外部进程的编辑
        pa = self._provider()
        pa.set("api_key", "pa-key")
        pb = self._provider()
        pb.set("api_key", "pb-key")
        self.assertEqual(pa.resolve("api_key"), ("pb-key", "file"))
        self.assertEqual(pa.describe("api_key"),
                         {"configured": True, "source": "file", "writable": True})

    def test_external_record_edit_hot_reloaded_on_read(self):
        pa = self._provider()
        pa.modify_record("a/b", lambda _: {"kind": "grant", "payload": 1})
        pb = self._provider()
        pb.modify_record("a/b", lambda _: {"kind": "grant", "payload": 2})
        self.assertEqual(pa.read_record("a/b"), {"kind": "grant", "payload": 2})
        self.assertEqual(pa.describe_record("a/b"),
                         {"configured": True, "kind": "grant", "writable": True})
        self.assertEqual(pa.list_records(), [{"key": "a/b", "kind": "grant"}])

    def test_external_delete_clears_on_read(self):
        pa = self._provider()
        pa.set("api_key", "sk-1")
        os.remove(self._filename)
        # 文件被外部删除 → 读侧清空为空存储（删掉的条目绝不在内存残留）
        self.assertIsNone(pa.resolve("api_key"))

    def test_unchanged_read_does_not_reload(self):
        pa = self._provider()
        pa.set("api_key", "sk-1")
        before = (pa._mtime_ns, pa._size)
        with mock.patch.object(pa, "_reload_values", wraps=pa._reload_values) as reloaded:
            pa.resolve("api_key")
        reloaded.assert_not_called()
        self.assertEqual((pa._mtime_ns, pa._size), before)


def fixture_cyclic():
    value = {}
    value["self"] = value
    return value


# ==================== 3) 子 agent 远程三通道 ====================

def _make_fork_loop(system_prompt, seed):
    session = Session(f"fork-{id(seed)}", seed=seed)
    ctx = Context()
    reg = ToolRegistry(ctx)
    reg.register(Tool(name="bash", description="d", execute=lambda a, e: "ok"))
    return AgentLoop(session, FakeLlmAdapter(final_text=f"子任务完成（{system_prompt[:4]}）"),
                     reg, ctx, system_prompt=system_prompt)


def _turn_ended(loop):
    return any(e["type"] == "turn/end" for e in loop.session.events)


class TestCompletedTurnPrefix(unittest.TestCase):
    def test_empty_events(self):
        self.assertEqual(completed_turn_prefix(Session("s").events), [])

    def test_unbalanced_turn_gives_empty_prefix(self):
        # 只有 turn/start（回合未平衡）→ 前缀为空（上游 completedTurnPrefix 同语义）
        session = Session("s")
        session.append("turn/start")
        self.assertEqual(completed_turn_prefix(session.events), [])

    def test_prefix_ends_at_last_turn_end(self):
        session = Session("s")
        ctx = Context()
        reg = ToolRegistry(ctx)
        reg.register(Tool(name="bash", description="d", execute=lambda a, e: "ok"))
        loop = AgentLoop(session, FakeLlmAdapter(final_text="回答"), reg, ctx)
        loop.run("第一轮")
        loop.followup("第二轮")
        loop.run("第二轮")
        events = session.events
        prefix = completed_turn_prefix(events)
        self.assertEqual(prefix[-1]["type"], "turn/end")
        self.assertEqual(prefix[0]["seq"], 0)
        # 前缀长度 = 最后一个 turn/end 的 seq + 1（seq 即下标）
        last_end = max(i for i, e in enumerate(events) if e["type"] == "turn/end")
        self.assertEqual(len(prefix), last_end + 1)


class TestForkChannel(unittest.TestCase):
    def test_fork_seeds_child_with_parent_prefix(self):
        parent = AgentLoop(Session("parent"), FakeLlmAdapter(final_text="父回答"),
                           ToolRegistry(Context()), Context())
        parent.run("父回合")
        provider = ForkSubAgentProvider(_make_fork_loop)
        child = provider.spawn("researcher", "研究员", parent=parent)
        # 子会话以父前缀开头：seq 0 连续 + 自动补 session/end-seed 标记
        child_events = child._loop.session.events
        prefix = completed_turn_prefix(parent.session.events)
        # 子会话以父前缀开头：逐条内容一致（冻结 vs 解冻仅形态不同）
        for child_event, parent_event in zip(child_events[:len(prefix)], prefix):
            self.assertEqual(thaw(child_event), parent_event)
        self.assertEqual(child_events[len(prefix)]["type"], "session/end-seed")
        out = child.run("子任务")
        self.assertIn("子任务完成", out)
        self.assertTrue(_turn_ended(child._loop))

    def test_fork_without_parent_starts_fresh(self):
        provider = ForkSubAgentProvider(_make_fork_loop)
        child = provider.spawn("coder", "程序员")
        out = child.run("写个函数")
        self.assertIn("子任务完成", out)
        child_events = child._loop.session.events
        # V2：空 seed（[]）构造同样补记 end-seed 边界标记（{}，不带 inherited 键
        # ——上游 types.ts 仅允许可选 true），输入先落其后的 agent/inbox/spliced，
        # 回合从随后的 turn/start 起
        self.assertEqual(child_events[0]["type"], "session/end-seed")
        self.assertEqual(child_events[0]["data"], {})
        self.assertEqual(child_events[1]["type"], "agent/inbox/spliced")
        self.assertIn("turn/start", [e["type"] for e in child_events])


class TestAcpChannel(unittest.TestCase):
    def test_acp_child_roundtrip(self):
        provider = AcpSubAgentProvider(permission="reject")
        child = provider.spawn("researcher", "你是一个研究员", cwd=os.getcwd())
        try:
            out = child.run("查一下资料")
            self.assertEqual(out, "任务完成。")
            self.assertEqual(child.stop_reason, "end_turn")
        finally:
            child.close()

    def test_acp_child_multi_turn_same_session(self):
        provider = AcpSubAgentProvider()
        child = provider.spawn("researcher", "你是一个研究员", cwd=os.getcwd())
        try:
            first = child.run("第一问")
            second = child.run("第二问")
            self.assertEqual(first, "任务完成。")
            self.assertEqual(second, "任务完成。")
            self.assertEqual(child.stop_reason, "end_turn")
        finally:
            child.close()

    def test_acp_child_streams_per_update_session_notifications(self):
        provider = AcpSubAgentProvider()
        child = provider.spawn("researcher", "你是一个研究员", cwd=os.getcwd())
        try:
            before = list(child._client.notifications)
            child.run("查一下")
            updates = [
                (m, params) for m, params in child._client.notifications[before.__len__():]
                if m == "session/update"
            ]
            self.assertTrue(updates)
            # 每条 update 一个 session/update 通知（对齐上游逐块流式粒度）
            self.assertTrue(all(
                params.get("sessionId") == child._session_id
                for _, params in updates))
            chunk = next(
                (params["update"] for _, params in updates
                 if params["update"].get("sessionUpdate") == "agent_message_chunk"),
                None)
            self.assertIsNotNone(chunk)
            self.assertEqual(chunk["content"], {"type": "text", "text": "任务完成。"})
        finally:
            child.close()

    def test_acp_provider_validates_permission(self):
        with self.assertRaises(ValueError):
            AcpSubAgentProvider(permission="bogus")

    def test_acp_child_process_exits_cleanly(self):
        provider = AcpSubAgentProvider()
        child = provider.spawn("r", "研究员", cwd=os.getcwd())
        child.run("任务")
        child.close()
        self.assertEqual(child._client._proc.returncode, 0)


class TestSdkChannel(unittest.TestCase):
    def test_sdk_child_roundtrip(self):
        provider = SdkSubAgentProvider()
        child = provider.spawn("coder", "你是一个程序员", cwd=os.getcwd())
        try:
            out = child.run("写个函数")
            self.assertEqual(out, "任务完成。")
            self.assertIsInstance(child.message_id, str)
            self.assertTrue(len(child.message_id) > 8)
        finally:
            child.close()

    def test_sdk_child_session_reuse(self):
        provider = SdkSubAgentProvider()
        child = provider.spawn("coder", "你是一个程序员", cwd=os.getcwd())
        try:
            child.run("第一问")
            child.run("第二问")
            self.assertNotEqual(child.message_id, "")
            self.assertTrue(len(child.message_id) > 8)
        finally:
            child.close()

    def test_sdk_child_process_exits_cleanly(self):
        provider = SdkSubAgentProvider()
        child = provider.spawn("c", "程序员", cwd=os.getcwd())
        child.run("任务")
        child.close()
        self.assertEqual(child._client._proc.returncode, 0)

    def test_sdk_worker_round_events_no_history_replay(self):
        """回合级透传不重发历史回合事件（会话复用只透传本次投递后的新事件）。"""
        runtime = _SdkWorkerRuntime()
        runtime.handle("initialize", {"cwd": "."})
        runtime.handle("session/prompt", {
            "sessionId": "s1", "contentBlocks": [{"type": "text", "text": "第一问"}]})
        first = [p["event"]["type"] for m, p in runtime.drain()
                 if m == "session.event"]
        self.assertEqual(first, ["agent/inbox/spliced", "assistant/message", "turn/end"])
        runtime.handle("session/prompt", {
            "sessionId": "s1", "contentBlocks": [{"type": "text", "text": "第二问"}]})
        second = [p["event"] for m, p in runtime.drain()
                  if m == "session.event"]
        second_types = [e["type"] for e in second]
        self.assertEqual(second_types,
                         ["agent/inbox/spliced", "assistant/message", "turn/end"])
        turn_nums = [e["data"]["turn"] for e in second if e["type"] == "turn/end"]
        self.assertEqual(turn_nums, [2])


# ==================== 6) 子进程 env 清洗（对齐 dsh-subprocess scrubbedParentEnv） ====================

class TestScrubbedParentEnv(unittest.TestCase):
    AMBIENT = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "DEEPSEEK_API_KEY": "sk-ambient",
        "MY_PASSWORD": "p",
        "top_secret": "s",
        "ACCESS_TOKEN": "t",
        "DSH_PERMISSION_MODE": "reject",
        "dsh_stale": "x",
        "MONKEYKEYS": "k",
    }

    def test_drops_credential_shaped_names_case_insensitively(self):
        env = scrubbed_parent_env(self.AMBIENT)
        for name in ("DEEPSEEK_API_KEY", "MY_PASSWORD", "top_secret", "ACCESS_TOKEN",
                     "MONKEYKEYS"):
            self.assertNotIn(name, env)

    def test_drops_dsh_prefix_case_insensitively(self):
        env = scrubbed_parent_env(self.AMBIENT)
        self.assertNotIn("DSH_PERMISSION_MODE", env)
        self.assertNotIn("dsh_stale", env)

    def test_keeps_ordinary_ambient_values(self):
        env = scrubbed_parent_env(self.AMBIENT)
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["HOME"], "/home/u")

    def test_returns_fresh_dict_and_defaults_to_os_environ(self):
        scrubbed = scrubbed_parent_env(self.AMBIENT)
        scrubbed["PATH"] = "mutated"
        self.assertEqual(self.AMBIENT["PATH"], "/usr/bin")
        with mock.patch.dict(os.environ, {"PLAIN": "v", "MY_SECRET": "x"}):
            env = scrubbed_parent_env()
            self.assertEqual(env.get("PLAIN"), "v")
            self.assertNotIn("MY_SECRET", env)

    def test_pattern_shape_matches_upstream_regex(self):
        # 上游 SENSITIVE_ENV_PATTERN = /KEY|PASSWORD|SECRET|TOKEN/i（index.ts:44）
        self.assertTrue(SENSITIVE_ENV_PATTERN.search("DEEPSEEK_API_KEY"))
        self.assertFalse(SENSITIVE_ENV_PATTERN.search("EDITOR"))
        self.assertEqual(DSH_ENV_PREFIX, "DSH_")


class TestSpawnEnvLayering(unittest.TestCase):
    """spawn 的 env 契约：清洗基底 + 显式 env 在 scrub 之后合并（上游 run.ts:123 同款）。"""

    AMBIENT = {"DEEPSEEK_API_KEY": "sk-ambient", "DSH_STALE": "old", "PATH": "/bin"}

    def _capture_popen(self, spawn_call):
        with mock.patch.dict(os.environ, self.AMBIENT), \
                mock.patch("miniharness.seams.subagent.providers.subprocess.Popen",
                           side_effect=RuntimeError("captured")) as popen:
            with self.assertRaises(RuntimeError):
                spawn_call()
        return popen.call_args.kwargs["env"]

    def test_acp_spawn_scrubs_ambient_credentials(self):
        provider = AcpSubAgentProvider()
        env = self._capture_popen(lambda: provider.spawn("n", "p"))
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertNotIn("DSH_STALE", env)
        self.assertEqual(env["PATH"], "/bin")

    def test_acp_spawn_explicit_env_merges_after_scrub(self):
        provider = AcpSubAgentProvider()
        env = self._capture_popen(
            lambda: provider.spawn("n", "p", env={"DEEPSEEK_API_KEY": "deliberate"}))
        # 刻意提供的凭据存活（上游 README.md:32：显式 env 叠加在清洗后的父环境上）
        self.assertEqual(env["DEEPSEEK_API_KEY"], "deliberate")

    def test_sdk_spawn_scrubs_ambient_credentials(self):
        provider = SdkSubAgentProvider()
        env = self._capture_popen(lambda: provider.spawn("n", "p"))
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertNotIn("DSH_STALE", env)

    def test_sdk_spawn_explicit_env_merges_after_scrub(self):
        provider = SdkSubAgentProvider()
        env = self._capture_popen(
            lambda: provider.spawn("n", "p", env={"DSH_CORDIS_CONFIG": "fresh"}))
        self.assertEqual(env["DSH_CORDIS_CONFIG"], "fresh")


if __name__ == "__main__":
    unittest.main()