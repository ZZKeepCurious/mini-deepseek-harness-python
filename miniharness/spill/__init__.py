"""溢出存储域（对齐 packages/spill/{spill,spill-local,spill-policy}）。

- `SpillStore`（`ctx.spillStore`）seam：`save_text` 持久化超大文本，返回不透明 locator +
  精确字节数 + 模型可见检索提示；
- `LocalSpillStore`：私有 root（缺省进程级 `dsh-spill-` 临时目录）下按会话哈希分目录、
  `encode_segment` 注入式安全段名、`O_EXCL` 0600 写入；
- `install_spill_policy`：`tools/post-execute` 结果转换器——全文本 `content` 超 `maxInlineBytes`
  时把完整文本落盘、替换为有界 head/tail 预览 + 溢出提示（best-effort，失败保留原结果）。

载体差异（登记）：上游 `@deepseek-ai/dsh-output-retention` 的 TextRetainer 预览与
`Omitted`/`describeOmitted` 文案在 mini 内联（该包不在 M5 范围）；`tools/ptc-dispatch-log`
次臂不承载（mini PTC 子派发日志路径已由 run_code 承载）；spill-local 的启动清理
（cleanup.ts）不承载。
"""
from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
from dataclasses import dataclass
from typing import Any

from ..core.scope import Context, Service

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
    "session_dir",
]

DEFAULT_ROOT_PREFIX = "dsh-spill-"


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


# ---------- 溢出策略（tools/post-execute 结果转换） ----------


def describe_omitted(omitted: dict, unit: str) -> str:
    """内联 output-retention 的 `describeOmitted` 文案。"""
    kind = omitted.get("kind")
    if kind == "none":
        return ""
    if kind == "unknown":
        return f"More {unit} were omitted."
    return f"Omitted {omitted.get('count', 0)} {unit}."


def format_spill_notice(omitted: dict, ref: SpillRef) -> str:
    return (f"({describe_omitted(omitted, 'bytes')} Full formatted result stored at: "
            f"{ref.locator}. {ref.retrieval_hint})")


def has_spill_notice(text: str) -> bool:
    return text.endswith(")") and " Full formatted result stored at: " in text


def _all_text(content: Any) -> str | None:
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            return None
        parts.append(block.get("text", ""))
    return "".join(parts)


def _head_tail(text: str, budget: int) -> tuple[str, dict]:
    head_budget = (budget + 1) // 2
    tail_budget = budget // 2
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text, {"kind": "none"}
    head = raw[:head_budget].decode("utf-8", errors="ignore")
    tail = raw[len(raw) - tail_budget:].decode("utf-8", errors="ignore") if tail_budget else ""
    omitted = len(raw) - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
    return head + tail, {"kind": "exact", "count": omitted}


def _owner_session_id(exec_: Any) -> str | None:
    agent = getattr(exec_, "agent", None)
    session = getattr(agent, "session", None)
    return getattr(session, "session_id", None)


def install_spill_policy(ctx: Context, *, max_inline_bytes: int | None = None) -> None:
    """注册 `tools/post-execute` 溢出策略；`max_inline_bytes` 缺省 → 不注册（no-op）。"""
    if max_inline_bytes is None:
        return
    if not isinstance(max_inline_bytes, int) or isinstance(max_inline_bytes, bool) \
            or max_inline_bytes < 0:
        raise ValueError("spill-policy: maxInlineBytes must be a non-negative integer")
    cap = max_inline_bytes

    def on_post_execute(payload: Any, next_: Any) -> Any:
        downstream = next_()
        if not isinstance(downstream, dict):
            return downstream
        text = _all_text(downstream.get("content"))
        if text is None:
            return downstream
        if len(text.encode("utf-8")) <= cap:
            return downstream
        exec_ = payload.get("exec")
        session_id = _owner_session_id(exec_)
        store = ctx.get("spillStore")
        if session_id is None or store is None or not hasattr(store, "save_text_sync"):
            return downstream
        try:
            preview, omitted = _head_tail(text, cap)
            ref = store.save_text_sync({
                "owner": {"sessionId": session_id},
                "source": {"kind": "tool", "toolName": getattr(payload.get("tool"), "name", "tool"),
                           "callId": getattr(exec_, "call_id", "") or "", "label": "result"},
                "suggestedName": f"{getattr(payload.get('tool'), 'name', 'tool')}.txt",
                "content": text,
            })
        except BaseException:  # noqa: BLE001 - best-effort：spill 失败绝不改写成功结果
            return downstream
        replaced = dict(downstream)
        replaced["content"] = [{"type": "text",
                                "text": f"{preview}\n\n{format_spill_notice(omitted, ref)}"}]
        return replaced

    ctx.on("tools/post-execute", on_post_execute, prepend=True)
