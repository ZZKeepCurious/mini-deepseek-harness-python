"""released 会话格式域：v0/v1 物理编解码 + 相邻迁移链（v0→v1→v2）。

对应 dsh 真实源码：
  * `packages/session/session-format/src/*`（chain/catalog/types/json/error/filename 基座）；
  * `packages/session/session-format-v0-to-v1/src/*`（共享 codec + legacy 归一化迁移）；
  * `packages/session/session-format-v1-to-v2/src/*`（chunk 流内嵌 + attempt 迁移）。

Phase B（2026-09-05）：v0/v1 深度校验层落地——51 类型逐字段 payload 语义
（payload_validation.py）+ 跨事件关系状态机（relationships.py）+ 真实 artifact 编排
（validate.py），消息措辞逐字对齐上游；v0/v1 codec 与 v0→v1 迁移换用真实校验器。
v2 校验（validate_v2.py，含内嵌流 BlockAssembler 复核）与 v1→v2 迁移接续同批。
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
    assert_released_event_payload,
    assert_released_session_format_header,
    assert_released_surface_metadata,
    assert_released_v1_artifact,
    assert_released_v1_header,
    assert_released_v1_physical_artifact,
    assert_released_v0_source_artifact,
    assert_normalized_released_v0_artifact,
    restore_released_v1_artifact,
)
from .validate_v2 import (
    RELEASED_V2_RELATIONSHIP_EXTENSIONS,
    assert_released_v2_artifact,
    assert_released_v2_header,
    assert_released_v2_physical_artifact,
    restore_released_v2_artifact,
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
    "assert_released_event_payload",
    "assert_released_session_format_header",
    "assert_released_surface_metadata",
    "assert_released_v1_artifact",
    "assert_released_v1_header",
    "assert_released_v1_physical_artifact",
    "assert_released_v0_source_artifact",
    "assert_normalized_released_v0_artifact",
    "restore_released_v1_artifact",
    "RELEASED_V2_RELATIONSHIP_EXTENSIONS",
    "assert_released_v2_artifact",
    "assert_released_v2_header",
    "assert_released_v2_physical_artifact",
    "restore_released_v2_artifact",
    "V0_TO_V1",
    "V1_TO_V2",
    "SESSION_FORMAT_CATALOG",
    "migrate_released_artifact",
    "migrate_released_header",
]
