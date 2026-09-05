"""released 目录（上游 session-format/src/chain.ts + catalog.ts + session-format-catalog
的 mini 载体）：编译唯一相邻链 0→1→2，物理 codec 分派 + 整件迁移 + 当前编码。

`encode_current` 产出 `{header, rows}`——rows 为存储态事件行（sourceEventSeqs 已折叠
区间编码，复用 mini 既有 `encode_seq_ranges`）；物理容器（zstd header 帧 + body 帧）
由 `generation.py` 组装。
"""
from __future__ import annotations

from typing import Any

from .codec import RELEASED_V0_CODEC, RELEASED_V1_CODEC, decode_released_header
from .helpers import SessionFormatError, SessionFormatUnsupportedMigrationError, fail, unsupported
from .migrate_v0_v1 import V0_TO_V1
from .migrate_v1_to_v2 import V1_TO_V2

__all__ = ["SESSION_FORMAT_CATALOG", "migrate_released_artifact", "migrate_released_header"]


def _assert_version(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise fail(f"{label} must be a non-negative safe integer")
    return value


class _Chain:
    """唯一相邻链（当前 v2）：plan(from) = ordered[from:]；migrate 逐边执行。"""

    def __init__(self) -> None:
        self.current_version = 2
        ordered = [V0_TO_V1, V1_TO_V2]
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
    """当前 v2 逻辑件 → {header, rows}（存储态；provenance 折叠）。

    物理头补写 `type:'session'` 标签（上游 v1-to-v2 codec encodeArtifact 的键序：
    type, version, id, createdAt, [cwd, parentSession,] isSeeded, [origin,]
    delegationDepth, [agentPreset]——可选键缺席即省略）。
    """
    from ..json import thaw
    from ..seq_ranges import encode_seq_ranges
    header = artifact["header"]
    if header.get("version") != 2:
        raise fail("encodeCurrent requires Session format v2")
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
        self.codecs = {0: RELEASED_V0_CODEC, 1: RELEASED_V1_CODEC}

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


SESSION_FORMAT_CATALOG = _Catalog()


def migrate_released_artifact(artifact: dict) -> dict:
    """便捷入口：任意 released 版本逻辑件 → 当前 v2 逻辑件。"""
    return SESSION_FORMAT_CATALOG.migrate(artifact)


def migrate_released_header(header: dict) -> dict:
    """便捷入口：任意 released 逻辑头 → 当前 v2 逻辑头（不读事件体）。"""
    return SESSION_FORMAT_CATALOG.migrate_header(header)
