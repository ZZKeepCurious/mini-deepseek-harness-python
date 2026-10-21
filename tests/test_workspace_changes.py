import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.tools import ToolExec
from miniharness.fs import install_local_fs
from miniharness.workspace_changes import install_workspace_changes
from miniharness.workspace_changes.capture import capture_file, same_capture
from miniharness.workspace_changes.compare import compare_text

ROOT = Path.cwd() / "out"
GIT = shutil.which("git")


def _workdir() -> Path:
    directory = ROOT / "workspace-changes-tests" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    return directory


class CompareTextTest(unittest.TestCase):
    def test_context_lines_share_one_hunk(self):
        before = "\n".join(list("abcdefghij")) + "\n"
        after = "\n".join(list("abcDefghijk")) + "\n"
        result = compare_text(before, after, 100)
        self.assertFalse(result["coarse"])
        self.assertEqual((result["added"], result["deleted"]), (2, 1))
        self.assertEqual(result["hunks"], [{
            "oldStart": 1, "oldLines": 10, "newStart": 1, "newLines": 11,
            "lines": [" a", " b", " c", "-d", "+D", " e", " f", " g", " h",
                      " i", " j", "+k"],
        }])

    def test_distant_changes_split_hunks(self):
        before = "\n".join(list("abcdefghij")) + "\n"
        after = "\n".join(list("abcDefghijk")) + "\n"
        result = compare_text(before + "z\n" * 10 + "end\n",
                              after + "z\n" * 10 + "END\n", 100)
        self.assertEqual(
            [(h["oldStart"], h["oldLines"], h["newStart"], h["newLines"])
             for h in result["hunks"]],
            [(1, 13, 1, 14), (18, 4, 19, 4)])

    def test_missing_side_and_unterminated_last_line(self):
        self.assertEqual(compare_text(None, "x\ny\n", 100)["hunks"], [{
            "oldStart": 1, "oldLines": 0, "newStart": 1, "newLines": 2,
            "lines": ["+x", "+y"]}])
        deleted = compare_text("x\n", None, 100)
        self.assertEqual((deleted["added"], deleted["deleted"]), (0, 1))
        added = compare_text("last", "last\nadded", 100)
        self.assertEqual((added["added"], added["deleted"]), (1, 0))
        self.assertEqual(compare_text("same\n", "same\n", 100),
                         {"hunks": [], "coarse": False, "added": 0, "deleted": 0})
        self.assertEqual(compare_text(None, None, 100),
                         {"hunks": [], "coarse": False, "added": 0, "deleted": 0})

    def test_timeout_degrades_to_whole_file(self):
        before = "\n".join(f"old {index}" for index in range(4000))
        after = "\n".join(f"new {index}" for index in range(4000))
        result = compare_text(before, after, 1)
        self.assertTrue(result["coarse"])
        self.assertEqual((result["added"], result["deleted"]), (4000, 4000))
        self.assertEqual(len(result["hunks"]), 1)
        self.assertEqual(result["hunks"][0]["oldStart"], 1)
        self.assertEqual(result["hunks"][0]["lines"][0], "-old 0")
        self.assertEqual(result["hunks"][0]["lines"][-1], "+new 3999")
        created = compare_text(None, after + "\n" + before, 1)
        self.assertTrue(created["coarse"])
        self.assertEqual(created["hunks"][0]["oldStart"], 1)
        self.assertEqual(created["hunks"][0]["oldLines"], 0)
        self.assertEqual(created["hunks"][0]["newLines"], 8000)


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.directory = _workdir()
        self.addCleanup(lambda: shutil.rmtree(self.directory, ignore_errors=True))

    def test_absent_regular_binary_and_oversized(self):
        missing = capture_file(str(self.directory / "missing"), str(self.directory), 10)
        self.assertEqual(missing, {"kind": "absent"})
        created = self.directory / "a.txt"
        created.write_text("hello\n", encoding="utf-8")
        capture = capture_file(str(created), str(self.directory / "copies"), 10)
        self.assertEqual(capture["kind"], "file")
        self.assertFalse(capture["binary"])
        self.assertTrue(os.path.exists(capture["file"]))
        self.assertFalse(same_capture(missing, capture))
        self.assertTrue(same_capture(capture, capture_file(str(created), str(self.directory / "copies"), 10)))
        binary = self.directory / "b.dat"
        binary.write_bytes(b"\0\1")
        self.assertTrue(capture_file(str(binary), str(self.directory / "copies"), 10)["binary"])
        big = self.directory / "big.bin"
        big.write_bytes(b"x" * 11)
        self.assertEqual(capture_file(str(big), str(self.directory / "copies"), 10),
                         {"kind": "oversized"})
        self.assertEqual(capture_file(str(self.directory), str(self.directory / "copies"), 10), None)


@unittest.skipUnless(GIT, "git is unavailable")
class RecorderTest(unittest.TestCase):
    def setUp(self):
        self.directory = _workdir()
        self.addCleanup(lambda: shutil.rmtree(self.directory, ignore_errors=True))
        self.repo = self.directory / "repo"
        self.scratch = self.directory / "scratch"
        self.repo.mkdir()
        self.scratch.mkdir()
        subprocess.run([GIT, "init", "-q"], cwd=self.repo, check=True)
        subprocess.run([GIT, "config", "core.autocrlf", "false"], cwd=self.repo, check=True)
        (self.repo / "a.txt").write_bytes(b"one\n")

    def _run(self, scenario):
        return asyncio.run(scenario())

    def _boot(self, max_file_bytes=2 * 1024 * 1024):
        ctx = Context()
        install_local_fs(ctx, {"cwd": str(self.repo)})
        service = install_workspace_changes(ctx, {
            "tempRoot": str(self.scratch),
            "maxFileBytes": max_file_bytes,
            "diffTimeoutMs": 1000,
        })
        holder = {}

        def on_append(event):
            ctx.emit("session/event", {"session": holder["session"], "event": event})

        session = Session("workspace-changes-test", meta={"cwd": str(self.repo)},
                          on_append=on_append)
        holder["session"] = session
        return ctx, service, session

    def _events(self, session):
        return [event for event in session.snapshot_events()
                if event["type"] == "workspace/changes"]

    def _exec(self, session):
        return ToolExec(agent=SimpleNamespace(session=session))

    async def _intent(self, ctx, exec_, path):
        await ctx.awaterfall("tools/pre-execute", {
            "tool": "write", "args": {}, "exec": exec_})
        target = await ctx.get("fs").resolve(path, {"cwd": str(self.repo)})
        await ctx.awaterfall("fs/write-intent", (target, exec_))

    def test_git_snapshot_summary_and_diff(self):
        async def scenario():
            ctx, service, session = self._boot()
            session.append("turn/start", {"turn": 1})
            recorder = service._recorders[session.session_id]
            await recorder.settled()
            exec_ = self._exec(session)
            await self._intent(ctx, exec_, "a.txt")
            (self.repo / "a.txt").write_bytes(b"changed\n")
            recorder.observe({"seq": 99, "data": {"turn": 1}})
            await recorder.stopping(1)
            events = self._events(session)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["data"], {"turn": 1})
            summary = service.summary(session.session_id, events[0]["seq"])
            self.assertEqual(summary["turn"], 1)
            self.assertEqual(summary["total"], 1)
            self.assertEqual(summary["added"], 1)
            self.assertEqual(summary["deleted"], 1)
            self.assertEqual(summary["files"][0]["path"], "a.txt")
            self.assertIn("snapshot", summary)
            diff = await service.diff(session.session_id, events[0]["seq"], 0)
            self.assertEqual(diff, {
                "kind": "text", "path": "a.txt", "display": "a.txt",
                "before": True, "after": True, "coarse": False,
                "hunks": [{"oldStart": 1, "oldLines": 1, "newStart": 1,
                           "newLines": 1, "lines": ["-one", "+changed"]}],
            })
            # A second record attempt at turn/end after stopping is skipped.
            recorder.end(1)
            await recorder.settled()
            self.assertEqual(len(self._events(session)), 1)
            return service, session

        self._run(scenario)

    def test_disposal_stops_serving(self):
        async def scenario():
            ctx, service, session = self._boot()
            session.append("turn/start", {"turn": 1})
            recorder = service._recorders[session.session_id]
            await recorder.settled()
            exec_ = self._exec(session)
            await self._intent(ctx, exec_, "a.txt")
            (self.repo / "a.txt").write_bytes(b"changed\n")
            recorder.observe({"seq": 1, "data": {"turn": 1}})
            await recorder.stopping(1)
            seq = self._events(session)[0]["seq"]
            self.assertIsNotNone(service.summary(session.session_id, seq))
            ctx.emit("session/disposed", {"session": session})
            self.assertIsNone(service.summary(session.session_id, seq))
            self.assertIsNone(await service.diff(session.session_id, seq, 0))
            self.assertEqual(service._recorders, {})

        self._run(scenario)

    def test_unknown_coordinates_and_abort(self):
        async def scenario():
            ctx, service, session = self._boot()
            session.append("turn/start", {"turn": 1})
            recorder = service._recorders[session.session_id]
            await recorder.settled()
            exec_ = self._exec(session)
            await self._intent(ctx, exec_, "a.txt")
            (self.repo / "a.txt").write_bytes(b"changed\n")
            recorder.observe({"seq": 1, "data": {"turn": 1}})
            await recorder.stopping(1)
            seq = self._events(session)[0]["seq"]
            self.assertIsNone(await service.diff(session.session_id, seq + 1, 0))
            self.assertIsNone(await service.diff(session.session_id, seq, 9))
            aborted = SimpleNamespace(is_set=lambda: True)
            with self.assertRaises(RuntimeError):
                await service.diff(session.session_id, seq, 0, aborted)

        self._run(scenario)

    def test_non_git_summarizes_captured_edits(self):
        async def scenario():
            base = Path(tempfile.mkdtemp(prefix="workspace-changes-plain-"))
            self.addCleanup(lambda: shutil.rmtree(base, ignore_errors=True))
            plain = base / "plain"
            plain.mkdir()
            ctx = Context()
            install_local_fs(ctx, {"cwd": str(plain)})
            service = install_workspace_changes(ctx, {
                "tempRoot": str(self.scratch), "diffTimeoutMs": 1000})
            holder = {}

            def on_append(event):
                ctx.emit("session/event", {"session": holder["session"], "event": event})

            session = Session("plain", meta={"cwd": str(plain)}, on_append=on_append)
            holder["session"] = session
            session.append("turn/start", {"turn": 1})
            recorder = service._recorders[session.session_id]
            await recorder.settled()
            exec_ = self._exec(session)
            await ctx.awaterfall("tools/pre-execute", {
                "tool": "write", "args": {}, "exec": exec_})
            target = await ctx.get("fs").resolve("new.txt", {"cwd": str(plain)})
            await ctx.awaterfall("fs/write-intent", (target, exec_))
            (plain / "new.txt").write_bytes(b"one\ntwo\n")
            recorder.observe({"seq": 5, "data": {"turn": 1}})
            await recorder.stopping(1)
            event = self._events(session)[0]
            summary = service.summary(session.session_id, event["seq"])
            self.assertEqual(summary["files"][0], {
                "path": "new.txt", "display": "new.txt", "added": 2, "deleted": 0})
            self.assertNotIn("snapshot", summary)
            diff = await service.diff(session.session_id, event["seq"], 0)
            self.assertEqual(diff["before"], False)
            self.assertEqual(diff["after"], True)
            self.assertEqual(diff["hunks"][0]["lines"], ["+one", "+two"])

        self._run(scenario)

    def test_binary_and_oversized_captures(self):
        async def scenario():
            ctx, service, session = self._boot(max_file_bytes=4)
            (self.repo / ".gitignore").write_bytes(b"big.bin\n")
            session.append("turn/start", {"turn": 1})
            recorder = service._recorders[session.session_id]
            await recorder.settled()
            binary_exec = self._exec(session)
            await self._intent(ctx, binary_exec, "bin.dat")
            (self.repo / "bin.dat").write_bytes(b"\0\1")
            big_exec = self._exec(session)
            await self._intent(ctx, big_exec, "big.bin")
            (self.repo / "big.bin").write_bytes(b"012345")
            recorder.observe({"seq": 7, "data": {"turn": 1}})
            await recorder.stopping(1)
            event = self._events(session)[0]
            summary = service.summary(session.session_id, event["seq"])
            by_path = {file["path"]: file for file in summary["files"]}
            self.assertTrue(by_path["bin.dat"]["binary"])
            self.assertTrue(by_path["big.bin"]["oversized"])
            binary_index = next(index for index, file in enumerate(summary["files"])
                                if file["path"] == "bin.dat")
            oversized_index = next(index for index, file in enumerate(summary["files"])
                                   if file["path"] == "big.bin")
            self.assertEqual(await service.diff(session.session_id, event["seq"], binary_index),
                             {"kind": "binary", "path": "bin.dat", "display": "bin.dat"})
            self.assertEqual(await service.diff(session.session_id, event["seq"], oversized_index),
                             {"kind": "oversized", "path": "big.bin", "display": "big.bin"})

        self._run(scenario)

    def test_subagent_and_delegated_sessions_are_skipped(self):
        async def scenario():
            ctx, service, _session = self._boot()
            holder = {}

            def on_append(event):
                ctx.emit("session/event", {"session": holder["session"], "event": event})

            session = Session("child", meta={"cwd": str(self.repo), "origin": "subagent"},
                              on_append=on_append)
            holder["session"] = session
            session.append("turn/start", {"turn": 1})
            self.assertNotIn(session.session_id, service._recorders)
            delegated = Session("delegated", meta={"cwd": str(self.repo), "delegationDepth": 1},
                                on_append=on_append)
            holder["session"] = delegated
            delegated.append("turn/start", {"turn": 1})
            self.assertNotIn(delegated.session_id, service._recorders)

        self._run(scenario)


if __name__ == "__main__":
    unittest.main()
