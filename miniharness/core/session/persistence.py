"""第 5 章：会话持久化 —— JSONL / SQLite 双后端 + 崩溃恢复。

对应 dsh 真实源码：packages/session/session-persistence(-jsonl)、
packages/core/session/src/chunk-rows.ts、session-persistence-jsonl/src/{format,zstd}.ts。

硬性规定：
  1. append 先复制事件、异步成批写入；flush 是"等待的栅栏"
  2. JSONL 每文件 header 行 + 事件行；header 首键 type:'session'；
     SESSION_FORMAT_VERSION 不符按方向给出升级指引文案并整体拒读
     （fail-closed，上游 format.ts refuseForeignFormatVersion）
  3. 物理载体默认 zstd 拼接帧容器：每个耐久批次一条独立可解码、带校验和的
     帧（后缀 .jsonl.zstd）；compression='none' 退回明文 .jsonl。两种编码
     的根目录互斥共存——发现对立编码制品即响亮拒绝，不猜测不迁移
  4. 存储记录层：写入默认把连续 assistant/chunk 增量串打包为
     text-chunks/reasoning-chunks/tool-call-chunks 行（packChunks=true）；
     读取无条件兼容两种布局（读回逐字节还原原事件）
  5. torn 尾部：明文残行忽略并截断修复；zstd 残帧先前缀恢复完整记录、
     再把恢复事件连同 closers 经 commit_repair 持久化（截断点 = 残帧起点，
     对齐上游 commitRepair(truncateTo, recoveredEvents, closers)）
   6. load 时未知事件类型整体拒绝 —— fail-closed
  7. 目录布局：root/<--projectKey(cwd)-->/<encodeSegment(id)>/session.jsonl[.zstd]；
     cwd 缺省退化到 _no-cwd 项目目录；项目目录下的散置 *.jsonl 制品
     （遗留平铺布局）响亮拒绝

简化说明（有意保留）：目录 fsync 在 Windows 上不可行（无目录句柄），跳过——
上游 win32 分支同样走不同发布路径；encodeSegment 以 Unicode 码点为单位转义
（上游按 UTF-16 码元以保留孤立代理项——文件系统层本就拒绝代理项）。
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import zstandard

from . import (
    KNOWN_TYPES,
    SESSION_FORMAT_VERSION,
    Session,
    repair_interrupted_turn,
    thaw,
    turn_balance,
)
from .chunk_rows import decode_storage_record, pack_chunk_runs
from .json import now_ms
from .seq_ranges import decode_seq_ranges, encode_seq_ranges
from .zstd_frames import (
    compress_zstd_frame,
    decode_frames,
    decompress_zstd_frame,
    decompress_zstd_prefix,
    read_first_frame,
    scan_zstd_frames,
)

# header 基础键（其余键扁平并入 meta；type 是 header 行的记录种类标签，
# 对齐上游 HeaderLine：{type:'session', version, id, createdAt, ...})
_HEADER_BASE_KEYS = frozenset({"type", "version", "id", "createdAt"})

_JSONL_COMPRESSIONS = ("zstd", "none")

# Windows 上部分 CRT 构建把全局文件模式默认为文本态（_fmode=0），裸 os.write
# 会把载荷里的每个 0x0A 翻译成 \r\n，静默破坏帧字节与校验和。所有二进制
# fd 一律显式 O_BINARY（POSIX 上该标志不存在，取 0 即可）。
_O_BINARY = getattr(os, "O_BINARY", 0)


def _encode_storage_event(value: dict) -> dict:
    """焊一到一行写入边界的区间编码：sourceEventSeqs 折成存储态（上游 format.ts
    encodeProvenanceForStorage）。无该字段的记录原样直通。"""
    if "sourceEventSeqs" not in value:
        return value
    out = dict(value)
    out["sourceEventSeqs"] = encode_seq_ranges(value["sourceEventSeqs"])
    return out


def _decode_storage_event(value: dict) -> dict:
    """焊一到一行读出边界的区间解码：存储态 sourceEventSeqs 展开回内存态
    （上游 format.ts expandProvenanceFromStorage，maxEntries = 所属事件 seq）。
    无该字段的记录原样直通；展开即形状校验（[start,end] 对 / end>=start /
    越界，fail-closed）。"""
    if "sourceEventSeqs" not in value:
        return value
    out = dict(value)
    out["sourceEventSeqs"] = decode_seq_ranges(value["sourceEventSeqs"], value.get("seq", 2**53 - 1))
    return out


class SessionFormatUnsupportedError(RuntimeError):
    """会话格式版本超出本构建可读范围（fail-closed，非损坏）。"""


def session_format_version_refusal(session_id: str, version: Any) -> str:
    """版本拒绝的稳定文案（上游 coordinator.ts sessionFormatVersionRefusal）。"""
    return (
        f'session "{session_id}" uses log format v{version}, but this harness reads '
        f"only v{SESSION_FORMAT_VERSION}: the log was written by a newer harness — "
        "upgrade the harness to open it"
        if version > SESSION_FORMAT_VERSION
        else f'session "{session_id}" uses log format v{version}, older than the '
        f"supported v{SESSION_FORMAT_VERSION}, and this build ships no upgrade path for it"
    )


def _flat_header(session_id: str, meta: dict | None, created_at: int | None) -> dict:
    """构造扁平 header（上游 prepare index.ts:877-887：meta 字段散开进 header）。"""
    header: dict[str, Any] = {
        "version": SESSION_FORMAT_VERSION,
        "id": session_id,
        "createdAt": created_at if created_at is not None else now_ms(),
    }
    if meta:
        header.update(meta)
    return header


def _header_meta(header: dict) -> dict | None:
    """从扁平 header 恢复 meta（剔除基础键；空 → None）。"""
    meta = {k: v for k, v in header.items() if k not in _HEADER_BASE_KEYS}
    return meta or None


# ---------- 上游 format.ts 目录布局（packages/session/session-persistence-jsonl）----------

def encode_segment(raw: str) -> str:
    """对齐上游 format.ts `encodeSegment`：任意字符串注入式编码为单段安全目录名。

    安全码点 [A-Za-z0-9._-] 保持字面；其余（含 '~'）一律 '~XXXX' 十六进制
    转义；'.'/'..' 整段特判防穿越。
    """
    if raw == "":
        raise ValueError("cannot encode an empty path segment")
    if raw == ".":
        return "~002E"
    if raw == "..":
        return "~002E~002E"
    out = []
    for ch in raw:
        code = ord(ch)
        if ch != "~" and ("a" <= ch <= "z" or "A" <= ch <= "Z" or "0" <= ch <= "9" or ch in "._-"):
            out.append(ch)
        else:
            out.append(f"~{code:04X}")
    return "".join(out)


def decode_segment(value: str) -> str:
    """`encode_segment` 的逆变换。"""
    if value == "~002E":
        return "."
    if value == "~002E~002E":
        return ".."
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "~":
            out.append(chr(int(value[i + 1 : i + 5], 16)))
            i += 5
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def project_key(cwd: str) -> str:
    """对齐上游 format.ts `projectKey`：人类可读的项目目录名。

    文件系统/盘符分隔符折成 '-'；安全字符字面；其余 '~XXXX' 转义；
    有损截断到 251 字符（跟随常见人类可导航项目目录约定）。
    """
    if cwd == "":
        raise ValueError("cannot encode an empty project path")
    readable = []
    separator_run = False
    for ch in cwd:
        if ch in "/\\:":
            if not separator_run:
                readable.append("-")
            separator_run = True
            continue
        separator_run = False
        if ch != "~" and ("a" <= ch <= "z" or "A" <= ch <= "Z" or "0" <= ch <= "9" or ch in "._-"):
            readable.append(ch)
        else:
            readable.append(f"~{ord(ch):04X}")
    slug = "".join(readable).lstrip("-") or "root"
    return f"--{slug[:251]}--"


def _log_suffix(compression: str | None) -> str:
    """对齐上游 format.ts `logSuffix`：zstd → '.jsonl.zstd'，否则 '.jsonl'。"""
    return ".jsonl.zstd" if compression == "zstd" else ".jsonl"


def _project_dir(root: Path, cwd: str | None) -> Path:
    """对齐上游 format.ts `projectDir`：cwd 空（含 None）→ '_no-cwd'。"""
    if cwd is None or cwd == "":
        return Path(root) / "_no-cwd"
    return Path(root) / project_key(cwd)


def _session_dir(root: Path, cwd: str | None, session_id: str) -> Path:
    """对齐上游 format.ts `sessionDir`：项目目录 → 会话目录。"""
    return _project_dir(root, cwd) / encode_segment(session_id)


def _log_path(root: Path, cwd: str | None, session_id: str, compression: str | None) -> Path:
    """对齐上游 format.ts `logPath`：会话目录下 session.jsonl[.zstd]。"""
    return _session_dir(root, cwd, session_id) / f"session{_log_suffix(compression)}"


def _to_header_line(header: dict) -> dict:
    """构造 header 行对象（上游 toHeaderLine）：type 标签居首、
    delegationDepth 必填（缺省补 0）、可选字段缺席即省略（绝不 null）。"""
    line: dict[str, Any] = {
        "type": "session",
        "version": header["version"],
        "id": header["id"],
        "createdAt": header["createdAt"],
    }
    for optional in ("cwd", "parentSession", "seedLength", "origin", "agentPreset"):
        if optional in header:
            line[optional] = header[optional]
    line["delegationDepth"] = header.get("delegationDepth", 0)
    # mini 扩展 meta 键（label 等）随行携带；上游解析守卫容忍未知额外键。
    for key, value in header.items():
        if key not in line and key not in ("delegationDepth",):
            line[key] = value
    return line


def _is_safe_int(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and -(2**53 - 1) <= value <= 2**53 - 1
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_header_line(value: Any) -> bool:
    """类型守卫：解析出的首行是形状完好的 session header（上游 isHeaderLine）。"""
    if not isinstance(value, dict) or value.get("type") != "session":
        return False
    if not _is_number(value.get("version")):
        return False
    if not isinstance(value.get("id"), str):
        return False
    created_at = value.get("createdAt")
    if not _is_safe_int(created_at) or created_at < 0:
        return False
    depth = value.get("delegationDepth")
    if not _is_safe_int(depth) or depth < 0:
        return False
    origin = value.get("origin")
    if origin is not None and origin != "subagent":
        return False
    preset = value.get("agentPreset")
    if preset is not None and not isinstance(preset, str):
        return False
    return True


def _from_header_line(line: dict) -> dict:
    """header 行还原为扁平 header；退役政策基线字段响亮拒绝。"""
    if "sandboxMode" in line or "approvalPolicy" in line:
        raise ValueError("session header uses retired policy baseline fields")
    return line


def _parse_header_line_object(parsed: Any) -> dict:
    """版本拒读在前（未来格式不必满足今日结构检查——用户该看到的是
    「升级 harness」而不是「日志损坏」），再守卫当前头形。"""
    if isinstance(parsed, dict) and _is_number(parsed.get("version")):
        if parsed["version"] != SESSION_FORMAT_VERSION:
            session_id = parsed["id"] if isinstance(parsed.get("id"), str) else str(parsed.get("id"))
            raise SessionFormatUnsupportedError(
                session_format_version_refusal(session_id, parsed["version"])
            )
    if not isinstance(parsed, dict) or not _is_header_line(parsed):
        raise ValueError("corrupt session log: first line is not a session header")
    return _from_header_line(parsed)


def _parse_header_record(record: bytes) -> dict:
    """解析恰好一行的 header 记录（必须以单个换行结尾）。"""
    if len(record) == 0 or record[-1:] != b"\n" or record.find(b"\n") != len(record) - 1:
        raise ValueError("empty or header-less session log")
    try:
        parsed = json.loads(record[:-1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("corrupt session log: header line is not valid JSON") from None
    return _parse_header_line_object(parsed)


def _parse_header_meta(first_line: str) -> dict | None:
    """只解析首行的列表用守卫：不是完好 header 返回 None（上游 parseHeaderMeta）。"""
    try:
        parsed = json.loads(first_line)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not _is_header_line(parsed):
        return None
    try:
        return _from_header_line(parsed)
    except ValueError:
        return None


# ---------- 读路径扫描器（上游 format.ts SessionLogScanner 的整缓冲等价物）----------


class SessionLogScanner:
    """在独立供给的 header 记录之后增量扫描完整 JSONL 事件行。

    新行搜索与字节偏移保持在原始字节上；只有完整记录解码为 UTF-8。
    一旦记下问题（畸形行 / seq 断档），后续行不再入列，直到遇到 turn/end
    才把问题抛出（保证崩溃恢复逻辑总能看到回合边界）。
    """

    def __init__(self, header_record: bytes):
        self.meta = _parse_header_record(header_record)
        self.events: list[dict] = []
        self._fragments: list[bytes] = []
        self.input_bytes = len(header_record)
        self.committed_bytes = len(header_record)
        self.event_line = 0
        self.issue: Exception | None = None

    def write(self, chunk: bytes) -> None:
        """消费下一块原始明文，仅保留不完整的末记录碎片。"""
        chunk_start = self.input_bytes
        self.input_bytes += len(chunk)
        line_start = 0
        while True:
            newline = chunk.find(b"\n", line_start)
            if newline == -1:
                break
            fragment = chunk[line_start:newline]
            if self._fragments:
                if fragment:
                    self._fragments.append(fragment)
                line = b"".join(self._fragments)
                self._fragments = []
            else:
                line = fragment
            self._consume_event_line(line, chunk_start + newline + 1)
            line_start = newline + 1
        if line_start < len(chunk):
            self._fragments.append(chunk[line_start:])

    @property
    def has_fragment(self) -> bool:
        """是否存在未终结的末记录（torn 尾判定）。"""
        return bool(self._fragments)

    def finish(self) -> list[dict]:
        """结束扫描：无换行的末记录按 torn 尾忽略。"""
        return self.events

    def _consume_event_line(self, line: bytes, end_byte: int) -> None:
        self.event_line += 1
        try:
            decoded = [
                _decode_storage_event(event)
                for event in decode_storage_record(json.loads(line.decode("utf-8")))
            ]
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            if self.issue is None:
                self.issue = ValueError(
                    f"corrupt session log: unparsable committed event at line {self.event_line}"
                )
            return
        if self.issue is not None:
            if any(event.get("type") == "turn/end" for event in decoded):
                raise self.issue
            return
        row_start = len(self.events)
        for event in decoded:
            expected = len(self.events)
            if event.get("seq") != expected:
                del self.events[row_start:]
                self.issue = ValueError(
                    f"corrupt session log: seq gap in committed region at line "
                    f"{self.event_line} (expected {expected}, got {event.get('seq')})"
                )
                if any(candidate.get("type") == "turn/end" for candidate in decoded):
                    raise self.issue
                return
            self.events.append(event)
        self.committed_bytes = end_byte


# ---------- 接缝与双后端 ----------


class SessionPersistence:
    """接缝接口：append / flush / load，以及 A7 追加的 declare / inspect / list_headers。

    cwd 缺省时按上游契约从 header 元数据（meta.cwd）或既有落盘位置反查；无法反查
    的新会话（测试用）退化到 _no-cwd 项目目录，保证可写可读。
    """

    def append(self, session_id: str, event: dict, cwd: str | None = None) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        raise NotImplementedError

    def load(self, session_id: str, cwd: str | None = None) -> list[dict]:
        raise NotImplementedError

    def read_prepared(self, session_id: str, cwd: str | None = None) -> dict:
        """读出已提交前缀与崩溃恢复信息（上游 StoredPrefix 的 mini 形态）。

        返回 {events, recovered_events, truncate_to}：events 为保留前缀 +
        从 torn 尾恢复的完整记录；truncate_to 为应回退的字节偏移（无 torn 为
        None）。缺省实现无恢复面。
        """
        return {"events": self.load(session_id, cwd), "recovered_events": [], "truncate_to": None}

    def declare(self, session_id: str, meta: dict | None = None, created_at: int | None = None,
                cwd: str | None = None) -> None:
        """在首次 append 前写 header 元数据（子会话创建用）；幂等。"""
        raise NotImplementedError

    def inspect(self, session_id: str, cwd: str | None = None) -> dict:
        """返回 {meta, events}：meta 为 header 元数据（无则 None）。"""
        raise NotImplementedError

    def list_headers(self) -> list[dict]:
        """枚举全部会话 header：{id, meta, created_at, cwd}（meta/cwd 可能为 None）。"""
        raise NotImplementedError

    def read_raw(self, session_id: str, cwd: str | None = None) -> str | None:
        """读出会话的逐字原始制品文本（上游逐会话 raw artifact）。

        不支持该形态的后端（如 SQLite）抛 NotImplementedError，由调用方
        判定为 501（does not expose per-session raw artifacts）。
        """
        raise NotImplementedError

    def commit_repair(self, session_id: str, closers: list[dict], cwd: str | None = None,
                      recovered: list[dict] | None = None) -> None:
        """把崩溃修复持久化落盘（对齐上游 PersistenceBackend.commitRepair）。

        上游签名 commitRepair(meta, tornMarker, closers)：先截断 torn 尾，
        再追加「恢复事件 + 合成 closers」。mini 的截断在读路径已即时落盘，
        故此处追加 recovered（从残尾抢救出的完整记录）+ closers。
        """
        raise NotImplementedError


class JsonlPersistence(SessionPersistence):
    """JSONL 后端：每会话一个拼接帧容器文件（默认 zstd）或明文 JSONL。

    目录布局对齐上游 session-persistence-jsonl：
        root/<--projectKey(cwd)-->/<encodeSegment(id)>/session.jsonl[.zstd]
    cwd 缺省时按 header.meta.cwd 或既有落盘位置反查；新会话反查不到时退化
    到 _no-cwd 项目目录。两种物理编码在同一根目录互斥——发现对立编码的
    制品即响亮拒绝（encodingMismatch），项目目录下的散置制品（遗留平铺
    布局）同样拒绝（legacyLayout），绝不静默迁移。
    """

    def __init__(self, root: Path, compression: str = "zstd", pack_chunks: bool = True):
        if compression not in _JSONL_COMPRESSIONS:
            raise ValueError(
                f"compression must be one of {_JSONL_COMPRESSIONS}, got {compression!r}"
            )
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.compression = compression
        self.pack_chunks = pack_chunks
        self._pending: dict[str, list[dict]] = {}
        self._created: set[str] = set()
        # session_id -> 用于布局的 cwd（declare/append 时登记，缺省 ""）
        self._cwd: dict[str, str] = {}
        # session_id -> 已定位的真实日志路径（扫描/存在性确认后缓存）。
        self._paths: dict[str, Path] = {}
        self._root_encoding_checked = False

    # --- 物理编码辅助 ---

    def _opposite_compression(self) -> str:
        return "none" if self.compression == "zstd" else "zstd"

    def _encoding_mismatch(self, path: Path) -> ValueError:
        return ValueError(
            f'session artifact "{path}" uses {_log_suffix(self._opposite_compression())}, '
            f'but this backend is configured for compression "{self.compression}"; '
            "use a separate root or select the matching compression mode"
        )

    @staticmethod
    def _legacy_layout(path: Path) -> ValueError:
        return ValueError(
            f'session artifact "{path}" uses the unsupported flat-file layout; '
            "use a separate root or move it into a project/session directory before loading"
        )

    def _check_root_encoding(self) -> None:
        """根目录里任何对立编码制品都拒绝（上游 checkRootEncoding）。"""
        for project in self._list_project_dirs():
            for session_dir in self._list_session_dirs(project):
                incompatible = session_dir / f"session{_log_suffix(self._opposite_compression())}"
                if incompatible.exists():
                    raise self._encoding_mismatch(incompatible)

    def _ensure_root_encoding(self) -> None:
        if not self._root_encoding_checked:
            self._check_root_encoding()
            self._root_encoding_checked = True

    def _list_project_dirs(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(p for p in self.root.iterdir() if p.is_dir())

    @staticmethod
    def _list_session_dirs(project: Path) -> list[Path]:
        return sorted(p for p in project.iterdir() if p.is_dir())

    @staticmethod
    def _reject_legacy_flat_artifacts(project: Path) -> None:
        """项目目录下的散置 *.jsonl(.zstd) 文件是遗留平铺布局——拒绝。"""
        if not project.exists():
            return
        for entry in project.iterdir():
            if entry.is_file() and (
                entry.name.endswith(".jsonl") or entry.name.endswith(".jsonl.zstd")
            ):
                raise JsonlPersistence._legacy_layout(entry)

    def _reject_opposite_artifact(self, cwd: str | None, session_id: str) -> None:
        opposite = _log_path(self.root, cwd, session_id, self._opposite_compression())
        if opposite.exists():
            raise self._encoding_mismatch(opposite)

    # --- 耐久写原语 ---

    @staticmethod
    def _sync_dir(path: Path) -> None:
        """POSIX 目录 fsync 让新建/改名条目抗断电；Windows 无目录句柄，跳过
        （上游 win32 分支同样走不同的耐久发布路径）。"""
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

    def _write_synced_temp(self, final_path: Path, content: bytes) -> Path:
        tmp = final_path.with_name(f"{final_path.name}.{os.urandom(6).hex()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
        try:
            os.write(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
        return tmp

    def _publish_new_file(self, tmp: Path, final_path: Path) -> None:
        """经 link()+unlink() 发布而非 rename()：link 对已存在的终路径报
        EEXIST，两个进程并发物化同一 id 不可能互相覆盖（rename 会静默覆盖）。"""
        try:
            os.link(tmp, final_path)
        except FileExistsError:
            raise ValueError(
                f'refusing to materialize: a log already exists on disk at "{final_path}" '
                "(load/resume it instead)"
            ) from None
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        self._sync_dir(final_path.parent)

    def _encode_lines(self, records: list[dict]) -> str:
        """存储记录序列化为 JSONL 文本（无尾换行由调用方补）。

        sourceEventSeqs 在写边界经 encode_seq_ranges 折成存储态区间
        （上游 eventLines → encodeProvenanceForStorage；读侧 expand 还原）。
        """
        return "".join(
            json.dumps(_encode_storage_event(thaw(record)), ensure_ascii=False) + "\n"
            for record in records
        )

    def _encode_event_batch(self, events: list[dict]) -> bytes | str:
        """一个耐久批次按配置编码：zstd 帧（打包行在序列化前完成）或明文。"""
        body = self._encode_lines(
            pack_chunk_runs(events) if self.pack_chunks else events
        )
        if self.compression == "zstd":
            return compress_zstd_frame(body)
        return body

    def _materialize_header(self, path: Path, session_id: str, meta: dict | None,
                            created_at: int | None) -> None:
        """原子写出 header 行帧（临时写 + fsync + link 发布）。"""
        self._ensure_root_encoding()
        project = _project_dir(self.root, self._cwd.get(session_id))
        project.mkdir(parents=True, exist_ok=True)
        self._sync_dir(project.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._sync_dir(project)
        self._reject_legacy_flat_artifacts(project)
        self._reject_opposite_artifact(self._cwd.get(session_id), session_id)
        if path.exists():
            raise ValueError(
                f'refusing to materialize "{session_id}": a log already exists on disk '
                "(load/resume it instead)"
            )
        # 头行与目录布局必须同源（上游 projectDir 恒由 header.cwd 派生）：
        # append 侧登记的运行时 cwd 在 meta 未携带时补写进 header。
        flat = _flat_header(session_id, meta, created_at)
        cwd = self._cwd.get(session_id)
        if cwd and "cwd" not in flat:
            flat["cwd"] = cwd
        header_line = _to_header_line(flat)
        text = json.dumps(header_line, ensure_ascii=False) + "\n"
        content = compress_zstd_frame(text) if self.compression == "zstd" else text.encode("utf-8")
        tmp = self._write_synced_temp(path, content)
        self._publish_new_file(tmp, path)
        self._created.add(session_id)

    # --- 定位 ---

    def _find(self, session_id: str) -> Path | None:
        """跨全部项目目录按编码后的 id 子目录定位唯一物理日志（上游 findLog）。"""
        self._ensure_root_encoding()
        matches: list[Path] = []
        suffix = _log_suffix(self.compression)
        opposite_suffix = _log_suffix(self._opposite_compression())
        for project in self._list_project_dirs():
            self._reject_legacy_flat_artifacts(project)
            session_dir = project / encode_segment(session_id)
            opposite = session_dir / f"session{opposite_suffix}"
            if opposite.exists():
                raise self._encoding_mismatch(opposite)
            candidate = session_dir / f"session{suffix}"
            if candidate.exists():
                matches.append(candidate)
        if len(matches) > 1:
            raise ValueError(
                f'duplicate JSONL session id "{session_id}" appears in multiple project directories'
            )
        return matches[0] if matches else None

    def _resolve(self, session_id: str, cwd: str | None) -> Path:
        """解析会话文件绝对路径：已定位缓存 → cwd 给定 → 记忆 → 扫描 → _no-cwd。

        projectKey 是有损编码（分隔符折叠不可逆），扫描命中后缓存**真实路径**
        而不是把反解码结果当 cwd 复用——后者会让后续解析算出不存在的路径。
        """
        cached = self._paths.get(session_id)
        if cached is not None and cwd is None:
            return cached
        if cwd is not None:
            self._cwd[session_id] = cwd
        elif session_id in self._cwd:
            cwd = self._cwd[session_id]
        else:
            found = self._find(session_id)
            if found is not None:
                project_name = found.parent.parent.name
                self._cwd[session_id] = (
                    "" if project_name == "_no-cwd" else decode_segment(project_name)
                )
                self._paths[session_id] = found
                return found
            cwd = ""
            self._cwd[session_id] = cwd
        path = _log_path(self.root, cwd, session_id, self.compression)
        if path.exists():
            self._paths[session_id] = path
        return path

    def path_of(self, session_id: str, cwd: str | None = None) -> Path | None:
        """对外：返回会话文件绝对路径，不存在返回 None（供 CLI 存在性判断）。

        已定位缓存优先——`_cwd` 里可能是扫描反解码的有损值，不能直接参与
        路径计算（见 `_resolve`）。
        """
        if cwd is None:
            cached = self._paths.get(session_id)
            if cached is not None:
                return cached if cached.exists() else None
        path = self._resolve(session_id, cwd)
        return path if path.exists() else None

    # --- 写路径 ---

    def append(self, session_id, event, cwd: str | None = None):
        if cwd is not None:
            self._cwd[session_id] = cwd
        self._pending.setdefault(session_id, []).append(event)

    def declare(self, session_id: str, meta: dict | None = None, created_at: int | None = None,
                cwd: str | None = None) -> None:
        cwd = cwd if cwd is not None else (meta or {}).get("cwd") or ""
        self._cwd[session_id] = cwd
        path = _log_path(self.root, cwd, session_id, self.compression)
        if session_id in self._created:
            return
        if not path.exists():
            self._materialize_header(path, session_id, meta, created_at)
        self._paths[session_id] = path

    def _ensure_header(self, session_id: str) -> None:
        if session_id in self._created:
            return
        path = self._resolve(session_id, None)
        if path.exists():
            self._created.add(session_id)
            return
        self._materialize_header(path, session_id, None, None)

    def flush(self):
        for sid, events in self._pending.items():
            self._ensure_header(sid)
            path = self._resolve(sid, None)
            self._append_lines(sid, path, events)
        self._pending.clear()

    def _append_lines(self, session_id: str, path: Path, events: list[dict]) -> None:
        """追加并 fsync 一个批次。部分写入或同步失败时先回滚到旧尺寸再抛出：
        未变化的游标会重试该批，留下半截字节就会制造重复序号。"""
        content = self._encode_event_batch(events)
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | _O_BINARY)
        try:
            before = os.fstat(fd).st_size
            try:
                os.write(fd, data)
                os.fsync(fd)
            except OSError as error:
                try:
                    os.close(fd)
                    self._rollback_append(path, before)
                except OSError as rollback_error:
                    raise RuntimeError(
                        f'failed to roll back append to "{path}"'
                    ) from rollback_error
                raise error
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    @staticmethod
    def _rollback_append(path: Path, size: int) -> None:
        fd = os.open(path, os.O_WRONLY | _O_BINARY)
        try:
            os.ftruncate(fd, size)
            os.fsync(fd)
        finally:
            os.close(fd)

    # --- 读路径 ---

    def _read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def _read_by_id(self, path: Path, session_id: str) -> tuple[dict, list[dict], dict | None]:
        """读出保留前缀并断言身份：请求 id 与 header id 一致、header id 与
        落盘路径互相指认。torn 尾状态转换为可往返的标记返回。"""
        buffer = self._read_bytes(path)
        if self.compression == "zstd":
            meta, events, marker = self._read_zstd_prefix(buffer)
        else:
            meta, events, marker = self._read_none_prefix(buffer)
        if meta["id"] != session_id:
            raise ValueError(
                f'corrupt session log "{path}": requested id "{session_id}" does not match '
                f'header id "{meta["id"]}"'
            )
        expected_path = _log_path(self.root, meta.get("cwd"), meta["id"], self.compression)
        if path != expected_path and not self._same_file(path, expected_path):
            raise ValueError(
                f'corrupt session log "{path}": header id "{meta["id"]}" and cwd identify '
                f'"{expected_path}"'
            )
        return meta, events, marker

    @staticmethod
    def _same_file(actual: Path, expected: Path) -> bool:
        """大小写不敏感文件系统上接受大小写别名，同时不在大小写敏感存储上放松身份检查。"""
        try:
            return actual.resolve() == expected.resolve()
        except OSError:
            return False

    def _read_zstd_prefix(self, buffer: bytes) -> tuple[dict, list[dict], dict | None]:
        scan = scan_zstd_frames(buffer)
        frames = scan.frames
        if not frames:
            raise ValueError("empty or header-less Zstandard session log")

        first_frame = frames[0]
        try:
            header_plaintext = decompress_zstd_frame(buffer[first_frame.start : first_frame.end])
        except zstandard.ZstdError as error:
            raise ValueError(
                "corrupt Zstandard session log: header frame failed validation"
            ) from error
        if len(header_plaintext) == 0 or header_plaintext.find(b"\n") != len(header_plaintext) - 1:
            raise ValueError(
                "corrupt Zstandard session log: first frame is not exactly one header line"
            )
        scanner = SessionLogScanner(header_plaintext)

        rest = decode_frames(buffer, frames[1:])
        for plaintext in rest:
            scanner.write(plaintext)
        if scanner.committed_bytes != scanner.input_bytes:
            raise ValueError(
                "corrupt Zstandard session log: complete frame contains a torn JSONL record"
            )
        committed_count = len(scanner.events)
        if scan.torn_start is None:
            return scanner.meta, scanner.events, None

        # 结构不完整的末帧：尽力恢复可得明文。极短残帧可能产不出任何明文，
        # 完整的前序帧保持可恢复。
        try:
            recovered_plaintext = decompress_zstd_prefix(buffer[scan.torn_start :])
        except zstandard.ZstdError:
            recovered_plaintext = b""
        scanner.write(recovered_plaintext)
        events = scanner.finish()
        marker = {"truncate_to": scan.torn_start, "recovered_events": events[committed_count:]}
        return scanner.meta, events, marker

    def _read_none_prefix(self, buffer: bytes) -> tuple[dict, list[dict], dict | None]:
        header_end = buffer.find(b"\n")
        if header_end == -1:
            raise ValueError("empty or header-less session log")
        scanner = SessionLogScanner(buffer[: header_end + 1])
        scanner.write(buffer[header_end + 1 :])
        events = scanner.finish()
        if scanner.has_fragment:
            marker = {"truncate_to": scanner.committed_bytes, "recovered_events": []}
            return scanner.meta, events, marker
        return scanner.meta, events, None

    def _truncate_to(self, path: Path, offset: int) -> None:
        fd = os.open(path, os.O_WRONLY | _O_BINARY)
        try:
            os.ftruncate(fd, offset)
            os.fsync(fd)
        finally:
            os.close(fd)

    def load(self, session_id, cwd: str | None = None):
        header, events = self._load_checked(session_id, cwd)
        return events

    def _load_checked(self, session_id: str, cwd: str | None):
        path = self._resolve(session_id, cwd)
        if not path.exists():
            return None, []
        header, events, marker = self._read_by_id(path, session_id)
        if marker is not None:
            self._truncate_to(path, marker["truncate_to"])
        return header, events

    def _read_by_id(self, path: Path, session_id: str):
        buffer = self._read_bytes(path)
        if self.compression == "zstd":
            meta, events, marker = self._read_zstd_prefix(buffer)
        else:
            meta, events, marker = self._read_none_prefix(buffer)
        if meta["id"] != session_id:
            raise ValueError(
                f'corrupt session log "{path}": requested id "{session_id}" does not match '
                f'header id "{meta["id"]}"'
            )
        expected_path = _log_path(self.root, meta.get("cwd"), meta["id"], self.compression)
        if path != expected_path and not self._same_file(path, expected_path):
            raise ValueError(
                f'corrupt session log "{path}": header id "{meta["id"]}" and cwd identify '
                f'"{expected_path}"'
            )
        return meta, events, marker

    def read_prepared(self, session_id: str, cwd: str | None = None) -> dict:
        path = self._resolve(session_id, cwd)
        if not path.exists():
            return {"events": [], "recovered_events": [], "truncate_to": None}
        _, events, marker = self._read_by_id(path, session_id)
        if marker is not None:
            self._truncate_to(path, marker["truncate_to"])
        return {
            "events": events,
            "recovered_events": list(marker["recovered_events"]) if marker else [],
            "truncate_to": marker["truncate_to"] if marker else None,
        }

    def inspect(self, session_id, cwd: str | None = None):
        header, events = self._load_checked(session_id, cwd)
        return {"meta": _header_meta(header) if header else None, "events": events}

    def list_headers(self):
        """枚举有效唯一会话的 header（只读首行/首帧——列举的成本随会话数而非
        全部日志总长伸缩）。"""
        self._ensure_root_encoding()
        headers = []
        ids: set[str] = set()
        suffix = _log_suffix(self.compression)
        opposite_suffix = _log_suffix(self._opposite_compression())
        for project in self._list_project_dirs():
            self._reject_legacy_flat_artifacts(project)
            for session_dir in self._list_session_dirs(project):
                opposite = session_dir / f"session{opposite_suffix}"
                if opposite.exists():
                    raise self._encoding_mismatch(opposite)
                path = session_dir / f"session{suffix}"
                if not path.exists():
                    continue
                first = self._read_first_line(path)
                if first is None:
                    continue  # 空文件 / 半写的头帧
                meta = _parse_header_meta(first)
                if meta is None:
                    continue  # 不是 session header
                if meta["version"] != SESSION_FORMAT_VERSION:
                    continue
                if meta["id"] in ids:
                    raise ValueError(
                        f'duplicate JSONL session id "{meta["id"]}" appears in multiple '
                        "project directories"
                    )
                ids.add(meta["id"])
                headers.append({
                    "id": meta["id"],
                    "meta": _header_meta(meta),
                    "created_at": meta.get("createdAt"),
                    "cwd": meta.get("cwd"),
                })
        return headers

    def _read_first_line(self, path: Path) -> str | None:
        """只读第一条完整记录（明文首行或首个 zstd 帧），验证其为单行 header。"""
        if self.compression == "zstd":
            with open(path, "rb") as handle:
                plaintext = read_first_frame(lambda: handle.read(8192))
            if plaintext is None:
                return None
            if plaintext.find(b"\n") != len(plaintext) - 1:
                raise ValueError(
                    "corrupt Zstandard session log: first frame is not exactly one header line"
                )
            return plaintext[:-1].decode("utf-8")
        with open(path, "rb") as handle:
            line = handle.readline()
        if not line.endswith(b"\n"):
            return None  # EOF 且无完整行
        return line[:-1].decode("utf-8")

    def read_raw(self, session_id: str, cwd: str | None = None) -> str | None:
        """读出会话的逐字原始制品文本：完整帧解压后拼接（或明文原文）。

        内容是后端写下的确切 JSONL 文本——绝不从解析后的事件重建，打包行、
        键序与换行逐字节幸存。逻辑制品名恒为 session.jsonl（.zstd 后缀只标
        物理编码）。
        """
        path = self.path_of(session_id, cwd)
        if path is None or not path.exists():
            return None
        buffer = self._read_bytes(path)
        if self.compression == "zstd":
            scan = scan_zstd_frames(buffer)
            if not scan.frames:
                raise ValueError("empty or header-less Zstandard session log")
            content = b"".join(decode_frames(buffer, scan.frames)).decode("utf-8")
        else:
            content = buffer.decode("utf-8")
        meta = _parse_header_meta(content.split("\n", 1)[0])
        if meta is None or meta["id"] != session_id:
            raise ValueError(f'corrupt session log: invalid header line in "{path}"')
        return content

    def commit_repair(self, session_id: str, closers: list[dict], cwd: str | None = None,
                      recovered: list[dict] | None = None) -> None:
        repaired = list(recovered or []) + list(closers)
        if not repaired:
            return
        self._ensure_header(session_id)
        path = self._resolve(session_id, cwd)
        self._append_lines(session_id, path, repaired)


class SqlitePersistence(SessionPersistence):
    """SQLite 后端：多会话一库；单调 SCHEMA_VERSION，版本不符拒绝加载。"""

    SCHEMA_VERSION = 2

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.root / "sessions.sqlite")
        self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self._conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(self.SCHEMA_VERSION),))
            self._conn.commit()
        elif int(row[0]) != self.SCHEMA_VERSION:
            self._conn.close()
            raise RuntimeError(
                f"SQLite 库版本 {row[0]} 与当前 {self.SCHEMA_VERSION} 不一致，拒绝加载"
            )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "session_id TEXT, seq INTEGER, type TEXT, data TEXT, "
            "PRIMARY KEY (session_id, seq))"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "session_id TEXT PRIMARY KEY, meta TEXT, created_at INTEGER)"
        )
        self._conn.commit()
        self._pending: dict[str, list[dict]] = {}

    def append(self, session_id, event, cwd: str | None = None):
        self._pending.setdefault(session_id, []).append(event)

    def declare(self, session_id: str, meta: dict | None = None, created_at: int | None = None,
                cwd: str | None = None) -> None:
        meta = dict(meta) if meta else {}
        if cwd is not None and "cwd" not in meta:
            meta["cwd"] = cwd
        self._conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO NOTHING",
            (session_id, json.dumps(meta, ensure_ascii=False) if meta else None, created_at),
        )
        self._conn.commit()

    def flush(self):
        for sid, events in self._pending.items():
            base = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) FROM events WHERE session_id=?", (sid,)
            ).fetchone()[0]
            rows = [
                (sid, base + 1 + i, ev["type"],
                 json.dumps(_encode_storage_event(thaw(ev)), ensure_ascii=False))
                for i, ev in enumerate(events)
            ]
            self._conn.executemany("INSERT INTO events VALUES (?, ?, ?, ?)", rows)
        self._conn.commit()
        self._pending.clear()

    def load(self, session_id, cwd: str | None = None):
        rows = self._conn.execute(
            "SELECT data FROM events WHERE session_id=? ORDER BY seq", (session_id,)
        ).fetchall()
        return [_decode_storage_event(json.loads(r[0])) for r in rows]

    def inspect(self, session_id, cwd: str | None = None):
        row = self._conn.execute(
            "SELECT meta FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        meta = json.loads(row[0]) if row and row[0] else None
        return {"meta": meta, "events": self.load(session_id, cwd)}

    def list_headers(self):
        rows = self._conn.execute(
            "SELECT session_id, meta, created_at FROM sessions ORDER BY session_id"
        ).fetchall()
        out = []
        for r in rows:
            meta = json.loads(r[1]) if r[1] else None
            out.append({
                "id": r[0],
                "meta": meta,
                "created_at": r[2],
                "cwd": (meta or {}).get("cwd"),
            })
        return out

    def commit_repair(self, session_id: str, closers: list[dict], cwd: str | None = None,
                      recovered: list[dict] | None = None) -> None:
        repaired = list(recovered or []) + list(closers)
        if not repaired:
            return
        rows = [
            (session_id, ev["seq"], ev["type"],
             json.dumps(_encode_storage_event(thaw(ev)), ensure_ascii=False))
            for ev in repaired
        ]
        self._conn.executemany(
            "INSERT INTO events (session_id, seq, type, data) VALUES (?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    def read_raw(self, session_id: str, cwd: str | None = None) -> str | None:
        """SQLite 后端不暴露逐会话原始制品（事件在行列存储，无 JSONL 形态）。"""
        raise NotImplementedError

    def close(self):
        self._conn.close()


# ---------- 加载与恢复 ----------

def load_events_checked(raw_events: list[dict]) -> list[dict]:
    """fail-closed：未知事件类型除非带 `ignorable` 标记否则拒绝加载。

    语义对齐上游 session-persistence coordinator.ts `assertEventsSupported`
    （:1143-1148）：此构建不认识、且写方未标 `ignorable` 的事件可能是更新版本
    harness 写入的必需事件——静默跳过会重建出错误的会话，故响亮拒绝；写方将事件
    标记为 `ignorable`（纯信息记录，丢失不影响重建）则放行保留。
    """
    for ev in raw_events:
        if ev.get("type") not in KNOWN_TYPES and ev.get("ignorable") is not True:
            raise RuntimeError(
                f'session contains event type "{ev.get("type")}" '
                f'(seq {ev.get("seq")}) unknown to this harness and not marked ignorable; '
                f"refusing to interpret the log — it was likely written by a newer harness"
            )
    return raw_events


def repair_and_replay(persistence: SessionPersistence, session_id: str, session: Session) -> Session:
    """load → 校验 → 崩溃修复 → 以 seed 回放进内存 Session（重启后继续对话）。

    与上游 prepareCore 顺序一致：fail-closed 校验在前，repair 只合成缺失 closers。
    修复结果持久化：对齐上游 commitRepair(meta, tornMarker, closers) —— 截断在
    读路径已即时落盘，此处把「从 torn 尾恢复的完整事件 + 合成 closers」一并
    追加落盘。已平衡的日志返回空 closers，二次加载幂等。
    """
    prepared = persistence.read_prepared(session_id)
    raw = load_events_checked(prepared["events"])
    closers = repair_interrupted_turn(raw)
    recovered = prepared["recovered_events"]
    if closers or recovered:
        persistence.commit_repair(session_id, closers, recovered=recovered)
    repaired = raw + closers
    if repaired:
        session = Session(session_id, seed=repaired, created_at=session.created_at)
    return session


def balanced_after_replay(persistence: SessionPersistence, session_id: str) -> bool:
    """验收辅助：加载修复后日志必须括号平衡。"""
    raw = load_events_checked(persistence.load(session_id))
    return turn_balance(raw + repair_interrupted_turn(raw)) == 0
