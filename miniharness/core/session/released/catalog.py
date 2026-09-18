"""released 目录（上游 session-format/src/chain.ts + catalog.ts + session-format-catalog
的 mini 载体）：编译唯一相邻链 0→1→2→3，物理 codec 分派 + 整件迁移 + 当前编码。

`encode_current` 产出 `{header, rows}`——rows 为存储态事件行（sourceEventSeqs 已折叠
区间编码，复用 mini 既有 `encode_seq_ranges`）；物理容器（zstd header 帧 + body 帧）
由 `generation.py` 组装。

V3（上游 dsh-v0.1.5-alpha.1）：链增 v2→v3 相邻边（system head 提升 + PTC 词汇
改名 + canonical 信封）；当前编码 = v3（`session.v3.jsonl`）。
"""
from __future__ import annotations

from typing import Any

from .codec import (
    RELEASED_V0_CODEC,
    RELEASED_V1_CODEC,
    RELEASED_V2_CODEC,
    decode_released_header,
)
from .helpers import SessionFormatError, SessionFormatUnsupportedMigrationError, fail, unsupported
from .migrate_v0_v1 import V0_TO_V1
from .migrate_v1_to_v2 import V1_TO_V2
from .migrate_v2_to_v3 import V2_TO_V3

__all__ = [
    "SESSION_FORMAT_CATALOG",
    "decode_released_header",
    "migrate_released_artifact",
    "migrate_released_header",
    "read_released_header",
]


def _assert_version(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise fail(f"{label} must be a non-negative safe integer")
    return value


class _Chain:
    """唯一相邻链（当前 v3）：plan(from) = ordered[from:]；migrate 逐边执行。"""

    def __init__(self) -> None:
        self.current_version = 3
        ordered = [V0_TO_V1, V1_TO_V2, V2_TO_V3]
        self._ordered = ordered

    def plan(self, from_version: int) -> list[dict]:
        _assert_version(from_version, "stored Session format version")
        if from_version > self.current_version:
            raise unsupported(
                f"stored Session uses newer format v{from_version}; "
                f"this build writes v{self.current_version}")
        return self._ordered[from_version:]

    def migrate(self, artifact: dict) -> dict:
        stored = artifact["header"]["version"]
        if stored == self.current_version:
            return artifact
        current = artifact
        for migration in self.plan(stored):
            try:
                current = migration["migrate"](current)
            except SessionFormatUnsupportedMigrationError:
                raise
            except SessionFormatError as error:
                raise unsupported(
                    f"{migration['name']} refuses this format v{stored} Session: {error}"
                ) from error
            if current["header"]["version"] != migration["to_version"]:
                raise fail(
                    f"{migration['name']} returned v{current['header']['version']}; "
                    f"expected v{migration['to_version']}")
        return current

    def migrate_header(self, header: dict) -> dict:
        current = dict(header)
        for migration in self.plan(current["version"]):
            try:
                current = migration["migrate_header"](current)
            except SessionFormatUnsupportedMigrationError:
                raise
            except SessionFormatError as error:
                raise unsupported(
                    f"{migration['name']} refuses this format v{header['version']} "
                    f"Session header: {error}") from error
        return current


def _encode_current(artifact: dict) -> dict:
    """当前 v3 逻辑件 → {header, rows}（存储态；provenance 折叠）。

    物理头补写 `type:'session'` 标签（上游 v2-to-v3 codec encodeArtifact 的键序：
    type, version, id, createdAt, [cwd, parentSession,] isSeeded, [origin,]
    delegationDepth, [agentPreset]——可选键缺席即省略；V3 沿用 v2 物理头形状）。
    """
    from ..json import thaw
    from ..seq_ranges import encode_seq_ranges
    header = artifact["header"]
    if header.get("version") != 3:
        raise fail("encodeCurrent requires Session format v3")
    physical: dict[str, Any] = {"type": "session"}
    for key in ("version", "id", "createdAt"):
        physical[key] = header[key]
    for key in ("cwd", "parentSession"):
        if key in header:
            physical[key] = header[key]
    physical["isSeeded"] = header["isSeeded"]
    if "origin" in header:
        physical["origin"] = header["origin"]
    physical["delegationDepth"] = header["delegationDepth"]
    if "agentPreset" in header:
        physical["agentPreset"] = header["agentPreset"]
    rows = []
    for event in artifact["events"]:
        row = thaw(event)
        if "sourceEventSeqs" in row:
            row = {**row, "sourceEventSeqs": encode_seq_ranges(row["sourceEventSeqs"])}
        rows.append(row)
    return {"header": thaw(physical), "rows": rows}


class _Catalog:
    def __init__(self) -> None:
        self.chain = _Chain()
        self.codecs = {0: RELEASED_V0_CODEC, 1: RELEASED_V1_CODEC, 2: RELEASED_V2_CODEC}

    @property
    def current_version(self) -> int:
        return self.chain.current_version

    def codec_for(self, stored_version: int) -> dict:
        if stored_version > self.chain.current_version:
            raise unsupported(
                f"stored Session uses newer format v{stored_version}; "
                f"this build writes v{self.chain.current_version}")
        codec = self.codecs.get(stored_version)
        if codec is None:
            raise unsupported(
                f"this build has no Session format codec for v{stored_version}")
        return codec

    def decode_artifact(self, header_value: Any, row_values: list[Any]) -> dict:
        stored = decode_released_header(header_value)
        return self.codec_for(stored)["decode_artifact"](header_value, row_values)

    def decode_recoverable_artifact(self, header_value: Any, row_values: list[Any]) -> dict:
        stored = decode_released_header(header_value)
        return self.codec_for(stored)["decode_recoverable_artifact"](header_value, row_values)

    def migrate(self, artifact: dict) -> dict:
        return self.chain.migrate(artifact)

    def migrate_header(self, header: dict) -> dict:
        return self.chain.migrate_header(header)

    def encode_current(self, artifact: dict) -> dict:
        return _encode_current(artifact)

    def encode_current_header(self, header: dict, inherited_event_count: int) -> dict:
        """编码一个当前逻辑头为物理头（上游 catalog.encodeCurrentHeader）。

        `inherited_event_count` 是调用方持有的切点；v3 物理头由 isSeeded 承载
        （cut 由最后一个 `{inherited:true}` marker 派生，不写物理头）。
        """
        encoded = _encode_current({"header": header, "events": []})
        return encoded["header"]

    def encode_current_event(self, event: dict) -> dict:
        """编码一个当前逻辑事件为存储态行（上游 catalog.encodeCurrentEvent）：
        sourceEventSeqs 折叠为区间编码（复用 mini encode_seq_ranges）。"""
        from ..json import thaw
        from ..seq_ranges import encode_seq_ranges

        row = thaw(event)
        if "sourceEventSeqs" in row:
            row = {**row, "sourceEventSeqs": encode_seq_ranges(row["sourceEventSeqs"])}
        return row


SESSION_FORMAT_CATALOG = _Catalog()


def migrate_released_artifact(artifact: dict) -> dict:
    """便捷入口：任意 released 版本逻辑件 → 当前 v3 逻辑件。"""
    return SESSION_FORMAT_CATALOG.migrate(artifact)


def migrate_released_header(header: dict) -> dict:
    """便捷入口：任意 released 逻辑头 → 当前 v3 逻辑头（不读事件体）。"""
    return SESSION_FORMAT_CATALOG.migrate_header(header)


def read_released_header(header_value: Any) -> dict:
    """分类一个物理头（上游 catalog.readHeader）：当前版本直接读，旧版本走相邻头迁移。

    @param header_value - 已解析的物理 header 行（含 `type:'session'` 标签）。
    @returns `{status, storedVersion, targetVersion, header}`——status 为
        'current'（stored == 当前版本）或 'migration-required'（需要迁移），
        header 为迁移到当前版本的逻辑头（物理 `type` 标签剥离，上游 readHeader 同形）。
    """
    stored = decode_released_header(header_value)
    if stored == SESSION_FORMAT_CATALOG.current_version:
        # 当前版本：校验逻辑头（物理 `type` 标签剥离）并在结果中返回逻辑头。
        from .validate_v3 import assert_released_v3_header

        logical = {key: value for key, value in header_value.items() if key != "type"}
        assert_released_v3_header(logical)
        return {"status": "current", "storedVersion": stored,
                "targetVersion": stored, "header": logical}
    # 旧版本：物理头先经存储版本 codec 解码为逻辑头（seedLength→isSeeded），再走相邻头迁移。
    logical = SESSION_FORMAT_CATALOG.codec_for(stored)["decode_header"](header_value)
    return {
        "status": "migration-required",
        "storedVersion": stored,
        "targetVersion": SESSION_FORMAT_CATALOG.current_version,
        "header": SESSION_FORMAT_CATALOG.migrate_header(logical),
    }
