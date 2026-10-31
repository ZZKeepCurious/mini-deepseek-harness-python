"""apply_patch 执行器（对齐 packages/fs/tool-fs/src/patch/*）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..core.scope import Context
from .patch_codec import PatchHunk, PatchOperation, PatchOpAdd, PatchOpDelete, PatchOpUpdate, PatchParseError, apply_hunks, parse_patch
from .tools import FsSandboxGate, _session_resolve_options, remediate_fs_error
from .types import FsError, FsObservation, FsTarget, FsWriteIntent

__all__ = ["PatchToolError", "execute_patch", "patch_tool_value", "render_patch", "patch_presentation_meta"]


class PatchToolError(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


@dataclass
class PatchOperationRecord:
    index: int
    kind: Literal["add", "update", "delete", "move"]
    path: str
    patch_line: int
    destination: str | None = None


@dataclass
class PlannedOperation:
    record: PatchOperationRecord
    target: FsTarget
    raw_path: str
    kind: Literal["add", "update", "delete", "move"]
    content: str | None = None
    hunks: list[PatchHunk] | None = None
    destination: FsTarget | None = None


def _patch_failure(gate: FsSandboxGate, policy: dict | None, error: FsError, path: str) -> dict:
    mapped = remediate_fs_error(gate.map_error(error, policy), path)
    return {"code": mapped.code, "message": str(mapped)}


async def _source_intent(ctx: Context, exec: Any, target: FsTarget,
                         kind: Literal["delete", "edit"]) -> dict | None:
    event = "fs/delete-intent" if kind == "delete" else "fs/edit-intent"
    return await ctx.awaterfall(event, (target, exec), base=lambda _payload: None)


async def _destination_intent(ctx: Context, exec: Any, target: FsTarget) -> FsWriteIntent | None:
    return await ctx.awaterfall("fs/write-intent", (target, exec), base=lambda _payload: None)


async def plan_patch(ctx: Context, gate: FsSandboxGate, exec: Any, patch: str,
                     policy: dict | None) -> tuple[list[PlannedOperation], list[dict]]:
    parsed = parse_patch(patch)
    targets: dict[str, FsTarget] = {}

    async def resolve(path: str) -> FsTarget:
        fs = ctx.get("fs")
        target = await fs.resolve(path, _session_resolve_options(exec))
        prior = targets.get(target.target_key)
        if prior is not None:
            return prior
        targets[target.target_key] = target
        return target

    operations: list[PlannedOperation] = []
    refusals: list[dict] = []
    for index, operation in enumerate(parsed):
        move_to = operation.move_to if isinstance(operation, PatchOpUpdate) else None
        kind: Literal["add", "update", "delete", "move"] = (
            "move" if move_to is not None else operation.kind  # type: ignore[assignment]
        )
        record = PatchOperationRecord(
            index=index, kind=kind, path=operation.path,
            patch_line=operation.line_number,
            destination=move_to,
        )
        failure_path = operation.path
        try:
            target = await resolve(operation.path)
            record.path = target.display_path
            if isinstance(operation, PatchOpAdd):
                operations.append(PlannedOperation(
                    record=record, target=target, raw_path=operation.path,
                    kind="add", content=operation.content))
            elif isinstance(operation, PatchOpDelete):
                operations.append(PlannedOperation(
                    record=record, target=target, raw_path=operation.path, kind="delete"))
            elif isinstance(operation, PatchOpUpdate):
                if move_to is None:
                    operations.append(PlannedOperation(
                        record=record, target=target, raw_path=operation.path,
                        kind="update", hunks=operation.hunks))
                else:
                    failure_path = move_to
                    dest = await resolve(move_to)
                    record.destination = dest.display_path
                    operations.append(PlannedOperation(
                        record=record, target=target, raw_path=operation.path,
                        kind="move", destination=dest, hunks=operation.hunks))
        except FsError as error:
            refusals.append({
                "operation": record,
                "failure": _patch_failure(gate, policy, error, failure_path),
            })
    return operations, refusals


async def _preflight_one(ctx: Context, gate: FsSandboxGate, exec: Any, policy: dict | None,
                         operation: PlannedOperation, states: dict) -> dict | None:
    failure_path = operation.target.display_path
    try:
        if operation.kind == "move" and operation.destination is not None:
            if operation.target.target_key == operation.destination.target_key:
                raise FsError(
                    f'cannot move "{operation.target.display_path}" to itself',
                    "FS_SAME_TARGET")
            info = await ctx.get("fs").lstat(
                operation.raw_path, _session_resolve_options(exec))
            if info is not None and info.type == "symlink":
                return {
                    "code": "PATCH_SYMLINK_SOURCE",
                    "message": (
                        f'cannot {operation.kind} "{operation.target.display_path}": '
                        "path is a symbolic link — operate on its target path instead"
                    ),
                }
        state = await _sim_state(ctx, exec, operation.target, states)
        if operation.kind == "add":
            await _sim_destination(ctx, exec, state, operation.target)
            states[operation.target.target_key] = {"exists": True, "touched": True,
                                                   "content": operation.content}
            return None
        if operation.kind == "delete":
            await _sim_source(ctx, exec, state, operation.target, "delete")
            states[operation.target.target_key] = {"exists": False, "touched": True}
            return None
        await _sim_source(ctx, exec, state, operation.target,
                          "move" if operation.kind == "move" else "edit")
        if operation.kind == "move" and operation.destination is not None:
            failure_path = operation.destination.display_path
            dest_state = await _sim_state(ctx, exec, operation.destination, states)
            await _sim_destination(ctx, exec, dest_state, operation.destination)
            failure_path = operation.target.display_path
        content = await _sim_content(ctx, exec, state, operation.target)
        matched, payload = apply_hunks(content, operation.hunks or [])
        if not matched:
            preview = payload.pattern[0][:80] if payload.pattern else ""
            if len(payload.pattern[0]) > 80:
                preview += "…"
            return {
                "code": "FS_PATCH_MISMATCH",
                "hunkLine": payload.hunk_line_number,
                "message": (
                    f'cannot patch "{operation.target.display_path}": hunk at patch line '
                    f"{payload.hunk_line_number} ({payload.stage}) does not match — {preview}"
                ),
            }
        if operation.kind == "move" and operation.destination is not None:
            states[operation.destination.target_key] = {"exists": True, "touched": True,
                                                        "content": payload}
            states[operation.target.target_key] = {"exists": False, "touched": True}
        else:
            states[operation.target.target_key] = {"exists": True, "touched": True,
                                                   "content": payload}
        return None
    except FsError as error:
        return _patch_failure(gate, policy, error, failure_path)


async def _sim_state(ctx: Context, exec: Any, target: FsTarget, states: dict) -> dict:
    known = states.get(target.target_key)
    if known is not None:
        return known
    info = await ctx.get("fs").stat(target, getattr(exec, "signal", None))
    state = {"exists": info is not None, "info": info, "touched": False}
    states[target.target_key] = state
    return state


async def _sim_source(ctx: Context, exec: Any, state: dict, target: FsTarget,
                      kind: str) -> None:
    intent_kind: Literal["delete", "edit"] = "delete" if kind == "delete" else "edit"
    intent = (None if state.get("touched")
              else await _source_intent(ctx, exec, target, intent_kind))
    if not state["exists"]:
        raise FsError(f'cannot {kind} "{target.display_path}": not found', "FS_NOT_FOUND")
    if state.get("info") is not None and state["info"].type != "file":
        raise FsError(
            f'cannot modify "{target.display_path}": not a regular file',
            "FS_NOT_REGULAR_FILE")
    if intent is not None and intent.get("version") != state["info"].version:
        raise FsError(
            f'cannot {kind} "{target.display_path}": file changed since it was read',
            "FS_STALE_VERSION")


async def _sim_destination(ctx: Context, exec: Any, state: dict, target: FsTarget) -> None:
    intent = None if state.get("touched") else await _destination_intent(ctx, exec, target)
    if state.get("info") is not None and state["info"].type != "file":
        raise FsError(
            f'cannot modify "{target.display_path}": not a regular file',
            "FS_NOT_REGULAR_FILE")
    if intent is not None and intent.kind == "createIfAbsent" and state["exists"]:
        raise FsError(
            f'cannot overwrite existing "{target.display_path}" without reading it first',
            "FS_NOT_OBSERVED")
    if intent is not None and intent.kind == "replaceIfVersion":
        if not state["exists"] or state["info"].version != intent.version:
            raise FsError(
                f'cannot write "{target.display_path}": file changed since it was read',
                "FS_STALE_VERSION")


async def _sim_content(ctx: Context, exec: Any, state: dict, target: FsTarget) -> str:
    if state.get("content") is None:
        text = await ctx.get("fs").read_text(target, getattr(exec, "signal", None))
        if "\0" in text:
            raise FsError(f'cannot patch "{target.display_path}": binary file', "FS_NOT_TEXT")
        state["content"] = text.replace("\r\n", "\n")
    return state["content"]


async def commit_patch(ctx: Context, gate: FsSandboxGate, exec: Any, policy: dict | None,
                       operations: list[PlannedOperation]) -> dict:
    fs = ctx.get("fs")
    outcomes: list[dict] = []
    for operation in operations:
        outcomes.append({"status": "skipped", "operation": operation.record})
    cancelled = False
    for index, operation in enumerate(operations):
        signal = getattr(exec, "signal", None)
        if signal is not None and getattr(signal, "is_set", lambda: False)():
            cancelled = True
            break
        record = operation.record
        failure_path = operation.target.display_path
        before_snapshots: list[tuple[FsTarget, Any]] = []
        try:
            if operation.kind == "add":
                intent = await _destination_intent(ctx, exec, operation.target)
                result = await fs.write_text(operation.target, operation.content or "",
                                             intent, signal, policy)
                ctx.emit("fs/observed",
                         (operation.target, FsObservation("present", result.version), exec))
                outcomes[index] = {
                    "status": "completed", "operation": record,
                    "changes": [{"path": fs.process_path(operation.target),
                                 "before": result.before, "after": result.after}],
                }
                continue
            if operation.kind == "delete":
                intent = await _source_intent(ctx, exec, operation.target, "delete")
                before_snapshots = [(operation.target, await fs.stat(operation.target, signal))]
                result = await fs.delete_file(operation.target, intent, signal, policy)
                ctx.emit("fs/observed", (operation.target, FsObservation("absent"), exec))
                outcomes[index] = {
                    "status": "completed", "operation": record,
                    "changes": [{"path": fs.process_path(operation.target),
                                 "before": result.before, "after": None}],
                }
                continue
            if operation.kind == "update":
                intent = await _source_intent(ctx, exec, operation.target, "edit")
                before_snapshots = [(operation.target, await fs.stat(operation.target, signal))]
                result = await fs.patch_text(operation.target, operation.hunks or [],
                                             intent, signal, policy)
                ctx.emit("fs/observed",
                         (operation.target, FsObservation("present", result.version), exec))
                outcomes[index] = {
                    "status": "completed", "operation": record,
                    "changes": [{"path": fs.process_path(operation.target),
                                 "before": result.before, "after": result.after}],
                }
                continue
            if operation.kind == "move" and operation.destination is not None:
                expected_source = await _source_intent(ctx, exec, operation.target, "edit")
                failure_path = operation.destination.display_path
                expected_dest = await _destination_intent(ctx, exec, operation.destination)
                before_snapshots = [
                    (operation.target, await fs.stat(operation.target, signal)),
                    (operation.destination, await fs.stat(operation.destination, signal)),
                ]
                result = await fs.move_file(
                    operation.target, operation.destination,
                    {"hunks": operation.hunks,
                     "expectedSource": expected_source,
                     "expectedDestination": expected_dest},
                    signal, policy)
                changes = [{
                    "path": fs.process_path(operation.destination),
                    "before": result.destination.before,
                    "after": result.destination.after,
                }]
                ctx.emit("fs/observed", (
                    operation.destination,
                    FsObservation("present", result.destination.version),
                    exec))
                if result.source_deleted:
                    ctx.emit("fs/observed", (operation.target, FsObservation("absent"), exec))
                    changes.append({
                        "path": fs.process_path(operation.target),
                        "before": result.source_before,
                        "after": None,
                    })
                    outcomes[index] = {"status": "completed", "operation": record, "changes": changes}
                else:
                    failure = result.source_failure or {"code": "FS_IO_ERROR", "message": "move failed"}
                    outcomes[index] = {
                        "status": "failed", "operation": record,
                        "failure": failure, "changes": changes,
                    }
                if outcomes[index]["status"] != "completed":
                    cancelled = signal is not None and getattr(signal, "is_set", lambda: False)()
                    break
                continue
            raise RuntimeError(f"unknown patch operation kind: {operation.kind}")
        except FsError as error:
            failure = _patch_failure(gate, policy, error, failure_path)
            uncertain = False
            if before_snapshots:
                try:
                    for target, info in before_snapshots:
                        after = await fs.stat(target, signal)
                        if after is None or (info is not None and after.version != info.version):
                            uncertain = True
                            break
                except FsError:
                    uncertain = True
            if error.code == "FS_ABORTED":
                uncertain = True
            if uncertain:
                outcomes[index] = {
                    "status": "unknown", "operation": record,
                    "failure": {
                        **failure,
                        "message": f"{failure['message']} — re-read the affected files before retrying",
                    },
                }
            else:
                outcomes[index] = {
                    "status": "failed", "operation": record,
                    "failure": failure, "changes": [],
                }
            cancelled = signal is not None and getattr(signal, "is_set", lambda: False)()
            break
    affected: list[str] = []
    for outcome in outcomes:
        for change in outcome.get("changes") or []:
            affected.append(change["path"])
    return {
        "phase": "commit",
        "outcomes": outcomes,
        "affectedPaths": list(dict.fromkeys(affected)),
        "cancelled": cancelled,
    }


async def execute_patch(ctx: Context, gate: FsSandboxGate, exec: Any, *,
                        patch: str, sandbox_policy: dict | None) -> dict:
    try:
        operations, plan_refusals = await plan_patch(ctx, gate, exec, patch, sandbox_policy)
    except PatchParseError as error:
        return {"phase": "syntax", "failure": {"code": "PATCH_SYNTAX", "message": str(error)}}
    refusals = list(plan_refusals)
    states: dict = {}
    for operation in operations:
        failure = await _preflight_one(ctx, gate, exec, sandbox_policy, operation, states)
        if failure is not None:
            refusals.append({"operation": operation.record, "failure": failure})
    refusals.sort(key=lambda item: item["operation"].index)
    if refusals:
        return {"phase": "preflight", "refusals": refusals}
    return await commit_patch(ctx, gate, exec, sandbox_policy, operations)


def _refusal_error(refusals: list[dict]) -> Exception:
    first = refusals[0]
    lines = [
        f"- operation {item['operation'].index + 1} ({item['operation'].kind} "
        f"{item['operation'].path}, patch line {item['operation'].patch_line}): "
        f"{item['failure']['message']}"
        for item in refusals
    ]
    code = first["failure"]["code"]
    message = f"Patch rejected before any change ({len(refusals)} problems):\n" + "\n".join(lines)
    if code in ("PATCH_SYNTAX", "PATCH_SYMLINK_SOURCE"):
        return PatchToolError(message, code)  # type: ignore[arg-type]
    return FsError(message, code)


def patch_tool_value(report: dict) -> dict:
    if report["phase"] == "syntax":
        failure = report["failure"]
        code = failure["code"]
        if code == "PATCH_SYNTAX":
            raise PatchToolError(failure["message"], code)  # type: ignore[arg-type]
        raise FsError(failure["message"], code)
    if report["phase"] == "preflight":
        raise _refusal_error(report["refusals"])
    outcomes = report["outcomes"]
    if outcomes and outcomes[0]["status"] == "failed" and not outcomes[0].get("changes"):
        if all(item["status"] == "skipped" for item in outcomes[1:]):
            raise _refusal_error([{
                "operation": outcomes[0]["operation"],
                "failure": outcomes[0]["failure"],
            }])
    flat_ops = []
    for outcome in outcomes:
        op = outcome["operation"]
        row = {
            "index": op.index,
            "kind": op.kind,
            "path": op.path,
            "patchLine": op.patch_line,
            "status": outcome["status"],
        }
        if op.destination is not None:
            row["destination"] = op.destination
        if "failure" in outcome:
            row.update(outcome["failure"])
        if "changes" in outcome:
            row["changes"] = outcome["changes"]
        flat_ops.append(row)
    return {
        "phase": "commit",
        "cancelled": report["cancelled"],
        "affectedPaths": report["affectedPaths"],
        "operations": flat_ops,
    }


def _patch_stopped(value: dict) -> bool:
    return value["cancelled"] or any(op["status"] != "completed" for op in value["operations"])


def render_patch(value: dict) -> str:
    if not _patch_stopped(value):
        if not value["operations"]:
            return "Success. No operations in patch."
        lines = ["Success. Updated the following files:"]
        for kind, letter in (("add", "A"), ("update", "M"), ("delete", "D")):
            for op in value["operations"]:
                if op["status"] != "completed":
                    continue
                op_kind = "update" if op["kind"] == "move" else op["kind"]
                if op_kind != kind:
                    continue
                path = op.get("destination") or op["path"]
                lines.append(f"{letter} {path}")
        return "\n".join(lines)
    completed = sum(1 for op in value["operations"] if op["status"] == "completed")
    lines = [f"Patch stopped after {completed} of {len(value['operations'])} operations."]
    return "\n".join(lines)


def patch_presentation_meta(value: dict) -> dict:
    from .diff import compute_hunk_diffs

    diffs: list[dict] = []
    for op in value["operations"]:
        if op["status"] not in ("completed", "failed"):
            continue
        for change in op.get("changes") or []:
            before = change["before"]
            after = change["after"]
            if before is None:
                continue
            if after is None:
                diffs.append({"path": change["path"], "oldText": before, "newText": ""})
            else:
                diffs.extend(compute_hunk_diffs(change["path"], before, after))
    return {"diffs": diffs, "stopped": _patch_stopped(value)}
