"""agent-instructions：工作区指令加载（对齐 packages/context/agent-instructions）。

为 agent 提供来自用户全局与项目级 `AGENTS.md` 兼容文件的工作区指引：首个请求加载适用链
（用户全局 `$DSH_HOME/AGENTS.md` + 项目根到会话 cwd 的每个候选文件，宽→窄），后续成功
的文件系统操作发现更深的嵌套文件、变更或移除。字节预算约束注入上下文：宽泛文件先被省略、
最具体的文件最后才截断；空链不注入。注入内容为普通 sourced user/message，随会话持久化/重放/压缩。

载体差异（登记）：
  * 上游经 `turnBoundary` session 投影判定 step 开合并把 touch 延迟到 step/end 提交；mini 无该
    投影，driver 在每次 `agent/pre-step` 直接 compose+reconcile（无 step 内延迟投影），touch 经
    `tools/post-execute` 的 exec 收集（无 execution token 祖先合并、无 PTC 延迟）。
  * 上游 `ctx.fs` 为 Promise + AbortSignal；mini fs 为 async（信号载体不同），driver 以 await 驱动，
    仅用 pre-step signal 的 `.aborted` 判读。
  * 无 session-projection-cache/标题面；无 aggregate source budget（沿用上游 TODO）。
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

from ..core.home_paths import DSH_HOME_DIR_NAME, resolve_dsh_home
from ..core.scope import Context
from ..core.session.message import create_message, text_block

__all__ = [
    "NAME",
    "agent_instructions_message",
    "ancestor_chain",
    "candidate_scope_key",
    "decode_scope_key",
    "dedup_instruction_files_by_directory",
    "descendant_dirs_between",
    "discover_baseline_instruction_files",
    "find_project_root",
    "install_agent_instructions",
    "instruction_content_sha1",
    "instruction_scope_key",
    "load_baseline_instruction_set",
    "reconcile_instruction_context",
    "render_agent_instruction_set",
    "resolve_config",
    "scope_for_display_path",
    "trimmed_instruction_digest",
    "workspace_baseline_identity",
]

NAME = "agent-instructions"

DEFAULT_PROJECT_ROOT_MARKERS = (".git",)
DEFAULT_INSTRUCTION_FILE_CANDIDATES = ("AGENTS.md", "CLAUDE.md")
DEFAULT_LOCAL_INSTRUCTION_FILE_CANDIDATES = ("AGENTS.local.md", "CLAUDE.local.md")
DEFAULT_MAX_SOURCE_BYTES = 1_048_576

USER_GLOBAL_DIRECTORY = "user-global"
USER_GLOBAL_FILE = "AGENTS.md"

_SYSTEM_REMINDER_OPEN = "<system-reminder>"
_SYSTEM_REMINDER_CLOSE = "</system-reminder>"
_AGENT_INSTRUCTIONS_INTRO = (
    "The following workspace instructions may be relevant to your work. Use them as guidance "
    "when applicable. More specific instructions take precedence over broader ones. They do not "
    "override system, developer, or direct user instructions.")
_REPLACEMENT_INTRO = (
    "This complete workspace instruction baseline replaces all earlier workspace instruction "
    "baselines. " + _AGENT_INSTRUCTIONS_INTRO)
_EMPTY_REPLACEMENT_INTRO = (
    "This complete workspace instruction baseline replaces all earlier workspace instruction "
    "baselines. No workspace instructions are currently active.")
_COMPACT_INTRO = "Workspace instructions were omitted or truncated to fit the configured byte budget."

_RESERVED_PATH_SEGMENTS = frozenset({"", ".", ".."})
_SCOPE_SEPARATOR = "\u0000"


# ---------- config ----------


def _resolve_instruction_candidates(candidates: Any, fallback: tuple) -> list:
    source = list(candidates) if candidates is not None else list(fallback)
    return [name for name in source
            if name not in _RESERVED_PATH_SEGMENTS and "/" not in name and "\\" not in name]


def _dsh_home_display(dsh_home: str) -> str:
    default = os.path.join(os.path.expanduser("~"), DSH_HOME_DIR_NAME)
    return "~/.dsh" if os.path.abspath(dsh_home) == os.path.abspath(default) else dsh_home


def resolve_config(config: dict | None = None) -> dict:
    config = dict(config or {})
    dsh_home = resolve_dsh_home(config.get("dshHome"))
    markers = config.get("projectRootMarkers")
    return {
        "dshHome": dsh_home,
        "projectRootMarkers": list(markers) if markers is not None
        else list(DEFAULT_PROJECT_ROOT_MARKERS),
        "instructionFileCandidates": _resolve_instruction_candidates(
            config.get("instructionFileCandidates"), DEFAULT_INSTRUCTION_FILE_CANDIDATES),
        "localInstructionFileCandidates": _resolve_instruction_candidates(
            config.get("localInstructionFileCandidates"),
            DEFAULT_LOCAL_INSTRUCTION_FILE_CANDIDATES),
        "maxBytes": config.get("maxBytes"),
        "maxSourceBytes": config.get("maxSourceBytes", DEFAULT_MAX_SOURCE_BYTES),
    }


def workspace_baseline_identity(resolved: dict, cwd: str, project_root: str) -> str:
    import json
    return json.dumps({
        "projectRoot": os.path.relpath(project_root, cwd),
        "projectRootMarkers": resolved["projectRootMarkers"],
        "maxBytes": resolved["maxBytes"],
        "maxSourceBytes": resolved["maxSourceBytes"],
        "instructionFileCandidates": resolved["instructionFileCandidates"],
        "localInstructionFileCandidates": resolved["localInstructionFileCandidates"],
    }, ensure_ascii=False, separators=(",", ":"))


# ---------- digest ----------


def instruction_content_sha1(content: str) -> str:
    return hashlib.sha1(content.encode("utf-8")).hexdigest()


def trimmed_instruction_digest(content: str) -> str:
    return instruction_content_sha1(content.strip())


# ---------- paths / discovery ----------


def find_project_root(cwd: str, markers: list) -> str:
    """向上走到第一个含根标记的目录；无标记返回 cwd（files.ts:181-196）。"""
    current = os.path.abspath(cwd)
    while True:
        for marker in markers:
            if os.path.exists(os.path.join(current, marker)):
                return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.abspath(cwd)
        current = parent


def ancestor_chain(root: str, cwd: str) -> list:
    chain = []
    current = os.path.abspath(cwd)
    resolved_root = os.path.abspath(root)
    while current != resolved_root:
        chain.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    chain.append(resolved_root)
    return list(reversed(chain))


def descendant_dirs_between(root: str, touched_path: str) -> list:
    resolved_root = os.path.abspath(root)
    target = os.path.abspath(touched_path) if os.path.isabs(touched_path) \
        else os.path.abspath(os.path.join(resolved_root, touched_path))
    target_dir = os.path.dirname(target)
    rel = os.path.relpath(target_dir, resolved_root)
    if rel == "." or rel.startswith("..") or os.path.isabs(rel):
        return []
    return ancestor_chain(resolved_root, target_dir)[1:]


def relative_display(root: str, path: str) -> str:
    return os.path.relpath(path, root)


def _stat_file(path: str, file_system: Any = None) -> dict:
    """返回 {kind: present|absent|unavailable, ...}（宿主文件系统直接探测）。"""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return {"kind": "absent"}
    except OSError:
        return {"kind": "unavailable"}
    if not os.path.isfile(path):
        return {"kind": "absent"}
    return {"kind": "present", "size": info.st_size}


def _exists_as_marker(path: str, file_system: Any = None) -> bool:
    return os.path.exists(path)


def _discover_files(options: dict, file_system: Any = None) -> list:
    resolved = resolve_config(options)
    files: list = []
    seen: set = set()

    def add(file: dict) -> None:
        if file["absolutePath"] in seen:
            return
        seen.add(file["absolutePath"])
        files.append(file)

    user_global = os.path.join(resolved["dshHome"], USER_GLOBAL_FILE)
    probe = _stat_file(user_global, file_system)
    if probe["kind"] == "present":
        add({"absolutePath": user_global,
             "displayPath": f"{_dsh_home_display(resolved['dshHome'])}/AGENTS.md",
             **{k: v for k, v in probe.items() if k != "kind"}})

    cwd = os.path.abspath(options["cwd"])
    project_root = options.get("projectRoot")
    if project_root is None:
        project_root = find_project_root(cwd, resolved["projectRootMarkers"])
    for directory in ancestor_chain(project_root, cwd):
        for candidates in (resolved["instructionFileCandidates"],
                           resolved["localInstructionFileCandidates"]):
            for candidate in candidates:
                path = os.path.join(directory, candidate)
                probe = _stat_file(path, file_system)
                if probe["kind"] == "present":
                    add({"absolutePath": path,
                         "displayPath": relative_display(project_root, path),
                         **{k: v for k, v in probe.items() if k != "kind"}})
    return files


def discover_baseline_instruction_files(options: dict) -> list:
    return [{"absolutePath": file["absolutePath"], "displayPath": file["displayPath"]}
            for file in _discover_files(options)]


def dedup_instruction_files_by_directory(files: list) -> list:
    kept_digests: dict = {}
    kept: list = []
    for file in files:
        directory = os.path.dirname(file["displayPath"])
        digests = kept_digests.setdefault(directory, set())
        digest = trimmed_instruction_digest(file["content"])
        if digest in digests:
            continue
        digests.add(digest)
        kept.append(file)
    return kept


def _read_bounded(file: dict, max_source_bytes: int, file_system: Any = None) -> str | None:
    if file.get("size") is not None and file["size"] > max_source_bytes:
        return None
    try:
        with open(file["absolutePath"], "rb") as handle:
            raw = handle.read(max_source_bytes + 1)
    except OSError:
        return None
    if len(raw) > max_source_bytes:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def load_baseline_instruction_set(options: dict, file_system: Any = None) -> dict | None:
    resolved = resolve_config(options)
    max_bytes = resolved["maxBytes"]
    max_source = resolved["maxSourceBytes"]
    if max_bytes is None or max_bytes <= 0 or not max_source or max_source <= 0:
        return None
    discovered = _discover_files(options, file_system)
    loaded: list = []
    for file in discovered:
        content = _read_bounded(file, max_source, file_system)
        if content is not None:
            entry = {"absolutePath": file["absolutePath"], "displayPath": file["displayPath"],
                     "content": content}
            if file.get("version") is not None:
                entry["version"] = file["version"]
            loaded.append(entry)
    deduped = dedup_instruction_files_by_directory(loaded)
    replace = options.get("replacePreviousBaseline")
    if not deduped:
        if replace is not True:
            return None
        rendered, included = render_agent_instruction_set(
            [], {"maxBytes": max_bytes, "replacePreviousBaseline": True})
        return {"rendered": rendered, "observed": [], "included": included}
    rendered, included = render_agent_instruction_set(
        deduped, {"maxBytes": max_bytes, "replacePreviousBaseline": replace})
    return {"rendered": rendered, "observed": loaded, "included": included}


# ---------- scope keys ----------


def scope_for_display_path(display_path: str) -> str:
    if display_path in ("~/.dsh/AGENTS.md", "$DSH_HOME/AGENTS.md"):
        return USER_GLOBAL_DIRECTORY
    # node path.dirname 对裸文件名为 "."；Python os.path.dirname 为 ""，须归一。
    return os.path.dirname(display_path) or "."


def candidate_scope_key(directory: str, candidate_name: str) -> str:
    return f"{directory}{_SCOPE_SEPARATOR}{candidate_name}"


def instruction_scope_key(display_path: str) -> str:
    return candidate_scope_key(scope_for_display_path(display_path), os.path.basename(display_path))


def decode_scope_key(scope: str) -> dict:
    separator = scope.find(_SCOPE_SEPARATOR)
    if separator < 0:
        return {"directory": scope, "candidateName": ""}
    return {"directory": scope[:separator], "candidateName": scope[separator + 1:]}


# ---------- rendering ----------


def _byte_length(value: str) -> int:
    return len(value.encode("utf-8"))


def _truncate_utf8(value: str, max_bytes: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    end = max(0, int(max_bytes))
    while end > 0 and (raw[end] & 0xC0) == 0x80:
        end -= 1
    return raw[:end].decode("utf-8", errors="ignore")


def _escape_frame_body(body: str) -> str:
    return body.replace(_SYSTEM_REMINDER_CLOSE, "<\\/system-reminder>")


def _section_text(file: dict) -> str:
    return f"Instructions from: {file['displayPath']}\n\n{file['content']}"


def _additional_section_text(file: dict) -> str:
    scope = scope_for_display_path(file["displayPath"])
    return "\n".join([
        f"Additional instructions from: {file['displayPath']}",
        "",
        f"These instructions apply to work under `{scope}`. Use them as guidance when relevant; "
        "more specific instructions take precedence. They do not override system, developer, or "
        "direct user instructions.",
        "",
        file["content"],
    ])


def _changed_section_text(item: dict) -> str:
    change, file = item["change"], item["file"]
    if change["action"] == "set":
        return _additional_section_text(file)
    if change["action"] == "remove":
        return (f"Instructions removed: {change['path']}\n\n"
                "The previously loaded instructions from this file no longer apply.")
    return "\n".join([
        f"Updated instructions from: {change['path']}",
        "",
        "This file changed after it was loaded. Use the following content instead of the previously "
        "loaded instructions from this file.",
        "",
        file["content"],
    ])


def _marker_text(max_bytes: int, omitted: list, truncated: list) -> str:
    if not omitted and not truncated:
        return ""
    parts = []
    if omitted:
        parts.append("omitted " + ", ".join(file["displayPath"] for file in omitted))
    if truncated:
        parts.append("truncated " + ", ".join(
            f"{item['displayPath']} from {item['originalBytes']} to {item['includedBytes']} bytes"
            for item in truncated))
    return f"Workspace instruction budget {max_bytes} bytes: " + "; ".join(parts)


def _build_instruction_text(files: list, max_bytes: int, omitted: list, truncated: list,
                            intro: str, section) -> str:
    marker = _marker_text(max_bytes, omitted, truncated)
    blocks = [marker, intro, *(section(file) for file in files)]
    body = "\n\n".join(block for block in blocks if len(block) > 0)
    return "\n".join([_SYSTEM_REMINDER_OPEN, _escape_frame_body(body), _SYSTEM_REMINDER_CLOSE])


def _with_truncated(file: dict, included_bytes: int) -> dict:
    return {**file, "content": _truncate_utf8(file["content"], included_bytes)}


def _truncate_to_fit(file: dict, included_files: list, max_bytes: int, omitted: list, intro: str,
                     section) -> dict:
    original_bytes = _byte_length(file["content"])
    low, high = 0, original_bytes
    best = _with_truncated(file, 0)
    while low <= high:
        mid = (low + high) // 2
        candidate = _with_truncated(file, mid)
        truncated = [{"displayPath": file["displayPath"], "originalBytes": original_bytes,
                      "includedBytes": _byte_length(candidate["content"])}]
        text = _build_instruction_text([*included_files, candidate], max_bytes, omitted,
                                       truncated, intro, section)
        if _byte_length(text) <= max_bytes:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def _render_instruction_context(files: list, max_bytes: int, intro: str, section) -> dict:
    if max_bytes <= 0:
        return {"text": "", "omitted": files, "truncated": [], "represented": []}
    full_text = _build_instruction_text(files, max_bytes, [], [], intro, section)
    if _byte_length(full_text) <= max_bytes:
        return {"text": full_text, "omitted": [], "truncated": [], "represented": files}
    for start in range(1, len(files)):
        included = files[start:]
        omitted = [{"absolutePath": f["absolutePath"], "displayPath": f["displayPath"]}
                   for f in files[:start]]
        suffix = _build_instruction_text(included, max_bytes, omitted, [], intro, section)
        if _byte_length(suffix) <= max_bytes:
            return {"text": suffix, "omitted": omitted, "truncated": [], "represented": included}
    most_specific = files[-1] if files else None
    if most_specific is None:
        return {"text": "", "omitted": [], "truncated": [], "represented": []}
    omitted = [{"absolutePath": f["absolutePath"], "displayPath": f["displayPath"]}
               for f in files[:-1]]
    original_bytes = _byte_length(most_specific["content"])
    for candidate_intro in (intro, _COMPACT_INTRO):
        truncated_file = _truncate_to_fit(most_specific, [], max_bytes, omitted, candidate_intro,
                                          section)
        included_bytes = _byte_length(truncated_file["content"])
        truncated = [{"displayPath": most_specific["displayPath"], "originalBytes": original_bytes,
                      "includedBytes": included_bytes}]
        text = _build_instruction_text([truncated_file], max_bytes, omitted, truncated,
                                       candidate_intro, section)
        if _byte_length(text) <= max_bytes:
            represented = [most_specific] if (included_bytes > 0 or original_bytes == 0) else []
            return {"text": text, "omitted": omitted, "truncated": truncated,
                    "represented": represented}
    truncated = [{"displayPath": most_specific["displayPath"], "originalBytes": original_bytes,
                  "includedBytes": 0}]
    compact_notice = _escape_frame_body(_marker_text(max_bytes, omitted, truncated))
    compact_with_heading = _escape_frame_body(
        "\n\n".join([compact_notice, section(_with_truncated(most_specific, 0))]))
    if _byte_length(compact_with_heading) <= max_bytes:
        represented = [most_specific] if original_bytes == 0 else []
        return {"text": compact_with_heading, "omitted": omitted, "truncated": truncated,
                "represented": represented}
    text = compact_notice if _byte_length(compact_notice) <= max_bytes \
        else _truncate_utf8(compact_notice, max_bytes)
    return {"text": text, "omitted": omitted, "truncated": truncated, "represented": []}


def render_agent_instruction_set(files: list, options: dict) -> tuple:
    replace = options.get("replacePreviousBaseline")
    intro = _AGENT_INSTRUCTIONS_INTRO
    if replace is True:
        intro = _EMPTY_REPLACEMENT_INTRO if not files else _REPLACEMENT_INTRO
    rendered = _render_instruction_context(files, options["maxBytes"], intro, _section_text)
    included = rendered.pop("represented")
    return rendered, included


def render_instruction_changes(items: list, max_bytes: int) -> dict:
    by_path = {item["file"]["absolutePath"]: item for item in items}

    def section(file: dict) -> str:
        item = by_path.get(file["absolutePath"])
        return "" if item is None else _changed_section_text({**item, "file": file})

    rendered = _render_instruction_context([item["file"] for item in items], max_bytes, "", section)
    represented = {file["absolutePath"] for file in rendered["represented"]}
    return {"text": rendered["text"],
            "changes": [item["change"] for item in items
                        if item["file"]["absolutePath"] in represented]}


# ---------- state / reconcile ----------


def agent_instructions_message(text: str) -> dict:
    return create_message("user", [text_block(text)], {"kind": "plugin", "plugin": NAME})


def _is_agent_instructions_source(source: Any) -> bool:
    from collections.abc import Mapping
    return (isinstance(source, Mapping) and source.get("kind") == "agent-instructions"
            and isinstance(source.get("changes"), (list, tuple)))


def _workspace_instruction_changes(source: Any) -> list:
    from collections.abc import Mapping
    changes = []
    for value in source.get("changes", []):
        if not isinstance(value, Mapping):
            continue
        if value.get("action") not in ("set", "replace", "remove"):
            continue
        if not isinstance(value.get("scope"), str) or not isinstance(value.get("path"), str):
            continue
        if value.get("digest") is not None and not isinstance(value.get("digest"), str):
            continue
        change = {"action": value["action"], "scope": value["scope"], "path": value["path"]}
        if value.get("digest") is not None:
            change["digest"] = value["digest"]
        changes.append(change)
    return changes


def _visible_instruction_changes(agent: Any, authority_messages: list) -> dict:
    from collections.abc import Mapping
    visible: dict = {}
    for node in agent.session.surface_nodes():
        seq = node.get("seq") if isinstance(node, Mapping) else node
        event = agent.session.event_at(seq)
        if event is None or event.get("type") != "user/message":
            continue
        source = (event.get("data") or {}).get("source")
        if not _is_agent_instructions_source(source):
            continue
        for change in _workspace_instruction_changes(source):
            visible[change["scope"]] = change
    for message in authority_messages:
        source = message.get("source") if isinstance(message, dict) else None
        if not _is_agent_instructions_source(source):
            continue
        for change in _workspace_instruction_changes(source):
            visible[change["scope"]] = change
    return visible


def baseline_instruction_state(files: list) -> dict:
    changes: dict = {}
    versions: dict = {}
    for file in files:
        digest = instruction_content_sha1(file["content"])
        change = {"action": "set", "scope": instruction_scope_key(file["displayPath"]),
                  "path": file["displayPath"], "digest": digest}
        changes[change["scope"]] = change
        if file.get("version") is not None:
            versions[change["scope"]] = {
                "path": file["displayPath"], "version": file["version"], "digest": digest,
                "trimmedDigest": trimmed_instruction_digest(file["content"])}
    return {"changes": changes, "versions": versions}


def _probe_scope(scope: str, project_root: str, resolved: dict, file_system: Any) -> dict:
    directory, candidate = decode_scope_key(scope)["directory"], decode_scope_key(scope)["candidateName"]
    if directory == USER_GLOBAL_DIRECTORY:
        base = resolved["dshHome"]
        display = f"{_dsh_home_display(resolved['dshHome'])}/AGENTS.md"
    else:
        base = project_root if directory == "." else os.path.join(project_root, directory)
        display = None
    path = os.path.join(base, candidate)
    probe = _stat_file(path, file_system)
    if probe["kind"] != "present":
        return {"kind": probe["kind"]}
    file = {"absolutePath": path,
            "displayPath": display if display is not None else relative_display(project_root, path),
            "target": probe.get("target"), "version": probe.get("version")}
    if probe.get("size") is not None:
        file["size"] = probe["size"]
    return {"kind": "present", "file": file}


def _relative_scope(project_root: str, directory: str) -> str:
    scope = relative_display(project_root, directory)
    return "." if scope == "" else scope


def reconcile_instruction_context(agent: Any, resolved: dict, version_cache: dict, file_system: Any,
                                  options: dict) -> dict | None:
    session = agent.session
    effective = _visible_instruction_changes(agent, options.get("authorityMessages", []))
    cwd = (getattr(session, "meta", {}) or {}).get("cwd") or os.getcwd()
    project_root = options.get("projectRoot") or find_project_root(
        cwd, resolved["projectRootMarkers"])
    scopes: set = set()
    baseline_scopes: set = set()

    def add_dir_scopes(target: set, directory: str) -> None:
        for candidate in resolved["instructionFileCandidates"]:
            target.add(candidate_scope_key(directory, candidate))
        for candidate in resolved["localInstructionFileCandidates"]:
            target.add(candidate_scope_key(directory, candidate))

    baseline_scopes.add(candidate_scope_key(USER_GLOBAL_DIRECTORY, USER_GLOBAL_FILE))
    for directory in ancestor_chain(project_root, cwd):
        add_dir_scopes(baseline_scopes, _relative_scope(project_root, directory))
    if options.get("includeBaselineScopes"):
        scopes.update(baseline_scopes)
    for message in options.get("scopeMessages", []):
        source = message.get("source") if isinstance(message, dict) else None
        if not _is_agent_instructions_source(source):
            continue
        for change in _workspace_instruction_changes(source):
            if not options.get("includeBaselineScopes") and change["scope"] in baseline_scopes:
                continue
            scopes.add(change["scope"])
    for scope in effective:
        if not options.get("includeBaselineScopes") and scope in baseline_scopes:
            continue
        directory = decode_scope_key(scope)["directory"]
        if directory == USER_GLOBAL_DIRECTORY:
            scopes.add(candidate_scope_key(USER_GLOBAL_DIRECTORY, USER_GLOBAL_FILE))
        else:
            add_dir_scopes(scopes, directory)
    for touched in options.get("touchedPaths", []):
        for directory in descendant_dirs_between(cwd, touched):
            add_dir_scopes(scopes, _relative_scope(project_root, directory))

    versions = version_cache.setdefault(id(session), {})
    kept_trimmed: dict = {}
    items: list = []
    version_updates: list = []

    def register_kept_trimmed(directory: str, digest: str) -> bool:
        digests = kept_trimmed.setdefault(directory, set())
        if digest in digests:
            return True
        digests.add(digest)
        return False

    def push_removal(scope: str, path: str) -> None:
        change = {"action": "remove", "scope": scope, "path": path}
        items.append({"change": change,
                      "file": {"absolutePath": f"removed:{scope}", "displayPath": path,
                               "content": ""}})
        version_updates.append({"change": change})

    scopes_by_directory: dict = {}
    for scope in scopes:
        directory = decode_scope_key(scope)["directory"]
        scopes_by_directory.setdefault(directory, []).append(scope)

    for directory, directory_scopes in scopes_by_directory.items():
        probed: list = []
        for scope in directory_scopes:
            excluded = options.get("excludedBaselineScopes")
            if excluded is not None and scope in baseline_scopes and scope in excluded:
                previous = effective.get(scope)
                if previous is None or previous["action"] == "remove":
                    versions.pop(scope, None)
                else:
                    push_removal(scope, previous["path"])
            else:
                probed.append(scope)
        item_start = len(items)
        update_start = len(version_updates)
        prior = {scope: versions.get(scope) for scope in probed}
        for scope in probed:
            previous = effective.get(scope)
            probe = _probe_scope(scope, project_root, resolved, file_system)
            if probe["kind"] == "unavailable":
                if previous is None or previous["action"] == "remove":
                    continue
                del items[item_start:]
                del version_updates[update_start:]
                for candidate_scope, state in prior.items():
                    if state is None:
                        versions.pop(candidate_scope, None)
                    else:
                        versions[candidate_scope] = state
                kept_trimmed.pop(directory, None)
                break
            if probe["kind"] == "absent":
                if previous is None or previous["action"] == "remove":
                    versions.pop(scope, None)
                else:
                    push_removal(scope, previous["path"])
                continue
            file = probe["file"]
            cached = versions.get(scope)
            if (cached is not None and cached["path"] == file["displayPath"]
                    and cached["version"] == file.get("version")
                    and previous is not None and previous["action"] != "remove"
                    and previous["path"] == cached["path"]
                    and previous["digest"] == cached["digest"]):
                if register_kept_trimmed(directory, cached["trimmedDigest"]):
                    push_removal(scope, previous["path"])
                continue
            loaded = _read_bounded(file, resolved["maxSourceBytes"], file_system)
            if loaded is None:
                continue
            loaded_entry = {"absolutePath": file["absolutePath"], "displayPath": file["displayPath"],
                            "content": loaded, "version": file.get("version")}
            current_digest = instruction_content_sha1(loaded)
            trimmed = trimmed_instruction_digest(loaded)
            if register_kept_trimmed(directory, trimmed):
                if previous is not None and previous["action"] != "remove":
                    push_removal(scope, previous["path"])
                else:
                    versions.pop(scope, None)
                continue
            next_version = {"path": file["displayPath"], "version": file.get("version"),
                            "digest": current_digest, "trimmedDigest": trimmed}
            if (previous is not None and previous["action"] != "remove"
                    and previous["path"] == file["displayPath"]
                    and previous["digest"] == current_digest):
                versions[scope] = next_version
                continue
            action = "set" if previous is None or previous["action"] == "remove" else "replace"
            change = {"action": action, "scope": scope, "path": file["displayPath"],
                      "digest": current_digest}
            items.append({"change": change, "file": loaded_entry})
            version_updates.append({"change": change, "state": next_version})

    if not items:
        return None
    rendered = render_instruction_changes(items, resolved["maxBytes"])
    if rendered["text"] == "" or not rendered["changes"]:
        return None
    retained = [update for update in version_updates
                if any(update["change"] == change for change in rendered["changes"])]
    return {"context": create_message("user", [text_block(rendered["text"])], {
                "kind": "agent-instructions", "form": "instructions",
                "changes": rendered["changes"]}),
            "versionUpdates": retained}


# ---------- plugin driver ----------


def _visible_baseline_source(agent: Any, authority: list) -> dict | None:
    """最近一条可见基线来源：先看 claimed，再看会话 surface（state.ts visibleBaselineSource）。"""
    from collections.abc import Mapping
    for message in reversed(authority):
        source = message.get("source") if isinstance(message, dict) else None
        if (isinstance(source, Mapping) and source.get("kind") == "agent-instructions"
                and source.get("baseline") is True):
            return source
    nodes = agent.session.surface_nodes()
    for node in reversed(nodes):
        seq = node.get("seq") if isinstance(node, Mapping) else node
        event = agent.session.event_at(seq)
        if event is None or event.get("type") != "user/message":
            continue
        source = (event.get("data") or {}).get("source")
        if (isinstance(source, Mapping) and source.get("kind") == "agent-instructions"
                and source.get("baseline") is True):
            return source
    return None


def _file_path_from_exec(exec_: Any) -> str | None:
    name = getattr(exec_, "name", None)
    if name not in ("read", "write", "edit"):
        return None
    arguments = getattr(exec_, "arguments", None)
    if not isinstance(arguments, dict):
        return None
    value = arguments.get("file_path")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def apply_agent_instructions(ctx: Context, config: dict | None = None) -> None:
    resolved = resolve_config(config)
    version_cache: dict = {}
    touched: dict = {}
    baseline_preparations: dict = {}

    def _compose(agent: Any, authority: list, scope_messages: list, touched_paths: list) -> dict | None:
        max_bytes = resolved["maxBytes"]
        if max_bytes is None or max_bytes <= 0:
            return None
        cwd = (getattr(agent.session, "meta", {}) or {}).get("cwd") or os.getcwd()
        project_root = find_project_root(cwd, resolved["projectRootMarkers"])
        identity = workspace_baseline_identity(resolved, cwd, project_root)
        visible_baseline = _visible_baseline_source(agent, authority)
        keep_visible = (visible_baseline is not None
                        and visible_baseline.get("baselineIdentity") == identity)
        content: list = []
        changes: list = []
        desired_baseline = False
        if not keep_visible:
            instructions = load_baseline_instruction_set({
                "cwd": cwd, "dshHome": resolved["dshHome"],
                "projectRootMarkers": resolved["projectRootMarkers"],
                "maxBytes": max_bytes, "maxSourceBytes": resolved["maxSourceBytes"],
                "instructionFileCandidates": resolved["instructionFileCandidates"],
                "localInstructionFileCandidates": resolved["localInstructionFileCandidates"],
                "projectRoot": project_root,
                "replacePreviousBaseline": visible_baseline is not None,
            }, None)
            baseline = baseline_instruction_state(instructions["included"] if instructions else [])
            observed = baseline_instruction_state(instructions["observed"] if instructions else [])
            excluded = set(observed["changes"]) - set(baseline["changes"])
            baseline_preparations[id(agent.session)] = {"identity": identity, "excluded": excluded}
            versions = version_cache.setdefault(id(agent.session), {})
            versions.update(baseline["versions"])
            if instructions is not None and instructions["rendered"]["text"]:
                baseline_message = agent_instructions_message(instructions["rendered"]["text"])
                content.extend(baseline_message["content"])
                changes.extend(baseline["changes"].values())
                authority = [*authority, baseline_message]
                desired_baseline = True
        update = reconcile_instruction_context(agent, resolved, version_cache, None, {
            "authorityMessages": authority, "scopeMessages": scope_messages,
            "includeBaselineScopes": keep_visible,
            "touchedPaths": touched_paths, "projectRoot": project_root})
        if update is not None:
            content.extend(update["context"]["content"])
            changes.extend(update["context"]["source"]["changes"])
            session_versions = version_cache.setdefault(id(agent.session), {})
            for item in update["versionUpdates"]:
                if item.get("state") is None:
                    session_versions.pop(item["change"]["scope"], None)
                else:
                    session_versions[item["change"]["scope"]] = item["state"]
        if not content:
            return None
        source = {"kind": "agent-instructions", "form": "instructions", "changes": changes}
        if desired_baseline:
            source["baseline"] = True
            source["baselineIdentity"] = identity
        return create_message("user", content, source)

    def _is_instructions(message: Any) -> bool:
        source = message.get("source") if isinstance(message, dict) else None
        return isinstance(source, dict) and source.get("kind") == "agent-instructions"

    def _sync_inbox(agent: Any, claimed: list, desired: dict | None) -> None:
        pending = [m for m in agent.inbox.next_step if _is_instructions(m)]
        already = desired is not None and any(m.get("content") == desired.get("content")
                                              for m in claimed)
        if desired is None or already:
            for message in pending:
                agent.inbox.remove(message["id"])
            return
        reusable = next((m for m in pending if m.get("content") == desired.get("content")), None)
        if reusable is not None:
            for message in pending:
                if message is not reusable:
                    agent.inbox.remove(message["id"])
            return
        if not pending:
            agent.inbox.prepend("next-step", desired)
        else:
            agent.inbox.replace(pending[0]["id"], desired)
            for message in pending[1:]:
                agent.inbox.remove(message["id"])

    async def _on_pre_step(payload: dict, next_fn) -> dict:
        decision = await next_fn()
        agent = payload.get("agent")
        if agent is None:
            return decision
        messages = decision.get("messages") if isinstance(decision, dict) else None
        claimed = list(messages) if isinstance(messages, list) else []
        pending = [m for m in agent.inbox.next_step if _is_instructions(m)]
        paths = touched.pop(id(agent), [])
        desired = _compose(agent, claimed, pending, paths)
        if isinstance(decision, dict) and decision.get("kind") == "reject":
            _sync_inbox(agent, claimed, desired)
            return decision
        for message in pending:
            agent.inbox.remove(message["id"])
        if desired is None:
            return decision
        return {**decision, "messages": [*claimed, desired]}

    def _on_post_execute(payload: Any, next_: Any) -> Any:
        downstream = next_()
        exec_ = payload.get("exec") if isinstance(payload, dict) else None
        result = payload.get("result") if isinstance(payload, dict) else None
        is_error = bool(result.get("isError")) if isinstance(result, dict) else False
        if exec_ is not None and not is_error:
            path = _file_path_from_exec(exec_)
            agent = getattr(exec_, "agent", None)
            if path is not None and agent is not None:
                touched.setdefault(id(agent), []).append(path)
        return downstream

    ctx.on("agent/pre-step", _on_pre_step, prepend=True)
    ctx.on("tools/post-execute", _on_post_execute, prepend=True)


def install_agent_instructions(ctx: Context, config: dict | None = None) -> None:
    if getattr(ctx, "_miniharness_agent_instructions_installed", False):
        return
    apply_agent_instructions(ctx, config)
    ctx._miniharness_agent_instructions_installed = True
