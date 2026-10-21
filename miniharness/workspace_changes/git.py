"""git 工作树快照、树差异与忽略检查。

上游对照：`packages/deliverables/workspace-changes/src/git.ts`。快照写入会话
临时目录的私有对象库（alternates 只读仓库对象库），不触碰仓库 index、refs
与工作树。
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
from typing import Any

from .paths import canonical_path, is_inside, to_posix

__all__ = [
    "GitRunner",
    "blob_text",
    "diff_trees",
    "gitlink_paths",
    "ignored_paths",
    "locate_git_workspace",
    "resolve_git",
    "snapshot_tree",
    "tree_blob",
]

STDERR_TAIL_BYTES = 16 * 1024


class GitRunner:
    """在固定超时与输出上限下运行一个已解析的 git 可执行文件。"""

    def __init__(self, executable: str, *, timeout_ms: int, output_max_bytes: int):
        self.executable = executable
        self.timeout_ms = timeout_ms
        self.output_max_bytes = output_max_bytes

    def run(self, args: list[str], *, cwd: str, env: dict | None = None,
            stdin: bytes | None = None, max_bytes: int | None = None,
            signal: Any = None) -> dict:
        _raise_if_aborted(signal)
        command = [self.executable, *args]
        environment = os.environ.copy()
        environment.update({
            "GIT_CONFIG_COUNT": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        })
        if env:
            environment.update(env)
        try:
            outcome = subprocess.run(
                command, cwd=cwd, env=environment, input=stdin,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self.timeout_ms / 1000.0, check=False)
        except subprocess.TimeoutExpired as error:
            _raise_if_aborted(signal)
            raise RuntimeError(
                f"git {' '.join(args)} timed out after {self.timeout_ms}ms") from error
        _raise_if_aborted(signal)
        limit = max_bytes if max_bytes is not None else self.output_max_bytes
        truncated = len(outcome.stdout) > limit
        return {
            "exitCode": outcome.returncode,
            "stdout": outcome.stdout[:limit] if truncated else outcome.stdout,
            "stderr": outcome.stderr[-STDERR_TAIL_BYTES:],
            "truncated": truncated,
        }


def _raise_if_aborted(signal: Any) -> None:
    if signal is None:
        return
    aborted = getattr(signal, "aborted", None)
    if aborted is True:
        raise RuntimeError("workspace-changes: aborted")
    is_set = getattr(signal, "is_set", None)
    if callable(is_set) and is_set():
        raise RuntimeError("workspace-changes: aborted")


def _text(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def _ok(result: dict, what: str) -> dict:
    if result["exitCode"] != 0:
        raise RuntimeError(f"{what} failed: {_text(result['stderr']).strip()}")
    return result


def resolve_git(signal: Any = None) -> str | None:
    """解析 git 可执行文件；不可用返回 None。"""
    executable = shutil.which("git")
    if executable is None:
        return None
    if platform.system() != "Darwin" or executable != "/usr/bin/git":
        return executable
    probe = subprocess.run(["/usr/bin/xcode-select", "-p"], cwd=os.path.expanduser("~"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    _raise_if_aborted(signal)
    return executable if probe.returncode == 0 else None


def locate_git_workspace(git: GitRunner, cwd: str, scratch: Any,
                         signal: Any = None) -> dict | None:
    found = git.run(["rev-parse", "--show-toplevel", "--absolute-git-dir",
                     "--git-path", "objects"], cwd=cwd, signal=signal)
    if found["exitCode"] == 128 and b"not a git repository" in found["stderr"].lower():
        return None
    lines = _text(_ok(found, "git rev-parse")["stdout"]).splitlines()
    if len(lines) < 3:
        raise RuntimeError("workspace-changes: git rev-parse returned too few lines")
    root, git_dir, repository_objects = (
        os.path.normpath(os.path.join(cwd, line)) for line in lines[:3])
    root = canonical_path(root)
    directory = canonical_path(scratch())
    objects = os.path.join(directory, "objects")
    os.makedirs(objects, exist_ok=True)
    excludes = [to_posix(os.path.relpath(directory, root))] if is_inside(root, directory) else []
    env = {
        "GIT_OBJECT_DIRECTORY": objects,
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": repository_objects,
    }
    return {
        "root": root,
        "gitDir": canonical_path(git_dir),
        "scratch": directory,
        "env": env,
        "excludes": excludes,
    }


def snapshot_tree(git: GitRunner, workspace: dict, signal: Any = None) -> str:
    scratch = tempfile.mkdtemp(prefix="index-", dir=workspace["scratch"])
    try:
        index = os.path.join(scratch, "index")
        source = os.path.join(workspace["gitDir"], "index")
        try:
            shutil.copyfile(source, index)
        except FileNotFoundError:
            pass
        env = dict(workspace["env"])
        env["GIT_INDEX_FILE"] = index
        pathspec: list[str] = []
        if workspace["excludes"]:
            pathspec = ["--", ".", *[f":(exclude){path}" for path in workspace["excludes"]]]
        added = git.run(["add", "--all", "--ignore-errors", *pathspec],
                        cwd=workspace["root"], env=env, signal=signal)
        if added["exitCode"] != 1:
            _ok(added, f"git add in {workspace['root']}")
        written = _ok(git.run(["write-tree"], cwd=workspace["root"], env=env,
                              signal=signal), "git write-tree")
        return _text(written["stdout"]).strip()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


_ENTRY = re.compile(rb"^\d+ (\S+) ([0-9a-f]+) +(\d+)\t")


def tree_blob(git: GitRunner, workspace: dict, tree: str, path: str,
              signal: Any = None) -> dict | None:
    result = _ok(git.run(["ls-tree", "-z", "-l", tree, "--", path],
                         cwd=workspace["root"],
                         env={**workspace["env"], "GIT_LITERAL_PATHSPECS": "1"},
                         signal=signal), "git ls-tree")
    entry = result["stdout"].split(b"\0")[0]
    match = _ENTRY.match(entry)
    if match is None or match.group(1) != b"blob":
        return None
    return {"oid": match.group(2).decode("ascii"), "size": int(match.group(3))}


def blob_text(git: GitRunner, workspace: dict, oid: str, max_bytes: int,
              signal: Any = None) -> str:
    result = _ok(git.run(["cat-file", "blob", oid], cwd=workspace["root"],
                         env=workspace["env"], max_bytes=max_bytes, signal=signal),
                 "git cat-file")
    if result["truncated"]:
        raise RuntimeError(f"workspace-changes: blob {oid} exceeds {max_bytes} bytes")
    return _text(result["stdout"])


def parse_numstat(output: bytes) -> list[dict]:
    queue = output.split(b"\0")
    if not queue or queue[-1] != b"":
        raise RuntimeError("workspace-changes: numstat output is not NUL-terminated")
    queue.pop()
    entries: list[dict] = []
    while queue:
        record = queue.pop(0)
        first = record.find(b"\t")
        second = record.find(b"\t", first + 1) if first >= 0 else -1
        if second < 0:
            raise RuntimeError(f"workspace-changes: malformed numstat record: {record!r}")
        added = record[:first]
        deleted = record[first + 1:second]
        path = record[second + 1:]
        old_path = None
        if path == b"":
            if len(queue) < 2:
                raise RuntimeError("workspace-changes: malformed numstat rename record")
            old_path = _text(queue.pop(0))
            path = queue.pop(0)
        binary = added == b"-"
        entries.append({
            "path": _text(path),
            **({"oldPath": old_path} if old_path is not None else {}),
            "added": 0 if binary else int(added),
            "deleted": 0 if binary else int(deleted),
            "binary": binary,
        })
    return entries


def diff_trees(git: GitRunner, workspace: dict, before: str, after: str,
               signal: Any = None) -> list[dict]:
    if before == after:
        return []
    result = _ok(git.run(["diff-tree", "-r", "-M", "-z", "--numstat", before, after],
                         cwd=workspace["root"], env=workspace["env"], signal=signal),
                 "git diff-tree")
    if result["truncated"]:
        raise RuntimeError(
            "workspace-changes: git diff-tree output exceeded the configured cap")
    return parse_numstat(result["stdout"])


def gitlink_paths(git: GitRunner, workspace: dict, signal: Any = None) -> set[str]:
    result = _ok(git.run(["ls-files", "-z", "--stage"], cwd=workspace["root"],
                         env=workspace["env"], signal=signal), "git ls-files")
    return {
        _text(entry.split(b"\t", 1)[1])
        for entry in result["stdout"].split(b"\0")
        if entry.startswith(b"160000 ")
    }


def ignored_paths(git: GitRunner, workspace: dict, paths: list[str],
                  signal: Any = None) -> set[str]:
    if not paths:
        return set()
    stdin = b"".join(path.encode("utf-8") + b"\0" for path in paths)
    result = git.run(["check-ignore", "-z", "--stdin"], cwd=workspace["root"],
                     env=workspace["env"], stdin=stdin, signal=signal)
    if result["exitCode"] == 1:
        return set()
    _ok(result, "git check-ignore")
    return {_text(path) for path in result["stdout"].split(b"\0") if path != b""}
