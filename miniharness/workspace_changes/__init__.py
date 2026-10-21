"""按顶回合汇总工作区变更，并提供按需的文件前后比较。

上游对照：`packages/deliverables/workspace-changes/src/index.ts`。每个有工作目录的
顶层会话在 `turn/start` 取 git 工作树快照，在文件工具改动前捕获整文件内容，
于回合末比较两棵快照树（并补上 git 未覆盖的捕获），追加只带回合号的
`workspace/changes` 事件；摘要与比较由 `ctx.workspaceChanges` 在会话存活期内服务。

载体差异（登记）：迷你 `session/event` 单载荷派发；`agent/turn-stopping` 由
miniharness 的 aserial 派发。临时目录根可通过 `tempRoot` 配置覆盖（上游取
`os.tmpdir()`），便于测试与部署指定。
"""
from __future__ import annotations

import inspect
import tempfile
import weakref
from typing import Any

from ..core.scope import Context, Service
from ..core.working_cwd import working_directory_of
from .git import GitRunner, resolve_git
from .recorder import TurnRecorder

__all__ = ["DEFAULT_CONFIG", "WorkspaceChanges", "install_workspace_changes", "name"]

name = "workspace-changes"

DEFAULT_CONFIG = {
    "timeoutMs": 30_000,
    "outputMaxBytes": 8 * 1024 * 1024,
    "maxFiles": 500,
    "maxFileBytes": 2 * 1024 * 1024,
    "diffTimeoutMs": 100,
}

_POSITIVE_FIELDS = ("timeoutMs", "outputMaxBytes", "maxFiles",
                    "maxFileBytes", "diffTimeoutMs")


def _config(raw: dict | None) -> dict:
    merged = dict(DEFAULT_CONFIG)
    if raw:
        unknown = set(raw) - set(DEFAULT_CONFIG) - {"tempRoot"}
        if unknown:
            raise ValueError(
                f"workspace-changes: unknown config fields {sorted(unknown)}")
        merged.update(raw)
    for field in _POSITIVE_FIELDS:
        value = merged[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(
                f"workspace-changes requires a positive integer {field}")
    merged["tempRoot"] = (raw or {}).get("tempRoot") or tempfile.gettempdir()
    if not isinstance(merged["tempRoot"], str) or merged["tempRoot"] == "":
        raise ValueError("workspace-changes: tempRoot must be a non-empty path")
    return merged


class WorkspaceChanges(Service):
    """`ctx.workspaceChanges`：存活会话的回合变更摘要与比较。"""

    provide = "workspaceChanges"

    def __init__(self, ctx: Context, config: dict | None = None):
        self._config = _config(config)
        self._recorders: dict[str, TurnRecorder] = {}
        self._mutations: weakref.WeakKeyDictionary[Any, TurnRecorder] = (
            weakref.WeakKeyDictionary())
        self._git_resolved = False
        self._git: GitRunner | None = None
        super().__init__(ctx)
        self._install()

    # ---------- 公开服务面 ----------

    def summary(self, session_id: str, seq: int) -> dict | None:
        """`workspace/changes` 事件声明的摘要；会话销毁后为 None。"""
        recorder = self._recorders.get(session_id)
        return recorder.summary(seq) if recorder is not None else None

    async def diff(self, session_id: str, seq: int, index: int,
                   signal: Any = None) -> dict | None:
        """比较摘要中一个文件回合首尾内容；未知坐标返回 None。"""
        recorder = self._recorders.get(session_id)
        if recorder is None:
            return None
        return await recorder.diff(seq, index, signal)

    # ---------- 装配 ----------

    def _install(self) -> None:
        ctx = self.ctx
        ctx.on("session/event", self._on_session_event, global_=True)
        ctx.on("session/disposed", self._on_disposed, global_=True)
        ctx.on("agent/turn-stopping", self._on_turn_stopping)
        ctx.on("tools/pre-execute", self._on_pre_execute)
        ctx.on("fs/write-intent", self._on_write_intent, prepend=True)
        ctx.on("fs/edit-intent", self._on_edit_intent, prepend=True)
        ctx.effect(lambda: lambda: self._dispose(), "workspaceChanges lifecycle")

    def _dispose(self) -> None:
        for recorder in list(self._recorders.values()):
            recorder.dispose()
        self._recorders.clear()
        self._mutations.clear()

    # ---------- 事件 ----------

    def _on_session_event(self, payload: dict) -> None:
        session = payload["session"]
        event = payload["event"]
        type_ = event.get("type")
        data = event.get("data") or {}
        if type_ == "turn/start":
            cwd = self._eligible(session)
            if cwd is not None:
                self._recorder_for(session, cwd).start(data.get("turn"))
            return
        recorder = self._recorders.get(session.session_id)
        if recorder is None:
            return
        if type_ == "tool/result":
            recorder.observe(event)
        elif type_ == "turn/end":
            recorder.end(data.get("turn"))

    async def _on_turn_stopping(self, payload: dict) -> None:
        agent = payload.get("agent")
        session = getattr(agent, "session", None)
        if session is None:
            return
        recorder = self._recorders.get(session.session_id)
        if recorder is not None:
            await recorder.stopping(payload.get("turn"))

    def _on_disposed(self, payload: dict) -> None:
        session = payload["session"]
        recorder = self._recorders.pop(session.session_id, None)
        if recorder is not None:
            recorder.dispose()

    def _on_pre_execute(self, payload: dict, next_: Any) -> Any:
        exec_ = payload.get("exec")
        agent = getattr(exec_, "agent", None)
        session = getattr(agent, "session", None)
        recorder = (self._recorders.get(session.session_id)
                    if session is not None else None)
        if exec_ is None or recorder is None:
            return next_(payload)
        self._mutations[exec_] = recorder
        result = next_(payload)
        if inspect.isawaitable(result):
            async def wait_for_settle() -> Any:
                await recorder.settled()
                return await result
            return wait_for_settle()
        return result

    async def _on_write_intent(self, payload: dict, next_: Any) -> Any:
        target, actor = payload
        intent = await next_(payload)
        await self._capture(actor, target)
        return intent

    async def _on_edit_intent(self, payload: dict, next_: Any) -> Any:
        target, actor = payload
        intent = await next_(payload)
        await self._capture(actor, target)
        return intent

    async def _capture(self, actor: Any, target: Any) -> None:
        recorder = self._mutations.get(actor)
        if recorder is None:
            return
        fs = self.ctx.get("fs")
        if fs is None:
            raise RuntimeError("workspace-changes requires ctx.fs")
        await recorder.capture_async(fs.process_path(target))

    # ---------- 帮助 ----------

    def _eligible(self, session: Any) -> str | None:
        meta = getattr(session, "meta", None) or {}
        if meta.get("origin") == "subagent":
            return None
        depth = meta.get("delegationDepth", 0)
        if isinstance(depth, int) and depth > 0:
            return None
        return working_directory_of(session, self.ctx)

    def _recorder_for(self, session: Any, cwd: str) -> TurnRecorder:
        recorder = self._recorders.get(session.session_id)
        if recorder is None:
            recorder = TurnRecorder(session, cwd, {
                "git": self._git_runner,
                "tempRoot": self._config["tempRoot"],
                "maxFiles": self._config["maxFiles"],
                "maxFileBytes": self._config["maxFileBytes"],
                "diffTimeoutMs": self._config["diffTimeoutMs"],
            }, self._warn)
            self._recorders[session.session_id] = recorder
        return recorder

    def _git_runner(self) -> GitRunner | None:
        if not self._git_resolved:
            self._git_resolved = True
            executable = resolve_git()
            if executable is None:
                self._info(
                    "workspace-changes: git is unavailable; only file-tool edits are summarized")
            else:
                self._git = GitRunner(
                    executable,
                    timeout_ms=self._config["timeoutMs"],
                    output_max_bytes=self._config["outputMaxBytes"])
        return self._git

    def _logger(self) -> Any:
        logger = getattr(self.ctx, "logger", None)
        return logger("workspace-changes") if callable(logger) else logger

    def _warn(self, message: str) -> None:
        logger = self._logger()
        if logger is not None and hasattr(logger, "warn"):
            logger.warn(message)

    def _info(self, message: str) -> None:
        logger = self._logger()
        if logger is not None and hasattr(logger, "info"):
            logger.info(message)


def install_workspace_changes(ctx: Context, config: dict | None = None) -> WorkspaceChanges:
    """装配 `ctx.workspaceChanges`（幂等：已装返回既有实例）。"""
    existing = ctx.get("workspaceChanges")
    if isinstance(existing, WorkspaceChanges):
        return existing
    return WorkspaceChanges(ctx, config)
