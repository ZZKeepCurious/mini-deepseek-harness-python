"""追加式事件日志 + 消息模型 + 崩溃恢复：纯 stdlib 事件溯源核心。

对应 dsh 真实源码：packages/core/session（会话与投影）+ packages/llm/llm（消息模型）
+ packages/session/session-persistence-jsonl（JSONL 持久化）。

事件信封：{type, seq, time, data}，seq 从 0 起单调递增（seq == log.length），
surface 事件额外携带 surfaceOp（'append' 或 {op:'replace', start, end}）与可选
sourceEventSeqs。

硬性规定（谁违反谁报错，绝不让坏事件进日志）：
  1. 类型必须在 KNOWN_TYPES 中，未知类型直接 ValueError
  2. surface 事件必须带 surfaceOp，非 surface 事件禁止携带
  3. 所有事件必须无损 JSON 序列化（is_json_safe），冻结后不可变
  4. 崩溃恢复（repair_interrupted_turn）只补确定性事件，不删除任何已有事件

崩溃恢复在加载路径使用：persistence 读回 JSONL 后调用 repair，保持日志自洽；
事件审计与重放（Trajectory 折叠、子 agent 复制）都依赖这条恢复保证。

聚合再导出：族内拆分为 types / json / message / invariant / repair / surface /
session 七个模块，本包保持旧模块面（全集再导出）。

显式 __all__（必需）：无 __all__ 时 `from .session import *`（core/__init__ 的
星号导入）会把包命名空间里所有公开名一并复制——包括与本包同名的子模块属性
session（指向 session.py）——从而覆盖父模块的 session 包引用；__all__ 保证
星号导入只导出契约名（上游 index.ts 同为显式导出）。
"""
from .types import *  # noqa: F401,F403
from .json import *  # noqa: F401,F403
from .message import *  # noqa: F401,F403
from .invariant import *  # noqa: F401,F403
from .repair import *  # noqa: F401,F403
from .surface import *  # noqa: F401,F403
from .session import *  # noqa: F401,F403

__all__ = [
    "KNOWN_TYPES",
    "NEXT_STEP",
    "NEXT_TURN",
    "SESSION_FORMAT_VERSION",
    "SURFACE_TYPES",
    "Session",
    "TOOL_NOT_STARTED",
    "TOOL_OUTCOME_UNKNOWN",
    "create_message",
    "deep_freeze",
    "derive_event_message",
    "derive_messages",
    "file_block",
    "image_block",
    "is_json_safe",
    "now_ms",
    "reasoning_block",
    "repair_interrupted_turn",
    "text_block",
    "thaw",
    "tool_call_block",
    "tool_result_block",
    "turn_balance",
    "validate_event",
]