"""released 会话格式域：v0/v1 物理编解码 + 相邻迁移链（v0→v1→v2）。

对应 dsh 真实源码：
  * `packages/session/session-format/src/*`（chain/catalog/types/json/error/filename 基座）；
  * `packages/session/session-format-v0-to-v1/src/*`（共享 codec + legacy 归一化迁移）；
  * `packages/session/session-format-v1-to-v2/src/*`（chunk 流内嵌 + attempt 迁移）。

Phase A 范围（design-generation-migration.md）：转换语义与迁移自身不变量逐字对齐；
51 类型逐字段 payload 语义与跨事件关系状态机（上游深度校验层）**不移植**——
病态输入由「迁移不变量 + 迁移产物过现行 v2 restore 全量校验」双防线兜底，
登记为 verified-diffs §2.24 显式简化。
"""
from .helpers import (
    SAFE_INT_MAX,
    SessionFormatError,
    SessionFormatUnsupportedMigrationError,
    count,
    deep_equal,
    exact_keys,
    fail,
    is_json_object,
    lossless_json,
    safe_integer,
    snapshot_json,
    unsupported,
)
from .dispositions import (
    RELEASED_V0_EVENT_DISPOSITIONS,
    RELEASED_V0_EVENT_TYPES,
    RELEASED_V2_EVENT_DISPOSITIONS,
    RELEASED_V2_EVENT_TYPES,
)
from .codec import (
    RELEASED_V0_CODEC,
    RELEASED_V1_CODEC,
    PackedRowError,
    create_released_codec,
    decode_released_header,
)
from .validate import (
    assert_released_surface_metadata,
    assert_scoped_v1_artifact,
    scoped_v1_source_check,
)
from .migrate_v0_v1 import V0_TO_V1
from .migrate_v1_to_v2 import V1_TO_V2
from .catalog import SESSION_FORMAT_CATALOG, migrate_released_artifact, migrate_released_header

__all__ = [
    "SAFE_INT_MAX",
    "SessionFormatError",
    "SessionFormatUnsupportedMigrationError",
    "PackedRowError",
    "count",
    "deep_equal",
    "exact_keys",
    "fail",
    "is_json_object",
    "lossless_json",
    "safe_integer",
    "snapshot_json",
    "unsupported",
    "RELEASED_V0_EVENT_DISPOSITIONS",
    "RELEASED_V0_EVENT_TYPES",
    "RELEASED_V2_EVENT_DISPOSITIONS",
    "RELEASED_V2_EVENT_TYPES",
    "RELEASED_V0_CODEC",
    "RELEASED_V1_CODEC",
    "create_released_codec",
    "decode_released_header",
    "assert_released_surface_metadata",
    "assert_scoped_v1_artifact",
    "scoped_v1_source_check",
    "V0_TO_V1",
    "V1_TO_V2",
    "SESSION_FORMAT_CATALOG",
    "migrate_released_artifact",
    "migrate_released_header",
]
