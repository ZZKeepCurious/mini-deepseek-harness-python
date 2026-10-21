"""每会话的回合记录器：快照、文件工具捕获、回合末差异与保留的记录。

上游对照：`packages/deliverables/workspace-changes/src/recorder.ts`。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from functools import cmp_to_key
from typing import Any

from .capture import capture_file, same_capture
from .compare import compare_text
from .git import (
    blob_text,
    diff_trees,
    gitlink_paths,
    ignored_paths,
    locate_git_workspace,
    snapshot_tree,
    tree_blob,
)
from .paths import (
    canonical_path,
    compare_display,
    display_path_of,
    durable_path_of,
    is_inside,
    is_temporary_path,
    temporary_roots,
    to_posix,
)

__all__ = ["TurnRecorder"]

OVERSIZED = object()


class TurnRecorder:
    """串行化一个会话的录制工作；记录随会话销毁。"""

    def __init__(self, session: Any, cwd: str, env: dict, warn: Any):
        self._session = session
        self._cwd = cwd
        self._env = env
        self._warn = warn
        self._tail: asyncio.Task | None = None
        self._disposed = False
        self._state = self._fresh(0)
        self._paths: dict | None = None
        self._repository: dict | None = None
        self._scratch: str | None = None
        self._records: dict[int, dict] = {}

    # ---------- 生命周期 ----------

    @staticmethod
    def _fresh(turn: int) -> dict:
        return {
            "turn": turn,
            "baseline": None,
            "captures": {},
            "lastToolResultSeq": -1,
            "attemptedAfterSeq": -1,
            "recordedAfterSeq": -1,
        }

    def start(self, turn: int) -> None:
        state = self._fresh(turn)
        self._state = state

        async def task() -> None:
            try:
                if self._paths is None:
                    self._paths = {
                        "cwd": canonical_path(self._cwd),
                        "home": canonical_path(os.path.expanduser("~")),
                        "temporaryRoots": temporary_roots(),
                    }
                repository = await asyncio.to_thread(self._locate, self._paths["cwd"])
                if repository is None:
                    return
                tree = await asyncio.to_thread(snapshot_tree, repository["git"],
                                               repository["workspace"])
                state["baseline"] = {**repository, "tree": tree}
            except Exception:
                state["baseline"] = "failed"
                raise

        self._spawn(task)

    def capture(self, path: str) -> dict | None:
        """捕获文件工具即将变更的路径（本回合首次）；返回捕获供测试观察。"""
        state = self._state
        paths = self._paths
        if paths is None or self._disposed:
            return None
        absolute = canonical_path(path)
        if absolute in state["captures"]:
            return state["captures"][absolute]
        capture = capture_file(absolute, self._capture_dir(), self._env["maxFileBytes"])
        if capture is not None:
            state["captures"][absolute] = capture
        return capture

    async def capture_async(self, path: str) -> None:
        await asyncio.to_thread(self.capture, path)

    def observe(self, event: dict) -> None:
        state = self._state
        data = event.get("data") or {}
        if data.get("turn") == state["turn"]:
            state["lastToolResultSeq"] = event["seq"]

    async def stopping(self, turn: int) -> None:
        state = self._state
        if turn != state["turn"]:
            return
        await self._run(lambda: self._record(state))

    def end(self, turn: int) -> None:
        state = self._state
        if turn != state["turn"] or state["attemptedAfterSeq"] >= state["lastToolResultSeq"]:
            return

        async def task() -> None:
            await self._record(state)

        self._spawn(task)

    async def settled(self) -> None:
        tail = self._tail
        if tail is not None:
            try:
                await tail
            except Exception:
                pass

    def summary(self, seq: int) -> dict | None:
        record = self._records.get(seq)
        if record is None:
            return None
        summary = record["summary"]
        return {
            "turn": summary["turn"],
            "cwd": summary["cwd"],
            "files": [dict(file) for file in summary["files"]],
            "total": summary["total"],
            "added": summary["added"],
            "deleted": summary["deleted"],
            **({"snapshot": dict(summary["snapshot"])} if "snapshot" in summary else {}),
        }

    async def diff(self, seq: int, index: int, signal: Any = None) -> dict | None:
        record = self._records.get(seq)
        if record is None or index < 0 or index >= len(record["summary"]["files"]):
            return None
        file = record["summary"]["files"][index]
        sources = record["sources"][index]
        path, display = file["path"], file["display"]
        if "refusal" in sources:
            return {"kind": sources["refusal"], "path": path, "display": display}
        _raise_if_aborted(signal)
        try:
            before = await asyncio.to_thread(self._read_side, sources["before"])
            _raise_if_aborted(signal)
            after = await asyncio.to_thread(self._read_side, sources["after"])
            _raise_if_aborted(signal)
        except Exception:
            if self._disposed:
                return None
            raise
        if before is OVERSIZED or after is OVERSIZED:
            return {"kind": "oversized", "path": path, "display": display}
        comparison = compare_text(before, after, self._env["diffTimeoutMs"])
        return {
            "kind": "text",
            "path": path,
            "display": display,
            "before": before is not None,
            "after": after is not None,
            "hunks": comparison["hunks"],
            "coarse": comparison["coarse"],
        }

    def dispose(self) -> None:
        self._disposed = True
        self._records.clear()
        if self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)

    # ---------- 内部 ----------

    def _spawn(self, task: Any) -> asyncio.Task | None:
        if self._disposed:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._run(task))
            return None
        previous = self._tail

        async def runner() -> None:
            if previous is not None:
                try:
                    await previous
                except Exception:
                    pass
            if self._disposed:
                return
            try:
                await task()
            except Exception as error:
                if not self._disposed:
                    self._warn(f"workspace-changes: {error}")

        spawned = loop.create_task(runner())
        self._tail = spawned
        return spawned

    async def _run(self, task: Any) -> None:
        if self._disposed:
            return
        try:
            await task()
        except Exception as error:
            if not self._disposed:
                self._warn(f"workspace-changes: {error}")

    def _capture_dir(self) -> str:
        return os.path.join(self._scratch_dir(), "captures")

    def _scratch_dir(self) -> str:
        if self._scratch is None:
            self._scratch = tempfile.mkdtemp(
                prefix="dsh-workspace-changes-", dir=self._env["tempRoot"])
        return self._scratch

    def _locate(self, cwd: str) -> dict | None:
        if self._repository is not None:
            return self._repository
        git = self._env["git"]()
        if git is None:
            return None
        workspace = locate_git_workspace(git, cwd, self._scratch_dir)
        if workspace is None:
            return None
        self._repository = {"git": git, "workspace": workspace}
        return self._repository

    def _read_side(self, source: dict) -> Any:
        if source["kind"] == "absent":
            return None
        if source["kind"] == "file":
            with open(source["file"], "rb") as handle:
                return handle.read().decode("utf-8", "replace")
        repository = source["repository"]
        blob = tree_blob(repository["git"], repository["workspace"],
                         source["tree"], source["path"])
        if blob is None:
            return None
        if blob["size"] > self._env["maxFileBytes"]:
            return OVERSIZED
        return blob_text(repository["git"], repository["workspace"], blob["oid"],
                         self._env["maxFileBytes"])

    async def _record(self, state: dict) -> None:
        paths = self._paths
        baseline = state["baseline"]
        if paths is None or baseline == "failed" or state["lastToolResultSeq"] < 0:
            return
        state["attemptedAfterSeq"] = state["lastToolResultSeq"]
        root = (canonical_path(baseline["workspace"]["root"])
                if baseline else paths["cwd"])
        listed: dict[str, dict] = {}
        snapshot = None
        if baseline is not None:
            after = await asyncio.to_thread(
                snapshot_tree, baseline["git"], baseline["workspace"])
            snapshot = {"before": baseline["tree"], "after": after}
            repository = {"git": baseline["git"], "workspace": baseline["workspace"]}
            entries = await asyncio.to_thread(
                diff_trees, baseline["git"], baseline["workspace"],
                baseline["tree"], after)
            for entry in entries:
                absolute = os.path.join(root, entry["path"])
                listed[absolute] = {
                    "file": self._changed_file(paths, root, absolute, entry),
                    "sources": (
                        {"refusal": "binary"} if entry["binary"] else {
                            "before": {"kind": "snapshot", "repository": repository,
                                       "tree": baseline["tree"],
                                       "path": entry.get("oldPath", entry["path"])},
                            "after": {"kind": "snapshot", "repository": repository,
                                      "tree": after, "path": entry["path"]},
                        }),
                }
        captured = [absolute for absolute in state["captures"] if absolute not in listed]
        work_tree_path = lambda absolute: to_posix(os.path.relpath(absolute, root))
        in_workspace = [absolute for absolute in captured if is_inside(root, absolute)]
        if baseline is not None and in_workspace:
            gitlinks = await asyncio.to_thread(
                gitlink_paths, baseline["git"], baseline["workspace"])
            in_workspace = [
                absolute for absolute in in_workspace
                if not any(is_inside(os.path.join(root, link), absolute) for link in gitlinks)
            ]
        uncovered_in_workspace = (
            {work_tree_path(absolute) for absolute in in_workspace}
            if baseline is None else await asyncio.to_thread(
                ignored_paths, baseline["git"], baseline["workspace"],
                [work_tree_path(absolute) for absolute in in_workspace])
        )
        for absolute in captured:
            if is_inside(root, absolute):
                uncovered = work_tree_path(absolute) in uncovered_in_workspace
            else:
                uncovered = not is_temporary_path(absolute, paths["temporaryRoots"])
            if not uncovered:
                continue
            before = state["captures"][absolute]
            after = await asyncio.to_thread(
                capture_file, absolute, self._capture_dir(), self._env["maxFileBytes"])
            if after is None or same_capture(before, after):
                continue
            listed[absolute] = await asyncio.to_thread(
                self._compared, paths, root, absolute, before, after)
        sorted_files = sorted(
            listed.values(),
            key=cmp_to_key(lambda a, b: compare_display(a["file"], b["file"])))
        if not sorted_files and state["recordedAfterSeq"] < 0:
            return
        event = self._session.append("workspace/changes", {"turn": state["turn"]})
        kept = sorted_files[:self._env["maxFiles"]]
        summary = {
            "turn": state["turn"],
            "cwd": self._cwd,
            "files": [entry["file"] for entry in kept],
            "total": len(sorted_files),
            "added": sum(entry["file"]["added"] for entry in sorted_files),
            "deleted": sum(entry["file"]["deleted"] for entry in sorted_files),
        }
        if snapshot is not None:
            summary["snapshot"] = snapshot
        self._records[event["seq"]] = {
            "summary": summary,
            "sources": [entry["sources"] for entry in kept],
        }
        state["recordedAfterSeq"] = event["seq"]

    def _compared(self, paths: dict, root: str, absolute: str,
                  before: dict, after: dict) -> dict:
        if before["kind"] == "oversized" or after["kind"] == "oversized":
            return {
                "file": self._changed_file(paths, root, absolute,
                                           {"added": 0, "deleted": 0,
                                            "binary": False, "oversized": True}),
                "sources": {"refusal": "oversized"},
            }
        if (before["kind"] == "file" and before["binary"]) or (
                after["kind"] == "file" and after["binary"]):
            return {
                "file": self._changed_file(paths, root, absolute,
                                           {"added": 0, "deleted": 0, "binary": True}),
                "sources": {"refusal": "binary"},
            }

        def text(capture: dict) -> str | None:
            if capture["kind"] != "file":
                return None
            with open(capture["file"], "rb") as handle:
                return handle.read().decode("utf-8", "replace")

        comparison = compare_text(text(before), text(after), self._env["diffTimeoutMs"])
        return {
            "file": self._changed_file(paths, root, absolute,
                                       {"added": comparison["added"],
                                        "deleted": comparison["deleted"],
                                        "binary": False}),
            "sources": {"before": before, "after": after},
        }

    @staticmethod
    def _changed_file(paths: dict, root: str, absolute: str, counts: dict) -> dict:
        file = {
            "path": durable_path_of(absolute, paths["cwd"]),
            "display": display_path_of(absolute, paths["cwd"], root, paths["home"]),
            "added": counts["added"],
            "deleted": counts["deleted"],
        }
        if counts.get("binary"):
            file["binary"] = True
        if counts.get("oversized") is True:
            file["oversized"] = True
        return file


def _raise_if_aborted(signal: Any) -> None:
    if signal is None:
        return
    if getattr(signal, "aborted", None) is True:
        raise RuntimeError("workspace-changes: diff aborted")
    is_set = getattr(signal, "is_set", None)
    if callable(is_set) and is_set():
        raise RuntimeError("workspace-changes: diff aborted")
