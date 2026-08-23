"""第 6 章扩展验收：真沙箱后端 / 凭据多来源 / 子 agent 远程三通道。

运行：python -m unittest discover -s tests -t .
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from miniharness.seams.credentials_local import (
    CredentialWriteLocked,
    LocalCredentialProvider,
    _assert_owner_only,
    parse_credentials_document,
    parse_dotenv,
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
        self.assertEqual(out["argv"][:2], ["--workspace", "C:\\ws"])
        self.assertIn("--mode", out["argv"])
        self.assertEqual(out["enforcement"], "partial")
        self.assertEqual(out["runnerFailureRules"], RUNNER_FAILURE_RULES["windows-acl"])

    def test_windows_acl_runner_args_with_session(self):
        args = windows_acl_runner_args(["node", "runner.js"], {
            "mode": "workspace-write", "workspaceRoot": "C:\\ws",
            "sessionId": "sess-1", })
        self.assertEqual(args[:2], ["node", "runner.js"])
        self.assertIn("--write-sid", args)
        self.assertIn("--temp-write-sid", args)

    def test_windows_acl_runner_args_agentless(self):
        args = windows_acl_runner_args(["node", "runner.js"], {
            "mode": "workspace-write", "workspaceRoot": "C:\\ws"})
        self.assertNotIn("--write-sid", args)

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
        self.assertEqual(parse_credentials_document('{"api_key": "sk-1"}', "f"),
                         {"api_key": "sk-1"})
        with self.assertRaises(TypeError):
            parse_credentials_document('["a"]', "f")
        with self.assertRaises(ValueError):
            parse_credentials_document('{"bad key!": "v"}', "f")
        with self.assertRaises(TypeError):
            parse_credentials_document('{"api_key": 42}', "f")
        with self.assertRaises(ValueError):
            parse_credentials_document('{"api_key": ""}', "f")
        # 重复键是解析错误（上游 uniqueKeys:true，credentials-local index.ts:160）
        with self.assertRaisesRegex(ValueError, "duplicate key"):
            parse_credentials_document('{"api_key": "a", "api_key": "b"}', "f")

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
            json.dump({"a": "1", "external": "kept"}, handle)
        self._provider.set("b", "2")
        self.assertEqual(self._provider.resolve("external"), ("kept", "file"))
        self.assertEqual(self._provider.resolve("b"), ("2", "file"))

    def test_invalid_document_at_boot_fails_loud(self):
        os.makedirs(self._dsh_home, exist_ok=True)
        with open(self._filename, "w", encoding="utf-8") as handle:
            handle.write('{"api_key": 42}')
        # CI umask 022 会让新建文件 0644，先收紧为 0600 再测文档解析失败
        os.chmod(self._filename, 0o600)
        with self.assertRaises(TypeError):
            LocalCredentialProvider(filename=self._filename, dsh_home=self._dsh_home,
                                    project_dir=self._project)

    def test_persisted_document_survives_reload(self):
        self._provider.set("api_key", "sk-1")
        reloaded = LocalCredentialProvider(filename=self._filename, dsh_home=self._dsh_home,
                                           project_dir=self._project)
        self.assertEqual(reloaded.resolve("api_key"), ("sk-1", "file"))

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
            with mock.patch.object(credentials_local, "LOCK_TIMEOUT_SECONDS", 0.2):
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
        # 无 seed 时 Session 不补 end-seed 标记；输入先落 agent/inbox/spliced，
        # 回合从随后的 turn/start 起
        self.assertEqual(child_events[0]["type"], "agent/inbox/spliced")
        self.assertIn("turn/start", [e["type"] for e in child_events])
        self.assertNotIn("session/end-seed", [e["type"] for e in child_events])


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