"""web downloads 域：GET /api/session.export（对齐 `packages/host/apiproxy` 的
session-export.ts + api/downloads.ts + fetch/handler.ts 的 GET 下载通道）。

契约（逐条对应上游）：
  * 路由：`GET|HEAD /api/session.export?sessionId=<id>[&includeDescendants=true|false]`。
  * 查询校验：sessionId 缺失/非法 → 400；includeDescendants 非 true/false/缺省 → 400。
  * 状态码（仅表达载体层）：200（zip 附件）/ 400（查询坏）/ 404（根会话无制品）/
    501（持久化后端不支持逐会话原始制品，如 SQLite）/ 500（服务缺失或中间读失败，
    正文恒为 `session log export failed to prepare the stored artifact`，绝不泄漏
    后端路径——对齐 upstream 的私有错误安全壳）。
  * 响应头：content-type=application/zip；Content-Disposition: attachment;
    filename="dsh-session-<safe>.zip"（safe = 非 [A-Za-z0-9_-] 折下划线）。
  * 归档条目（zip 顺序）：根制品逐字置于其原始文件名（session.v2.jsonl）→ 每个
    subagent 后代置于 `subagents/<safe-id>/<filename>`（按 lineage BFS，seen-set
    去重）→ 每个被含日志引用的独立媒体置于 `media/<attachmentId>.<ext>`（内容寻址）。
    根制品在读出前先经 live-session 的 flush 栅栏落盘（cold 会话无需）。

载体差异（保留简化，须在 AGENTS.md §3.4 标注）：
  * 上游用 fflate 流式分块 + 响应队列高水位背压；mini 用 stdlib zipfile 在内存
    一次性成档（无逐块流控，归档规模受内存约束；mini 无 Web 长连接背压需求）。
  * 上游经 sessionQuery.traceSession 取后代谱系；mini 无查询引擎，按持久化/
    内存 store 的 header.meta.parentSession 做 BFS 重建（语义等价：子代理必然
    经父会话 meta 登记）。
  * 上游的媒体经 attachments 服务读字节；mini 无持久化 attachment store（图片
    以内联引用经工具结果透传），故 media 条目仅在注入了一个提供 read_image 的
    attachments 服务时才产出，默认省略——归档的日志文本本身已 verbatim 含引用。
  * 压缩等级：上游 sessionExportCompressionLevel（缺省 6，整数 0-9）；mini 同款
    常量 DEFAULT_SESSION_LOG_COMPRESSION_LEVEL=6，可经 ctx.get 同名键覆盖。
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from typing import Any, Callable

from ..core.session import SESSION_FORMAT_VERSION, thaw

__all__ = [
    "DEFAULT_SESSION_LOG_COMPRESSION_LEVEL",
    "SESSION_LOG_FILENAME",
    "SessionLogExportDeps",
    "resolve_export_deps",
    "safe_session_id_segment",
    "session_log_zip_filename",
    "parse_export_query",
    "build_session_export",
    "ExportResult",
]

DEFAULT_SESSION_LOG_COMPRESSION_LEVEL = 6

#: 导出制品名（上游 session-log-export archive.ts
#: `SESSION_LOG_FILENAME = sessionFormatLogFilename(SESSION_FORMAT_VERSION)`）。
SESSION_LOG_FILENAME = f"session.v{SESSION_FORMAT_VERSION}.jsonl"

#: 上游媒体类型 → zip 扩展名（session-export.ts MEDIA_TYPE_EXTENSIONS）。
_MEDIA_TYPE_EXTENSIONS: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}

#: 私有错误安全壳（upstream：正文不泄漏 /host/private/ 后端路径）。
_PREPARE_FAILED = "session log export failed to prepare the stored artifact"


@dataclass
class SessionLogExportDeps:
    """session-log 导出所需的服务（live store 可选）。

    upstream sessionLogExportDeps 四件套（sessionQuery→lineage、sessionPersistence、
    attachments、sessions）；mini 把 sessionQuery 的谱系重建并入 persistence 与
    sessions 的 header.meta.parentSession，故字段收敛为三者。
    """

    sessions: Any | None = None
    persistence: Any | None = None
    attachments: Any | None = None


class ExportResult:
    """导出响应（与 FastAPI Response 解耦，便于纯函数测试）。"""

    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body


def resolve_export_deps(ctx: Any) -> SessionLogExportDeps:
    """从组合 ctx 解析导出依赖（缺则 None）。

    upstream sessionLogExportDeps：ctx.get 取 sessionQuery/sessionPersistence/
    attachments/sessions；mini 用 persistence（durable 谱系）+ sessions（live
    flush）+ attachments（可选媒体）。
    """

    def _get(name: str) -> Any | None:
        try:
            return ctx.get(name)
        except Exception:  # noqa: BLE001 - 未登记服务返回 None（宽松，对齐 ctx.get default）
            return None

    return SessionLogExportDeps(
        sessions=_get("sessions"),
        persistence=_get("sessionPersistence"),
        attachments=_get("attachments"),
    )


def safe_session_id_segment(session_id: str) -> str:
    """单段文件系统安全化（upstream safeSessionIdSegment：[^A-Za-z0-9_-]→_）。"""
    return re.sub(r"[^A-Za-z0-9_-]", "_", session_id)


def session_log_zip_filename(session_id: str) -> str:
    """归档附件文件名（upstream sessionLogZipFilename）。"""
    return f"dsh-session-{safe_session_id_segment(session_id)}.zip"


def parse_export_query(params: dict[str, str]) -> tuple[str, bool] | None:
    """解析 GET 查询为 (sessionId, includeDescendants)。

    sessionId 缺省/空串 → None（调用方 400）；includeDescendants 仅接受
    'true'/'false'/缺省，其它值 → None（调用方 400，upstream 防止拼错静默漏导出）。
    """
    session_id = params.get("sessionId")
    if not session_id:
        return None
    raw = params.get("includeDescendants")
    if raw is None:
        return (session_id, False)
    if raw == "true":
        return (session_id, True)
    if raw == "false":
        return (session_id, False)
    return None


def _session_to_jsonl(session: Any) -> str:
    """把一个活会话的事件日志重建成 *事件文本*（不含 header 行）。

    对照上游：zip 条目 content 是 SessionRawArtifact.content（事件日志），
    header 由独立的 meta 承载、不进 zip 条目。Mini 持久化文件首行是 header，
    导出时剥离首行只保留事件行，与上游 content 语义一致。
    """
    lines = [json.dumps(thaw(event), ensure_ascii=False) for event in session.events]
    return "\n".join(lines) + "\n" if lines else ""


def _events_text(raw: str) -> str:
    """从持久化原始制品里剥离 header 行，只保留事件文本（upstream content）。"""
    lines = raw.split("\n")
    # 首行是 header（含 version），其后才是事件行；尾部空串由 join 自然丢弃。
    return "\n".join(lines[1:])


def _read_raw_artifact(deps: SessionLogExportDeps, session_id: str) -> tuple[str, str] | None:
    """读出根制品 (filename, content) 或 None。

    判定层级（对照上游 readRaw 语义：制品 = 已落盘/可还原的事件日志）：
      - 先 flush live（内存事实来源）；
      - 若 persistence 存在：read_raw 抛 NotImplementedError → 调用方 501；
        返回文本 → 用（剥离 header 的事件文本）；返回 None 且 live 有事件 →
        内存真相优先；返回 None 且 live 无事件 → 视为「无制品」返回 None
        （调用方：根 404 / 后代 500）；
      - 若 persistence 不存在（纯内存 web store）：live 即真相，事件为空也
        导出空条目（""）。
    """
    live = None
    if deps.sessions is not None:
        live = deps.sessions.get(session_id)
        if live is not None:
            try:
                deps.sessions.flush(live)
            except Exception:  # noqa: BLE001 - flush 失败不阻断（内存已是事实来源）
                pass
    live_events = getattr(live, "events", []) if live is not None else []
    if deps.persistence is not None:
        # read_raw 抛 NotImplementedError → 后端不支持逐会话原始制品（调用方 501）；
        # 其它异常 → 准备失败壳（调用方 500）。均向上传播由 build_session_export 分类。
        raw = deps.persistence.read_raw(session_id)
        if raw is not None:
            return (SESSION_LOG_FILENAME, _events_text(raw))
        if live_events:
            return (SESSION_LOG_FILENAME, _session_to_jsonl(live))
        return None
    if live is not None:
        return (SESSION_LOG_FILENAME, _session_to_jsonl(live))
    return None


def _trace_descendants(deps: SessionLogExportDeps, root_id: str) -> list[str]:
    """按 parentSession 谱系 BFS 还原后代 id 列表（seen-set 去重）。"""
    parent_of: dict[str, str | None] = {}
    if deps.persistence is not None:
        for header in deps.persistence.list_headers():
            meta = header.get("meta") or {}
            parent_of[header["id"]] = meta.get("parentSession")
    if deps.sessions is not None:
        for session in deps.sessions.list():
            meta = getattr(session, "meta", None) or {}
            parent_of[session.session_id] = meta.get("parentSession")
    seen: set[str] = {root_id}
    result: list[str] = []
    frontier = [root_id]
    while frontier:
        current = frontier.pop()
        for child_id, parent in parent_of.items():
            if parent == current and child_id not in seen:
                seen.add(child_id)
                result.append(child_id)
                frontier.append(child_id)
    return result


def _collect_image_refs(content: Any) -> list[dict]:
    """扫描一个 content 数组里的 image 引用（upstream collectImageRefs，含嵌套）。"""
    refs: list[dict] = []
    if not isinstance(content, list):
        return refs
    pending: list[Any] = list(content)
    while pending:
        value = pending.pop()
        if not isinstance(value, dict):
            continue
        attachment = value.get("attachment")
        if value.get("type") == "image" and isinstance(attachment, dict) and "attachmentId" in attachment:
            refs.append(attachment)
        nested = value.get("content")
        if isinstance(nested, list):
            pending.extend(nested)
    return refs


def _event_image_refs(event: Any) -> list[dict]:
    """扫描单个事件各载体（content/message.content/inserted[].content）。

    V2：assistant/chunk 事件废止（块内嵌 assistant/message.content），chunk
    载体扫描移除。"""
    if not isinstance(event, dict):
        return []
    data = event.get("data")
    if not isinstance(data, dict):
        return []
    refs: list[dict] = []
    refs.extend(_collect_image_refs(data.get("content")))
    message = data.get("message")
    if isinstance(message, dict):
        refs.extend(_collect_image_refs(message.get("content")))
    inserted = data.get("inserted")
    if isinstance(inserted, list):
        for message in inserted:
            if isinstance(message, dict):
                refs.extend(_collect_image_refs(message.get("content")))
    return refs


def _media_entries(deps: SessionLogExportDeps, contents: list[str]) -> list[tuple[str, bytes]]:
    """收集被引媒体条目；仅当 attachments 服务支持 read_image 时产出字节。"""
    if deps.attachments is None or not hasattr(deps.attachments, "read_image"):
        return []
    seen: dict[str, dict] = {}
    for content in contents:
        for line in content.split("\n"):
            if not line:
                continue
            try:
                refs = _event_image_refs(json.loads(line))
            except (ValueError, TypeError):
                continue
            for ref in refs:
                seen[_string_of(ref.get("attachmentId"))] = ref
    entries: list[tuple[str, bytes]] = []
    for ref in seen.values():
        media_type = ref.get("mediaType", "")
        ext = _MEDIA_TYPE_EXTENSIONS.get(media_type)
        if ext is None:
            continue
        try:
            stored = deps.attachments.read_image(ref)
        except Exception:  # noqa: BLE001 - 媒体读失败归 500（调用方 fail-loud）
            raise
        if stored is None:
            continue
        data = stored if isinstance(stored, (bytes, bytearray)) else stored.get("data")
        if data is None:
            continue
        path = f"media/{ref.get('attachmentId')}.{ext}"
        entries.append((path, bytes(data)))
    return entries


def _string_of(value: Any) -> str:
    return "" if value is None else str(value)


def build_session_export(
    ctx: Any,
    session_id: str,
    include_descendants: bool,
    *,
    method: str = "GET",
    compression_level: int | None = None,
    signal: Any = None,
) -> ExportResult:
    """构建一次 session-log 导出响应（纯函数，便于测试）。

    @param ctx - 组合根 ctx（经 resolve_export_deps 取服务）。
    @param session_id - 根会话 id。
    @param include_descendants - 是否含 subagent 后代。
    @param method - 'GET' 产出正文；'HEAD' 仅预检（同 200 + 头 + 空体）。
    @param compression_level - 0-9；None → DEFAULT_SESSION_LOG_COMPRESSION_LEVEL（或
        ctx.get('sessionExportCompressionLevel')）。
    @param signal - 预留取消信号（mini 同步载体暂无消费方，签名对齐上游）。
    @returns ExportResult（status/headers/body）。
    """
    _ = signal  # mini 同步载体无异步取消消费方；签名保留对齐上游 AbortSignal
    deps = resolve_export_deps(ctx)
    if deps.sessions is None and deps.persistence is None:
        return ExportResult(500, {"content-type": "text/plain"}, _PREPARE_FAILED.encode("utf-8"))

    level = compression_level
    if level is None:
        try:
            level = ctx.get("sessionExportCompressionLevel")
        except Exception:  # noqa: BLE001 - 未配置 → 缺省
            level = DEFAULT_SESSION_LOG_COMPRESSION_LEVEL
    if not isinstance(level, int) or not 0 <= level <= 9:
        level = DEFAULT_SESSION_LOG_COMPRESSION_LEVEL

    try:
        root = _read_raw_artifact(deps, session_id)
    except NotImplementedError:
        # 持久化后端不支持逐会话原始制品（如 SQLite）
        return ExportResult(501, {"content-type": "text/plain"},
                            b"session-persistence backend does not expose per-session raw artifacts")
    except Exception:  # noqa: BLE001 - 私有错误安全壳
        return ExportResult(500, {"content-type": "text/plain"}, _PREPARE_FAILED.encode("utf-8"))
    if root is None:
        return ExportResult(404, {"content-type": "text/plain"}, b"session not found")

    filename = session_log_zip_filename(session_id)
    headers = {
        "content-type": "application/zip",
        "content-disposition": f'attachment; filename="{filename}"',
    }
    if method == "HEAD":
        return ExportResult(200, headers, b"")

    entries: list[tuple[str, Any]] = [(root[0], root[1])]
    contents_for_media: list[str] = [root[1]]
    if include_descendants:
        try:
            for child_id in _trace_descendants(deps, session_id):
                child = _read_raw_artifact(deps, child_id)
                if child is None:
                    # 后代缺制品 → 整档失败（upstream：fail-loud，绝不静默漏导出）
                    return ExportResult(500, {"content-type": "text/plain"},
                                        _PREPARE_FAILED.encode("utf-8"))
                path = f"subagents/{safe_session_id_segment(child_id)}/{child[0]}"
                entries.append((path, child[1]))
                contents_for_media.append(child[1])
        except Exception:  # noqa: BLE001 - 谱系/读失败 → 500 安全壳
            return ExportResult(500, {"content-type": "text/plain"}, _PREPARE_FAILED.encode("utf-8"))

    try:
        entries.extend(_media_entries(deps, contents_for_media))
    except Exception:  # noqa: BLE001 - 媒体读失败 → 500 安全壳
        return ExportResult(500, {"content-type": "text/plain"}, _PREPARE_FAILED.encode("utf-8"))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=level) as archive:
        for path, payload in entries:
            archive.writestr(path, payload)
    body = buffer.getvalue()
    return ExportResult(200, headers, body)
