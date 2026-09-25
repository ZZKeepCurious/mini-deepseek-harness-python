"""溢出存储域（对齐 packages/spill/{spill,spill-local,spill-policy}）。

- `SpillStore`（`ctx.spillStore`）seam：`save_text` 持久化超大文本，返回不透明 locator +
  精确字节数 + 模型可见检索提示；
- `LocalSpillStore`：私有 root（缺省进程级 `dsh-spill-` 临时目录）下按会话哈希分目录、
  `encode_segment` 注入式安全段名、`O_EXCL` 0600 写入；
- `install_spill_policy`：`tools/post-execute` 结果转换器——可保留内容（text/image）
  的估算 token 超 `maxInlineTokens` 时把完整文本（图片位置写成可读地址）落盘、
  替换为有界 head/tail 预览 + 溢出提示（best-effort，失败保留原结果）。图片永不
  切分；整图被省略时在提示里报告 ` Omitted N images.`。嵌套 PTC 复合结果（含图）
  同样保留，省略图片经 source `{kind:'ptc-mode'}` 的 additionalContexts user 消息重注。

载体差异（登记）：
  * 上游 `@deepseek-ai/dsh-output-retention` 的 TextRetainer 预览与 `Omitted`/
    `describeOmitted` 文案在 mini 内联（该包不在 M5 范围）；本模块的 token 预算
    head/tail 与整图保留对齐 spill-policy/retention.ts。
  * 上游 `tools/ptc-dispatch-log` 次臂不承载：mini PTC 子派发日志路径已由 run_code
    的 `tool/ptc-dispatch` 承载，故 post-execute 对嵌套结果照常保留（不跳过文本型
    嵌套调用），语义与「post-execute + dispatch-log 两臂」的净效果一致。
  * spill-local 的启动清理（cleanup.ts）不承载。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from typing import Any

from ..attachment.types import AttachmentId, Dimensions, ImageAttachmentRef
from ..core.scope import Context, Service
from ..core.session import create_message
from ..llm.content import resolve_image_attachment_access
from ..llm.token_meter import estimate_content
from .retention import retain_content

__all__ = [
    "DEFAULT_ROOT_PREFIX",
    "LocalSpillStore",
    "SpillStore",
    "describe_omitted",
    "encode_segment",
    "format_spill_notice",
    "has_spill_notice",
    "install_local_spill",
    "install_spill_policy",
    "retain_content",
    "session_dir",
]

DEFAULT_ROOT_PREFIX = "dsh-spill-"

# 消息 notice 的持久化拼写常量（对齐 spill-policy/notice.ts）。
_OPEN = "("
_CLOSE = ")"
_LOCATION = " Full formatted result stored at: "
_GUIDANCE_SEPARATOR = ". "
_SEPARATOR = "\n\n"
# describeOmitted({kind:'exact',count:0},'bytes') = "Omitted 0 bytes."
_EXACT_ZERO = "Omitted 0 bytes."
_COUNT_OFFSET = _EXACT_ZERO.index("0")
_COUNT_SUFFIX = _EXACT_ZERO[_COUNT_OFFSET + 1:]
_GAP = {"type": "text", "text": "\n\n[...]\n\n"}
_MAX_SAFE_INTEGER = 2 ** 53 - 1
_IMAGE_NOTICE = re.compile(r" Omitted ([1-9][0-9]*) images\.$")


@dataclass(frozen=True)
class SpillRef:
    locator: str
    bytes: int
    retrieval_hint: str


class SpillStore(Service):
    """抽象溢出存储（`ctx.spillStore`）。"""

    provide = "spillStore"

    def __init__(self, ctx: Context):
        super().__init__(ctx, "spillStore")

    async def save_text(self, input_: dict) -> SpillRef:
        """持久化 `input_['content']`，返回 `SpillRef`；存储失败必须 reject。"""
        raise NotImplementedError


def encode_segment(raw: str) -> str:
    """把任意字符串编码成单段安全路径（注入式覆盖全部 UTF-16 码元，对齐 encodeSegment）。"""
    if raw == "":
        return "~"
    if raw == ".":
        return "~002E"
    if raw == "..":
        return "~002E~002E"
    out = []
    for char in raw:
        code = ord(char)
        if char != "~" and (char.isascii() and (char.isalnum() or char in "._-")):
            out.append(char)
        else:
            out.append("~" + format(code, "04X"))
    return "".join(out)


def session_dir(root: str, session_id: str) -> str:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return os.path.join(root, f"session-{digest}")


def _private_root() -> str:
    return tempfile.mkdtemp(prefix=DEFAULT_ROOT_PREFIX)


class LocalSpillStore(SpillStore):
    """本地文件系统溢出后端。"""

    def __init__(self, ctx: Context, *, root: str | None = None):
        super().__init__(ctx)
        self.root = root or _private_root()

    async def save_text(self, input_: dict) -> SpillRef:
        return self.save_text_sync(input_)
    def save_text_sync(self, input_: dict) -> SpillRef:
        owner = input_.get("owner") or {}
        session_id = owner.get("sessionId")
        if not isinstance(session_id, str) or session_id == "":
            raise ValueError("spill owner.sessionId must be a non-empty string")
        content = input_.get("content")
        if not isinstance(content, str):
            raise TypeError("spill content must be a string")
        suggested = input_.get("suggestedName") or "spill.txt"
        directory = session_dir(self.root, session_id)
        name = f"{secrets.token_hex(6)}-{encode_segment(str(suggested))}"
        path = os.path.join(directory, name)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        return SpillRef(locator=path, bytes=len(content.encode("utf-8")),
                        retrieval_hint=f"Read {path} to retrieve the full result.")


def install_local_spill(ctx: Context, *, root: str | None = None) -> LocalSpillStore:
    """幂等装配本地溢出后端（`ctx.spillStore`）。"""
    existing = ctx.get("spillStore")
    if existing is not None:
        return existing
    return LocalSpillStore(ctx, root=root)


# ---------- 溢出提示（spill-policy/notice.ts） ----------


def describe_omitted(omitted: dict, unit: str) -> str:
    """内联 output-retention 的 `describeOmitted` 文案。"""
    kind = omitted.get("kind")
    if kind == "none":
        return ""
    if kind == "unknown":
        return f"More {unit} were omitted."
    return f"Omitted {omitted.get('count', 0)} {unit}."


def format_spill_notice(omitted: dict, ref: SpillRef, images: int = 0) -> str:
    """格式化保留预览末尾的提示（保留持久化拼写）。

    @param omitted - 保留策略省略的字节量（对齐 upstream Omitted）。
    @param ref - 已保存文本的 locator 与检索提示。
    @param images - 与文本一同被整块省略的图片数。
    """
    image_notice = f" Omitted {images} images." if images > 0 else ""
    return (f"{_OPEN}{describe_omitted(omitted, 'bytes')}{image_notice}{_LOCATION}"
            f"{ref.locator}{_GUIDANCE_SEPARATOR}{ref.retrieval_hint}{_CLOSE}")


def _safe_int(text: str) -> int | None:
    """JS `Number` 安全整数的 Python 近似：仅接受规范整数串且 |n| <= 2^53-1。"""
    if not text or not (text[0] == "-" and text[1:].isdigit() or text.isdigit()):
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if abs(value) <= _MAX_SAFE_INTEGER else None


def _is_omission(text: str) -> bool:
    """识别一种 producer 能产出的省略子句（对齐 notice.ts isOmission）。"""
    match = _IMAGE_NOTICE.search(text)
    if match is not None:
        if _safe_int(match.group(1)) is None:
            return False
        text = text[:match.start()]
    if text == describe_omitted({"kind": "none"}, "bytes") \
            or text == describe_omitted({"kind": "unknown"}, "bytes"):
        return True
    raw_count = text[_COUNT_OFFSET:len(text) - len(_COUNT_SUFFIX)] \
        if len(text) >= _COUNT_OFFSET + len(_COUNT_SUFFIX) else ""
    count = _safe_int(raw_count)
    return count is not None and count >= 0 \
        and text == describe_omitted({"kind": "exact", "count": count}, "bytes")


def has_spill_notice(text: str) -> bool:
    """识别持久化文本末尾的完整溢出提示（含仅提示输出）。

    对齐 notice.ts hasSpillNotice：从末尾向前扫描 `\\n\\n(` 分段，定位
    ` Full formatted result stored at: ` 与检索提示分隔符；识别的是文本约定，
    不是经过认证的工具输出来源。
    """
    if not text.endswith(_CLOSE):
        return False
    start = 0
    while True:
        next_at = text.find(f"{_SEPARATOR}{_OPEN}", start)
        candidate = text[start:next_at if next_at >= 0 else -len(_CLOSE)]
        location = candidate.find(_LOCATION, len(_OPEN))
        if candidate.startswith(_OPEN) and location >= 0 \
                and _is_omission(candidate[len(_OPEN):location]):
            return text.find(_GUIDANCE_SEPARATOR, start + location + len(_LOCATION)) >= 0
        if next_at < 0:
            return False
        start = next_at + len(_SEPARATOR)


# ---------- 溢出策略（tools/post-execute 结果转换） ----------


def _retainable(content: list) -> bool:
    """仅当全部块都是 text/image 时才可保留（不重写不支持的块）。"""
    return all(isinstance(block, dict) and block.get("type") in ("text", "image")
               for block in content)


def _owner_session_id(exec_: Any) -> str | None:
    agent = getattr(exec_, "agent", None)
    session = getattr(agent, "session", None)
    return getattr(session, "session_id", None)


def _route(exec_: Any) -> tuple[str | None, str | None]:
    """最新 durable 路由的 provider/model（request/header 优先，options 回退）。"""
    agent = getattr(exec_, "agent", None)
    session = getattr(agent, "session", None)
    provider = model = None
    if session is not None and hasattr(session, "request_header"):
        header = session.request_header()
        config = (header or {}).get("config") if isinstance(header, dict) else None
        if isinstance(config, dict):
            provider, model = config.get("provider"), config.get("model")
    options = getattr(agent, "options", None)
    if provider is None:
        provider = options.get("provider") if isinstance(options, dict) \
            else getattr(options, "provider", None)
    if model is None:
        model = options.get("model") if isinstance(options, dict) \
            else getattr(options, "model", None)
    return provider, model


def _image_calculator(ctx: Context, exec_: Any):
    """当前模型路由的请求图计价器（上游 ctx.get('llm').imageRequestPricing）。

    mini 无全局 `llm` 服务，路由能力挂在 `exec.agent.adapter` 上；无适配器或
    路由缺 provider/model 时返回 None（与上游「无计价器」同路：保留内联）。
    """
    agent = getattr(exec_, "agent", None)
    adapter = getattr(agent, "adapter", None)
    pricing = getattr(adapter, "image_request_pricing", None)
    if pricing is None:
        return None
    _provider, model = _route(exec_)
    if model is None:
        return None
    try:
        return pricing(model)
    except Exception:  # noqa: BLE001 - 计价器构建失败等同不可计价
        return None


def _pricing(ctx: Context, exec_: Any, images: list):
    """按模型路由给保留块计价（文本走 estimate_content，图片走 provider visual tokens 加其文本）。"""
    costs: dict[int, int] = {}
    if images:
        calculator = _image_calculator(ctx, exec_)
        if calculator is None:
            raise RuntimeError("the current model has no image token calculator")
        prices = calculator.price_images(images)
        if len(prices) != len(images):
            raise RuntimeError("image token calculator returned an inconsistent occurrence count")
        for image, cost in zip(images, prices):
            costs[id(image)] = cost.visualTokens + estimate_content(
                [{"type": "text", "text": cost.text}])

    def price(block: dict) -> int:
        if block.get("type") == "image":
            return costs[id(block)]
        return estimate_content([block])

    return price


def _attachment_ref(value: Any) -> ImageAttachmentRef:
    """image 块的 durable 引用（dict 或 dataclass）→ ImageAttachmentRef。"""
    if isinstance(value, ImageAttachmentRef):
        return value
    original = value.get("originalDimensions")
    return ImageAttachmentRef(
        attachmentId=AttachmentId(str(value["attachmentId"])),
        mediaType=value["mediaType"], bytes=value["bytes"],
        width=value["width"], height=value["height"], name=value.get("name"),
        originalDimensions=(Dimensions(**original) if isinstance(original, dict) else original),
    )


def _full_text(ctx: Context, content: list) -> str:
    """完整有序文本，在每个图片位置写入执行世界可读的附件路径（对齐 fullText）。"""
    parts = []
    for block in content:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
            continue
        ref = _attachment_ref(block.get("attachment") or {})
        attachments = ctx.get("attachments")
        fs = ctx.get("fs")
        access = None
        if attachments is not None and fs is not None:
            access = resolve_image_attachment_access(
                attachments, lambda path: fs.process_path_from_host_path(path), ref)
        if access is None:
            raise RuntimeError(f"image {ref.attachmentId} has no readable attachment path")
        parts.append(
            f'\n[Image: {json.dumps(access.readonlyPath)}; {ref.mediaType}; '
            f"{ref.width}x{ref.height}. Use read_image to view it.]\n")
    return "".join(parts)


def _log_warn(ctx: Context, message: str) -> None:
    if hasattr(ctx, "logger"):
        ctx.logger.warn(message)


def _bound(ctx: Context, exec_: Any, content: list, max_tokens: int,
           tool_name: str, label: str) -> list | None:
    """一次可恢复的有界预览；失败返回 None 让成功工具内容保持可见。"""
    images = [block for block in content if block.get("type") == "image"]
    price = _pricing(ctx, exec_, images)
    if sum(price(block) for block in content) <= max_tokens:
        return None
    session_id = _owner_session_id(exec_)
    if session_id is None:
        raise RuntimeError(f"no session owner for {tool_name} {label}")
    store = ctx.get("spillStore")
    if store is None or not hasattr(store, "save_text_sync"):
        raise RuntimeError("no ctx.spillStore backend loaded")
    text = _full_text(ctx, content)
    ref = store.save_text_sync({
        "owner": {"sessionId": session_id},
        "source": {"kind": "tool", "toolName": tool_name,
                   "callId": getattr(exec_, "call_id", None) or "", "label": label},
        "suggestedName": f"{tool_name}.txt",
        "content": text,
    })
    total_bytes = sum(len(block.get("text", "").encode("utf-8"))
                      for block in content if block.get("type") == "text")

    def notice(bytes_: int, count: int) -> dict:
        return {"type": "text",
                "text": format_spill_notice({"kind": "exact", "count": bytes_}, ref, count)}

    worst = notice(total_bytes, len(images))
    reserved = price(_GAP) + price({"type": "text", "text": f"\n\n{worst['text']}"})
    if price(worst) > max_tokens:
        raise RuntimeError(f"spill notice for {tool_name} exceeds maxInlineTokens")
    retained = retain_content(content, max(0, max_tokens - reserved), price)
    footer = notice(retained["omittedBytes"], retained["omittedImages"])
    if len(retained["head"]) + len(retained["tail"]) == 0:
        result = [footer]
    else:
        result = [*retained["head"], _GAP, *retained["tail"],
                  {"type": "text", "text": f"\n\n{footer['text']}"}]
    # 相邻文本在 wire 与 spill preview 里共享一次框架成本。
    merged: list = []
    for block in result:
        previous = merged[-1] if merged else None
        if block.get("type") == "text" and previous is not None \
                and previous.get("type") == "text":
            previous["text"] += block["text"]
        else:
            merged.append(dict(block) if block.get("type") == "text" else block)
    return merged


def install_spill_policy(ctx: Context, *, max_inline_tokens: int | None = None) -> None:
    """注册 `tools/post-execute` 溢出策略；`max_inline_tokens` 缺省 → 不注册（no-op）。"""
    if max_inline_tokens is None:
        return
    if not isinstance(max_inline_tokens, int) or isinstance(max_inline_tokens, bool) \
            or max_inline_tokens < 0:
        raise ValueError("spill-policy: maxInlineTokens must be a non-negative integer")
    cap = max_inline_tokens

    def on_post_execute(payload: Any, next_: Any) -> Any:
        downstream = next_()
        if not isinstance(downstream, dict) or downstream.get("kind") != "accept":
            return downstream
        if "value" in downstream:
            return downstream
        exec_ = payload.get("exec") if isinstance(payload, dict) else None
        tool = payload.get("tool") if isinstance(payload, dict) else None
        tool_name = tool if isinstance(tool, str) else getattr(tool, "name", None)
        if tool_name == "read":
            return downstream
        content = downstream.get("content")
        if not isinstance(content, list) or not content:
            return downstream
        if not _retainable(content):
            return downstream
        has_images = any(block.get("type") == "image" for block in content)
        try:
            retained = _bound(
                ctx, exec_, content, cap,
                tool_name or getattr(exec_, "name", None) or "tool",
                "dispatch" if getattr(exec_, "parent", None) is not None else "result",
            )
        except BaseException as error:  # noqa: BLE001 - best-effort：spill 失败绝不改写成功结果
            _log_warn(ctx, f"spill-policy: {error}; keeping the inline content")
            return downstream
        if retained is None:
            return downstream
        contexts = list(downstream.get("additionalContexts") or [])
        # 嵌套 PTC 复合结果省略整图时，把保留预览重注为 ptc-mode user 消息。
        if (getattr(exec_, "parent", None) is not None and has_images
                and not any(block.get("type") == "image" for block in retained)):
            contexts.append(create_message("user", retained, {"kind": "ptc-mode"}))
        result = dict(downstream)
        result["kind"] = "accept"
        result["content"] = retained
        if contexts:
            result["additionalContexts"] = contexts
        return result

    ctx.on("tools/post-execute", on_post_execute, prepend=True)
