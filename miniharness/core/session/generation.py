"""多代 generation 选择与 migrate-on-open（上游 session-persistence-jsonl/src/generation.ts
+ session-format/src/filename.ts 的 mini 同步载体）。

Phase A 裁定（design-generation-migration.md）：跨进程写租约 `session.lock` 架构不适用
（单进程写手）；win32 专属发布原语不移植——发布用「staged 临时文件 + fsync +
`os.replace` 原子替换」（上游为 link 独占发布；单进程语义等价，登记载体差异）。

错误三分类（上游同名）：
  * `JsonlGenerationNewerVersionError` —— 最高代比本构建新（`session log format vN is
    newer than current vM`）；
  * `JsonlGenerationUnsupportedMigrationError` —— 制品完好但格式边拒绝内容；
  * `JsonlGenerationTargetConflictError` —— 当前代文件名已存在且既非本迁移产物
    （`current session generation already exists at "<path>": <reason>`）。
"""
from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Callable

from .released import (
    SessionFormatError,
    SessionFormatUnsupportedMigrationError,
    SESSION_FORMAT_CATALOG,
    count,
    decode_released_header,
    fail,
    is_json_object,
)

__all__ = [
    "CURRENT_GENERATION_VERSION",
    "JsonlGenerationNewerVersionError",
    "JsonlGenerationTargetConflictError",
    "JsonlGenerationUnsupportedMigrationError",
    "ensure_generation_current",
    "generation_log_filename",
    "log_suffix",
    "parse_generation_log_filename",
    "resolve_generation_in_directory",
]

#: 本构建写出的当前代版本（SESSION_FORMAT_VERSION）。
CURRENT_GENERATION_VERSION = 2

_CANONICAL_LOG_FILENAME = re.compile(r"^session(?:\.v([1-9][0-9]*))?\.jsonl$")


def log_suffix(compression: str) -> str:
    """压缩后缀（追加在 canonical stem `session[.vN].jsonl` 之后）。

    mini 载体约定 `.zstd`（上游 Node 侧为 `.zst`——跨实现互读按字节容器而非
    文件名，登记 verified-diffs §2.24）。
    """
    return ".zstd" if compression == "zstd" else ""


def generation_log_filename(version: int, compression: str) -> str:
    """canonical 代文件名：v0 = `session.jsonl`；vN = `session.vN.jsonl`（+压缩后缀）。"""
    count(version, "Session log generation version")
    base = "session.jsonl" if version == 0 else f"session.v{version}.jsonl"
    return base + log_suffix(compression)


def parse_generation_log_filename(filename: str, compression: str) -> int | None:
    """canonical 代文件名 → 版本；临时/大写/前导零/`.v0`/对立编码名 → None。"""
    stem = filename
    suffix = log_suffix(compression)
    if suffix:
        if not stem.endswith(suffix):
            return None
        stem = stem[: -len(suffix)]
    match = _CANONICAL_LOG_FILENAME.match(stem)
    if match is None:
        return None
    if match.group(1) is None:
        return 0
    return int(match.group(1))


def resolve_generation_in_directory(directory: Path, compression: str,
                                    ) -> dict | None:
    """目录内选数值最高 canonical generation（上游 resolveGenerationInDirectory）。

    命中对立编码 canonical 名 → 编码互斥错误（上游 encodingMismatch）；无制品 → None。
    """
    opposite_suffix = log_suffix("zstd" if compression == "none" else "none")
    generations: list[tuple[int, Path]] = []
    opposite: Path | None = None
    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            continue
        if parse_generation_log_filename(entry.name, compression) is not None:
            generations.append((parse_generation_log_filename(entry.name, compression), entry))
        elif entry.name.endswith(opposite_suffix) and _stem_is_canonical(
                entry.name[: -len(opposite_suffix)]):
            opposite = entry
    if opposite is not None:
        raise ValueError(
            f'session artifact "{opposite}" uses {opposite_suffix}, '
            f'but this backend is configured for compression "{compression}"; '
            "use a separate root or select the matching compression mode"
        )
    if not generations:
        return None
    version, path = max(generations, key=lambda pair: pair[0])
    return {"source_path": path, "source_version": version,
            "current_path": directory / generation_log_filename(
                CURRENT_GENERATION_VERSION, compression)}


def _stem_is_canonical(stem: str) -> bool:
    return _CANONICAL_LOG_FILENAME.match(stem) is not None


class JsonlGenerationNewerVersionError(Exception):
    """最高代比本构建新（上游同名）。"""

    def __init__(self, stored_version: int, current_version: int, stored_id: str) -> None:
        super().__init__(
            f"session log format v{stored_version} is newer than current v{current_version}")
        self.stored_version = stored_version
        self.current_version = current_version
        self.stored_id = stored_id


class JsonlGenerationUnsupportedMigrationError(Exception):
    """制品完好但格式边拒绝内容（上游同名）。"""

    def __init__(self, from_version: int, reason: Exception) -> None:
        super().__init__(str(reason))
        self.from_version = from_version
        self.reason = reason


class JsonlGenerationTargetConflictError(Exception):
    """当前代文件名已存在且既非本迁移产物（上游同名）。"""

    def __init__(self, path: Path, reason: Exception) -> None:
        super().__init__(
            f'current session generation already exists at "{path}": {reason}')
        self.path = path
        self.reason = reason


def _parse_json(text: str, subject: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as error:
        raise fail(f"corrupt session log: {subject} is not valid JSON") from error


def stored_version(header: Any) -> int:
    """版本判别（无任何版本特定字段校验；generation.ts storedVersion）。"""
    if not is_json_object(header):
        raise fail("corrupt session log: first line is not a JSON object")
    return count(header.get("version"), "corrupt session log: header version")


def stored_id(header: Any) -> str:
    return str(header.get("id"))


def split_records(bytes_text: str) -> list[str]:
    """以换行拆分记录（尾换行已由解码层保证）。"""
    return bytes_text[:-1].split("\n") if bytes_text.endswith("\n") else bytes_text.split("\n")


def parse_generation(lines: list[str], recover_suffix: bool) -> dict:
    """解析 header + 行序列（generation.ts parseGeneration 的可恢复语义）。"""
    if not lines:
        raise fail("empty or header-less session log")
    header = _parse_json(lines[0], "header line")
    stored_version(header)
    rows: list[Any] = []
    issue: Exception | None = None
    for index, record in enumerate(lines[1:]):
        try:
            row = _parse_json(record, f"row {index + 1}")
        except SessionFormatError as error:
            if not recover_suffix:
                raise
            issue = issue if issue is not None else error
            continue
        if issue is not None:
            if is_json_object(row) and row.get("type") == "turn/end":
                raise issue
            continue
        rows.append(row)
    return {"header": header, "rows": rows}


def decode_generation_bytes(data: bytes, compression: str) -> tuple[str, bool]:
    """物理解码 → 逻辑 JSONL 文本（完整帧拼接；torn 残帧前缀恢复到行界）。

    返回 `(text, torn)`。zstd：逐帧解压拼接（首帧必须恰一行 header——上游
    assertIndependentHeaderFrame）；torn 尾 = 残帧可解前缀的最后一个换行处截断。
    明文：截到最后换行。
    """
    from .zstd_frames import decompress_zstd_frame, decompress_zstd_prefix, scan_zstd_frames
    if compression == "zstd":
        scan = scan_zstd_frames(data)
        frames, torn_start = scan.frames, scan.torn_start
        if not frames:
            raise fail("empty or header-less Zstandard session log")
        complete: list[bytes] = []
        for index, frame in enumerate(frames):
            plaintext = decompress_zstd_frame(data[frame.start:frame.end])
            if index == 0 and plaintext.count(b"\n") != 1:
                raise fail(
                    "corrupt Zstandard session log: first frame is not exactly one header line")
            complete.append(plaintext)
        complete_bytes = b"".join(complete)
        if not complete_bytes.endswith(b"\n"):
            raise fail("corrupt Zstandard session log: complete frame contains a torn JSONL record")
        if torn_start is None:
            return complete_bytes.decode("utf-8"), False
        try:
            recovered = decompress_zstd_prefix(data[torn_start:])
        except Exception:  # noqa: BLE001 - 结构性残帧可能无可解前缀
            return complete_bytes.decode("utf-8"), True
        newline = recovered.rfind(b"\n")
        if newline == -1:
            return complete_bytes.decode("utf-8"), True
        return (complete_bytes + recovered[:newline + 1]).decode("utf-8"), True
    newline = data.rfind(b"\n")
    if newline == -1:
        raise fail("empty or header-less session log")
    return data[:newline + 1].decode("utf-8"), newline + 1 != len(data)


def encode_generation_bytes(header_json: str, body_text: str, compression: str) -> bytes:
    """逻辑 JSONL → 物理（zstd：header 独立帧 + body 帧；明文直出，header 行尾
    换行两分支一致）。"""
    from .zstd_frames import compress_zstd_frame
    if compression == "none":
        return (header_json + "\n" + body_text).encode("utf-8")
    header_frame = compress_zstd_frame((header_json + "\n").encode("utf-8"))
    if not body_text:
        return header_frame
    return header_frame + compress_zstd_frame(body_text.encode("utf-8"))


def _sync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_synced_temp(current_path: Path, suffix: str, content: bytes) -> Path:
    # Windows 上部分 CRT 构建全局文件模式默认文本态：裸 os.open 的 fd 会把
    # 0x0A 翻译成 \r\n，静默破坏 zstd 帧。二进制 fd 必须显式 O_BINARY
    # （persistence.py 同款注释；POSIX 上该标志不存在取 0）。
    o_binary = getattr(os, "O_BINARY", 0)
    while True:
        path = current_path.parent / f"session.migration.{secrets.token_hex(8)}{suffix}.tmp"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | o_binary, 0o600)
        except FileExistsError:
            continue
        try:
            os.write(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
        return path


def _validate_staged_current(path: Path, compression: str, expected_bytes: bytes | None = None,
                             ) -> dict:
    """staged/既有当前代校验：物理解码 + 版本 current + v2 全量 restore（现行
    Session 语义——最终防线）。返回 {header, events, inherited_event_count}。"""
    data = path.read_bytes()
    text, torn = decode_generation_bytes(data, compression)
    if torn:
        raise fail("staged current session generation has a torn physical tail")
    lines = split_records(text)
    parsed = parse_generation(lines, recover_suffix=False)
    if stored_version(parsed["header"]) != CURRENT_GENERATION_VERSION:
        raise fail(f"staged session generation is not current v{CURRENT_GENERATION_VERSION}")
    _assert_current_generation(parsed)
    if expected_bytes is not None:
        if len(data) < len(expected_bytes) or not data.startswith(expected_bytes):
            raise fail("target bytes do not begin with the migrated generation")
    return parsed


def _assert_current_generation(parsed: dict) -> None:
    """v2 物理件 → 迁移产物 restore 校验（Phase A 组合防线，§2.24 登记）：

    ① released v2 词表面（51 型）+ surface/provenance scoped 规则；② marker/cut
    双向一致性（`inherited_cut`）；③ assistant/message|attempt 的现行 stream 三事实
    cross-check（content / usage / replayState——与 `Session._replay_seed` 同款）。
    不走 `Session(seed=...)` 全量 restore：mini KNOWN_TYPES 是 30/51 子集，released
    域外来型（feedback/record 等）会被它拒读；全量语义接入属 Phase B。
    """
    from .json import thaw  # noqa: PLC0415
    from .persistence import (  # noqa: PLC0415
        _decode_storage_event,
        _from_header_line,
        _is_header_line,
        inherited_cut,
    )
    from .released import (  # noqa: PLC0415
        RELEASED_V2_EVENT_TYPES,
        assert_scoped_v1_artifact,
    )
    header_value = parsed["header"]
    if not _is_header_line(header_value):
        raise fail("staged session generation header is not a current v2 header")
    meta = _from_header_line(dict(header_value))
    events = [_decode_storage_event(thaw(row)) for row in parsed["rows"]]
    for index, event in enumerate(events):
        if event.get("seq") != index:
            raise fail("staged session generation rows are not dense")
        if event["type"] not in RELEASED_V2_EVENT_TYPES:
            raise fail(
                f'staged session generation row {index} has unknown event type '
                f'"{event["type"]}"')
    cut = inherited_cut(meta, events)
    artifact = {"header": {k: v for k, v in dict(header_value).items() if k != "type"},
                "inherited_event_count": cut, "events": events}
    assert_scoped_v1_artifact(artifact, forbid_assistant_provenance=True)
    # ③ stream 三事实 cross-check（现行 v2 语义；空流跳过——上游同款）
    from ...llm.assistant_stream import expand_assistant_stream  # noqa: PLC0415
    from ...llm.protocol import BlockAssembler  # noqa: PLC0415
    from .session import _json_deep_equal  # noqa: PLC0415
    for event in events:
        if event["type"] not in ("assistant/message", "assistant/attempt"):
            continue
        data = event["data"]
        try:
            timed = expand_assistant_stream(list(data["stream"]))
            assembler = BlockAssembler()
            for member in timed:
                assembler.push(member.chunk)
        except Exception as error:  # noqa: BLE001
            raise fail(
                f"staged {event['type']} at index {event['seq']} has an invalid "
                f"embedded stream: {error}") from error
        if event["type"] == "assistant/attempt" or not timed:
            continue
        message = data.get("message") or {}
        expected = assembler.interrupted_blocks() if data.get("interrupted") is True \
            else assembler.blocks
        if not _json_deep_equal(message.get("content"), expected):
            raise fail(
                f"staged assistant/message at index {event['seq']} content disagrees "
                "with its embedded stream")
        if not _json_deep_equal(data.get("usage"), assembler.usage):
            raise fail(
                f"staged assistant/message at index {event['seq']} usage disagrees "
                "with its embedded stream")
        source = message.get("source") or {}
        if not _json_deep_equal(source.get("replayState"), assembler.replay_state):
            raise fail(
                f"staged assistant/message at index {event['seq']} replay state "
                "disagrees with its embedded stream")


def ensure_generation_current(
    source_path: Path,
    source_version: int,
    current_path: Path,
    compression: str,
    validate_historical_header: Callable[[dict], None] | None = None,
) -> dict:
    """选择/迁移/发布编排（上游 ensureCurrent 的同步单进程载体）。

    返回 `{"status": "current"|"migrated", "path": ...}`；源为当前代 → 不迁移。
    """
    suffix = log_suffix(compression)
    if source_path.name != generation_log_filename(source_version, compression):
        raise fail(
            f'resolved JSONL source path must end with '
            f'"{generation_log_filename(source_version, compression)}": {source_path}')
    if current_path.name != generation_log_filename(CURRENT_GENERATION_VERSION, compression):
        raise fail(
            f'current JSONL generation path must end with '
            f'"{generation_log_filename(CURRENT_GENERATION_VERSION, compression)}": {current_path}')
    if source_path.parent != current_path.parent:
        raise fail("source and current JSONL generations must share one Session directory")
    data = source_path.read_bytes()
    text, _torn = decode_generation_bytes(data, compression)
    lines = split_records(text)
    if not lines:
        raise fail("empty or header-less session log")
    quick_header = _parse_json(lines[0], "header line")
    quick_version = stored_version(quick_header)
    if quick_version != source_version:
        raise fail(
            f"resolved JSONL source filename identifies v{source_version}, "
            f"but its header identifies v{quick_version}: {source_path}")
    if quick_version > CURRENT_GENERATION_VERSION:
        raise JsonlGenerationNewerVersionError(
            quick_version, CURRENT_GENERATION_VERSION, stored_id(quick_header))
    if quick_version == CURRENT_GENERATION_VERSION:
        return {"status": "current", "version": quick_version, "path": str(source_path)}
    if validate_historical_header is not None:
        validate_historical_header(quick_header)
    parsed = parse_generation(lines, recover_suffix=True)
    catalog = SESSION_FORMAT_CATALOG
    try:
        decoded = catalog.decode_recoverable_artifact(parsed["header"], parsed["rows"])
        migrated = catalog.migrate(decoded)
    except SessionFormatUnsupportedMigrationError as error:
        raise JsonlGenerationUnsupportedMigrationError(quick_version, error) from error
    encoded = catalog.encode_current(migrated)
    header_json = json.dumps(encoded["header"], ensure_ascii=False)
    body_text = "".join(
        json.dumps(row, ensure_ascii=False) + "\n" for row in encoded["rows"])
    physical = encode_generation_bytes(header_json, body_text, compression)
    staged = _write_synced_temp(current_path, suffix, physical)
    try:
        _validate_staged_current(staged, compression)
        # 复核源未变后发布；目标已存在 → 校验既有 + 字节前缀匹配（上游
        # reopenExpectedCurrent；匹配即接受为已完成，否则 TargetConflict）
        if current_path.exists():
            try:
                _validate_staged_current(current_path, compression, expected_bytes=physical)
            except Exception as error:  # noqa: BLE001
                raise JsonlGenerationTargetConflictError(current_path, error) from error
            return {"status": "migrated", "from_version": quick_version,
                    "to_version": CURRENT_GENERATION_VERSION, "path": str(current_path),
                    "source_path": str(source_path)}
        os.replace(staged, current_path)
        _sync_dir(current_path.parent)
        return {"status": "migrated", "from_version": quick_version,
                "to_version": CURRENT_GENERATION_VERSION, "path": str(current_path),
                "source_path": str(source_path)}
    finally:
        try:
            os.unlink(staged)
        except FileNotFoundError:
            pass
