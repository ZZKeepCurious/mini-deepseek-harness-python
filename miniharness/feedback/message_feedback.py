"""消息级人工反馈（messageFeedback Remote：list / put / delete）。

上游：packages/feedback/message-feedback/src/index.ts（313 行）+ types.ts。

语义（已核实）：
  * 配置 `maxNoteBytes`（必填；构造校验正整数）。
  * 事件：`feedback/message-put`（{sessionId, item}，item 含 version UUID CAS 令牌
    + createdAt/updatedAt）+ `feedback/message-delete`（{sessionId, messageId}），
    两者 log-only 非 surface。当前反馈由整日志重放派生（current_items）——
    首建序 Map，键 = messageId。
  * `resolve_note`（任何会话/持久化查前）：undefined ok；纯空白 → note-blank；
    UTF-8 字节 > maxNoteBytes → note-too-large（maxBytes/actualBytes）。
  * `put`：目标校验（必须有一条 **append-origin 非空 assistant/message** 且
    derive_event_message().id === messageId，否则 target-not-found）→ 版本校验
    （ifVersion !== existing.version ?? null → version-conflict，current 现状）
    → 无操作（rating/note/category 全同）则**不追加事件**仍验证持久化 →
    实质变更铸新 UUID version、createdAt 保留、updatedAt 单调不减、追加事件。
    无 note/category 时键省略（category-less put 会丢掉已存 category）。
  * `delete`：存在 → 版本校验 + 追加 delete；不存在 → 不追加（恒成功
    `{absent:true}`，幂等重试稳定）。
  * 活会话路径：append → `ctx.sessions.flush`（无监听器参与 → 报错）；冷会话
    路径：persistence load + append + flush + `feedback/committed` 并行事件。
  * `list`：只读，返回冻结快照 {items}。

载体差异：上游 stat/open/read 句柄与逐条持久化验证；mini 以 `ctx.sessions` +
`ctx.sessionPersistence`（load/append/flush）承载，持久化校验的 handle 级
read-back 不承载（登记）。
"""
from __future__ import annotations

import time as _time
import uuid
from types import MappingProxyType
from typing import Any, Callable

from ..core.scope import Context, Service
from ..core.session import derive_event_message
from .command_feedback import FEEDBACK_CATEGORIES

__all__ = [
    "MessageFeedbackError",
    "MessageFeedbackService",
    "install_message_feedback",
]

_RATINGS = ("positive", "negative")


class MessageFeedbackError(Exception):
    """message-feedback 业务失败（code 进 web/envelope RPC_ERROR_CODES）。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _uuid() -> str:
    return str(uuid.uuid4())


def current_items(session_id: str, events: list) -> dict:
    """从整日志重放当前反馈（index.ts:97-115）：put 记录 / delete 移除。

    返回 Map 插入序（首建序）；put payload 经 put_schema 校验、delete 经
    delete_schema 校验；仅会话自有的条目计入（event.data.sessionId === id）。
    """
    items: dict[str, dict] = {}
    for event in events:
        etype = event.get("type")
        data = event.get("data") or {}
        if etype == "feedback/message-put":
            if data.get("sessionId") != session_id:
                continue
            item = _validate_item(data.get("item"))
            items[item["messageId"]] = item
        elif etype == "feedback/message-delete":
            if data.get("sessionId") != session_id:
                continue
            message_id = data.get("messageId")
            if isinstance(message_id, str):
                items.pop(message_id, None)
    return items


def _validate_item(value: Any) -> dict:
    """item 载荷校验（itemSchema：messageId/rating/note?/category?/version/
    createdAt/updatedAt + updatedAt>=createdAt + note 非空白精化）。"""
    if not isinstance(value, (dict, MappingProxyType)):
        raise MessageFeedbackError("invalid-record",
                                   "feedback item must be an object")
    message_id = value.get("messageId")
    if not isinstance(message_id, str) or message_id == "":
        raise MessageFeedbackError("invalid-record", "feedback item messageId must be a string")
    rating = value.get("rating")
    if rating not in _RATINGS:
        raise MessageFeedbackError("invalid-record", "feedback item rating must be positive or negative")
    version = value.get("version")
    if not isinstance(version, str) or version == "":
        raise MessageFeedbackError("invalid-record", "feedback item version must be a string")
    created_at = value.get("createdAt")
    updated_at = value.get("updatedAt")
    if not isinstance(created_at, int) or not isinstance(updated_at, int) \
            or created_at < 0 or updated_at < 0 or updated_at < created_at:
        raise MessageFeedbackError(
            "invalid-record",
            "feedback item timestamps must be non-negative with updatedAt >= createdAt")
    note = value.get("note")
    if note is not None and (not isinstance(note, str) or note.strip() == ""):
        raise MessageFeedbackError("invalid-record", "feedback item note must be non-blank")
    category = value.get("category")
    if category is not None and category not in FEEDBACK_CATEGORIES:
        raise MessageFeedbackError("invalid-record", "feedback item category is unknown")
    item = {
        "messageId": message_id, "rating": rating,
        "version": version, "createdAt": created_at, "updatedAt": updated_at,
    }
    if note is not None:
        item["note"] = note
    if category is not None:
        item["category"] = category
    return item


def _resolve_note(note: Any, max_note_bytes: int) -> dict | None:
    """note 校验（index.ts:286-294，任何查前）：None ok；纯空白/超字节 → 失败。"""
    if note is None:
        return None
    if not isinstance(note, str):
        return {"code": "note-blank", "details": {}}
    if note.strip() == "":
        return {"code": "note-blank", "details": {}}
    actual = len(note.encode("utf-8"))
    if actual > max_note_bytes:
        return {"code": "note-too-large",
                "details": {"maxBytes": max_note_bytes, "actualBytes": actual}}
    return None


def _target_exists(session_id: str, message_id: str, events: list) -> bool:
    """目标校验（index.ts:172-176）：一条 append-origin 非空 assistant/message
    的 derive_event_message().id === message_id。"""
    for event in events:
        if event.get("type") != "assistant/message":
            continue
        if event.get("surfaceOp") != "append":
            continue
        message = derive_event_message(event)
        if message is None:
            continue
        if message.get("id") == message_id:
            return True
    return False


class MessageFeedbackService(Service):
    """`messageFeedback` Remote（list / put / delete + 每会话串行队列）。"""

    provide = "messageFeedback"

    def __init__(self, ctx: Context, config: dict | None = None):
        config = config or {}
        max_note_bytes = config.get("maxNoteBytes")
        if not isinstance(max_note_bytes, int) or isinstance(max_note_bytes, bool) \
                or max_note_bytes < 1:
            raise TypeError("message-feedback: maxNoteBytes must be a positive safe integer")
        self.max_note_bytes = max_note_bytes
        super().__init__(ctx, "messageFeedback")

    # ---------- 会话解析（活 vs 冷） ----------

    def _with_session(self, session_id: str, write: bool, operation: Callable) -> Any:
        """在活/冷会话上运行操作；写路径把事件写入对应持久化载体。"""
        sessions = self.ctx.get("sessions")
        live = sessions.get(session_id) if sessions is not None else None

        if live is not None:
            def append(record: dict | None) -> None:
                if record is None:
                    return
                live.append(record["type"], record["data"])
            result = operation(list(live.snapshot_events()), append)
            if write:
                persistence = self.ctx.get("sessionPersistence")
                if persistence is not None:
                    # 持久化在场：反馈必须落盘才 ack（上游 flush 参与校验）。
                    # 持久化缺席（内存 store）时无盘可验，跳过该屏障。
                    participated = sessions.flush(live)
                    if not participated:
                        raise MessageFeedbackError(
                            "durability-unavailable",
                            f"message-feedback: no durability listener participated for "
                            f"live session '{session_id}'")
            return result

        persistence = self.ctx.get("sessionPersistence")
        if persistence is not None:
            # 存在性门（对齐上游 `stat`）：`load` 对缺失会话返回空列表，无法区分
            # 「不存在」与「空日志」；mini 以 path_of（或 inspect）探测。
            exists = True
            if hasattr(persistence, "path_of"):
                exists = persistence.path_of(session_id) is not None
            elif hasattr(persistence, "inspect"):
                try:
                    exists = persistence.inspect(session_id).get("meta") is not None
                except Exception:  # noqa: BLE001 - 探测失败按存在处理（读路径再报）
                    exists = True
            if not exists:
                raise MessageFeedbackError(
                    "session-not-found", f'session "{session_id}" not found',
                    {"sessionId": session_id})
            try:
                stored = list(persistence.load(session_id))
            except Exception as error:  # noqa: BLE001 - 持久化错误向调用方传播
                raise

            def append(record: dict | None) -> None:
                if record is None:
                    return
                persistence.append(session_id, record)

            result = operation(stored, append)
            if write:
                persistence.flush()
            return result
        raise MessageFeedbackError(
            "session-not-found", f'session "{session_id}" not found',
            {"sessionId": session_id})

    # ---------- Remote 方法 ----------

    def list(self, request: dict) -> dict:
        """`list`：读当前反馈项目（只读，冻结快照）。"""
        session_id = request.get("sessionId")

        def operate(events: list, append: Callable) -> dict:
            items = list(current_items(session_id, events).values())
            return {"items": items}

        return self._with_session(session_id, False, operate)

    def put(self, request: dict) -> dict:
        """`put`：创建/更新一条消息反馈（版本 CAS）。"""
        session_id = request.get("sessionId")
        item = request.get("item") or {}
        note = item.get("note")

        note_error = _resolve_note(note, self.max_note_bytes)
        if note_error is not None:
            raise MessageFeedbackError(note_error["code"],
                                       _note_message(note_error["code"]),
                                       note_error["details"])

        message_id = item.get("messageId")
        if not isinstance(message_id, str) or message_id == "":
            raise MessageFeedbackError("target-not-found",
                                       f'feedback target message "{message_id}" not found',
                                       {"sessionId": session_id, "messageId": message_id})
        rating = item.get("rating")
        if rating not in _RATINGS:
            raise MessageFeedbackError("invalid-record",
                                       "feedback item rating must be positive or negative")
        if_version = request.get("ifVersion") if "ifVersion" in request else item.get("ifVersion")

        def operate(events: list, append: Callable) -> dict:
            if not _target_exists(session_id, message_id, events):
                raise MessageFeedbackError(
                    "target-not-found",
                    f'feedback target message "{message_id}" not found',
                    {"sessionId": session_id, "messageId": message_id})
            existing = current_items(session_id, events).get(message_id)
            current_version = existing["version"] if existing else None
            if if_version != current_version:
                raise MessageFeedbackError(
                    "version-conflict", "message-feedback: version conflict",
                    {"current": existing})
            accepted_note = note if (note is not None and note.strip() != "") else None
            if existing is not None \
                    and existing["rating"] == rating \
                    and existing.get("note", None) == accepted_note \
                    and existing.get("category", None) == item.get("category"):
                # 无操作：不追加事件，仍验证持久化（version 保留）
                return existing
            now = int(time_ms())
            version = _uuid()
            created_at = existing["createdAt"] if existing else now
            updated_at = existing["updatedAt"] if existing else now
            if existing is not None:
                updated_at = max(now, existing["updatedAt"])
            new_item: dict[str, Any] = {
                "messageId": message_id, "rating": rating,
                "version": version, "createdAt": created_at, "updatedAt": updated_at,
            }
            if accepted_note is not None:
                new_item["note"] = accepted_note
            if item.get("category") is not None:
                new_item["category"] = item["category"]
            append({"type": "feedback/message-put",
                    "data": {"sessionId": session_id, "item": new_item}})
            return new_item

        return self._with_session(session_id, True, operate)

    def delete(self, request: dict) -> dict:
        """`delete`：删除一条消息反馈（幂等；恒返回 {absent:true}）。"""
        session_id = request.get("sessionId")
        message_id = request.get("messageId")
        if_version = request.get("ifVersion") if "ifVersion" in request else None

        def operate(events: list, append: Callable) -> dict:
            existing = current_items(session_id, events).get(message_id)
            if existing is not None:
                current_version = existing["version"]
                if if_version != current_version:
                    raise MessageFeedbackError(
                        "version-conflict", "message-feedback: version conflict",
                        {"current": existing})
                append({"type": "feedback/message-delete",
                        "data": {"sessionId": session_id, "messageId": message_id}})
            # 不存在 → 不追加（幂等）
            return {"absent": True}

        return self._with_session(session_id, True, operate)


def _note_message(code: str) -> str:
    if code == "note-blank":
        return "message-feedback: note must not be blank"
    return "message-feedback: note exceeds the configured byte limit"


def time_ms() -> int:
    """epoch 毫秒（对齐 upstream Date.now()）。"""
    return int(_time.time() * 1000)


def install_message_feedback(ctx: Context, config: dict | None = None) -> MessageFeedbackService:
    """装配 `messageFeedback`（opt-in；重复装返回既有实例）。"""
    existing = ctx.get("messageFeedback")
    if existing is not None:
        return existing
    return MessageFeedbackService(ctx, config)