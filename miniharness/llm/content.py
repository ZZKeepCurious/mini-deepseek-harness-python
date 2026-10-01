"""请求侧 image/file 块投影（上游 packages/llm/llm/src/content.ts）。

file 是第六类 ContentBlock（alpha.1）：durable verbatim 引用经 attachment 服务
存储；**任何 provider 都不原生接收 file 块**——请求组装无条件把 file 投影为
确定性 handle 文本（fileHandleText），模型按需用文件工具读取该路径。V4 起
tool/result 消息平铺 content，内容块不再嵌套（tool-result 包裹块废止）。

image 的处理分两条路径：
  * text-only 模型：把历史 image 块确定性投影为占位文本（textOnlyImageText）
  * image-capable 模型：把 image 块序列化为 wire 格式（file_id 或 inline base64）
    并通过 IMAGE_OFFLOAD_REQUIRED 机制处理超预算场景。
"""
from __future__ import annotations

import json

from .protocol import IMAGE_OFFLOAD_REQUIRED, LlmImageRequestBudget, ImageAttachmentAccess, ImageBlock

__all__ = [
    "IMAGE_OFFLOAD_REQUIRED",
    "ImageAttachmentAccess",
    "ImageBlock",
    "LlmImageRequestBudget",
    "base64_length",
    "content_has_image",
    "content_has_file",
    "file_handle_text",
    "offloaded_image_text",
    "project_files_to_text",
    "project_images_for_text_model",
    "project_offloaded_images",
    "project_tool_updates",
    "request_image_handle_text",
    "required_image_offload",
    "resolve_image_attachment_access",
    "text_only_image_text",
    "visit_image_blocks",
]


def resolve_image_attachment_access(
    attachments,
    map_host_path,
    ref: dict,
) -> ImageAttachmentAccess | None:
    """把一个 attachment provider 的宿主对象位置桥接进已挂载的工具执行世界。

    上游 resolveImageAttachmentAccess（llm/src/content.ts:33-40）：consumer 提供
    当前文件系统 provider 的映射，而不让 attachment 或 LLM 定义依赖它。任一
    provider 不暴露映射即返回 None；durable 引用非法时 attachment provider 抛错。

    @param attachments - 拥有规范化附件对象的 provider（鸭子类型：需 image_host_path）。
    @param map_host_path - 把一个绝对宿主路径映射进当前工具执行世界。
    @param ref - durable 规范化图片引用（dict）。
    @returns 只读执行世界路径，不可用时 None。
    """
    host_path = attachments.image_host_path(ref)
    if host_path is None:
        return None
    readonly_path = map_host_path(host_path)
    return None if readonly_path is None else ImageAttachmentAccess(readonly_path)


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


# ---------- 基础工具 ----------

def base64_length(bytes_count: int) -> int:
    """Base64 编码后的字节长度（含填充）。"""
    return ((bytes_count + 2) // 3) * 4


# ---------- contentHasImage / contentHasFile ----------

def content_has_image(content: list) -> bool:
    """内容块是否含 image 块（V4 平铺内容，无嵌套，上游 contentHasImage）。"""
    return any(block.get("type") == "image" for block in content or [])


def content_has_file(content: list) -> bool:
    """内容块是否含 file 块（V4 平铺内容，无嵌套，上游 contentHasFile）。"""
    return any(block.get("type") == "file" for block in content or [])


# ---------- image text 生成函数 ----------

def image_identity(ref: dict) -> str:
    """image 身份标识：name 或 attachmentId。"""
    name = ref.get("name")
    if name is None:
        return str(ref.get("attachmentId", ""))
    return f"{_quoted(str(name))} ({ref.get('attachmentId', '')})"


def text_only_image_text(ref: dict) -> str:
    """Stable text shown to a model that cannot accept one durable image reference.

    上游 textOnlyImageText 逐字文案：纯文本模型的占位符。
    """
    aid = str(ref.get("attachmentId", ""))
    digest = aid[len("sha256:"):len("sha256:") + 8] if aid.startswith("sha256:") else aid[:8]
    return f"[image omitted because this model accepts text only; attachment sha256:{digest}]"


def request_image_handle_text(
    ref: dict,
    version: dict,
    access: dict | None = None,
) -> str:
    """Stable model-facing handle for one exact request image.

    上游 requestImageHandleText：包含请求 preview 尺寸的 handle 文本。
    @param ref - durable normalized attachment reference.
    @param version - {width, height} of the request image.
    @param access - optional ImageAttachmentAccess for tool execution world.
    @returns attachment handle and request-image dimensions text.
    """
    preview = f"Image {image_identity(ref)}; request preview {version.get('width', '?')}x{version.get('height', '?')}px."
    if access is None:
        return f"{preview} It may be resized or re-encoded; source dimensions, format, and byte size may differ."
    return preview + normalized_access_text(ref, access)


def offloaded_image_text(
    ref: dict,
    access: dict | None = None,
) -> str:
    """Stable per-image placeholder for a request-limit omission.

    上游 offloadedImageText：因预算限制被卸载的 image 占位符。
    """
    identity = f"image omitted to fit request image limits; {image_identity(ref)}."
    if access is None:
        return f"[{identity} No local normalized image path is available; ask the user to attach it again if needed.]"
    return f"[{identity}{normalized_access_text(ref, access)}]"


def normalized_access_text(ref: dict, access: dict) -> str:
    """Normalized access text for image (上游 normalizedAccessText)。"""
    return (f" Normalized copy (read-only; may be resized or re-encoded): {_quoted(access.get('readonlyPath', ''))} "
            f"({ref.get('width', '?')}x{ref.get('height', '?')}px, {ref.get('mediaType', '')})."
            " Source dimensions, format, and byte size may differ."
            f" Copy to a writable path ending in .{ref.get('mediaType', 'png').split('/')[-1]} before editing.")


def file_handle_text(ref: dict, readonly_path: str | None) -> str:
    """durable file 引用的模型可见 handle（上游 fileHandleText 逐字文案）。"""
    attachment_id = str(ref.get("attachmentId") or "")
    digest = attachment_id[len("sha256:"):len("sha256:") + 8] if attachment_id.startswith("sha256:") else ""
    identity = f"File {_quoted(str(ref.get('name') or ''))} ({ref.get('bytes', 0)} bytes, sha256:{digest})"
    if readonly_path is None:
        return (f"[{identity} was uploaded, but the current execution environment cannot "
                "access a readable path. Report that limitation if its contents are needed; "
                "do not claim to have read it.]")
    return (f"[{identity}: verbatim read-only copy saved at {_quoted(readonly_path)}. "
            "Read that path with your file tools when its contents are needed; copy it to a "
            "writable location before modifying it. When delegating file work, include this "
            "saved path in the delegation prompt; only subagents sharing this execution "
            "environment can read it.]")


# ---------- image 投影函数 ----------

def _replace_offloaded_images(
    blocks: list,
    placeholder,
) -> list:
    """Replace every offloaded occurrence with placeholder text."""
    next_blocks = None
    for index, block in enumerate(blocks or []):
        if block.get("type") == "image" and block.get("offloaded") is True:
            if next_blocks is None:
                next_blocks = list(blocks[:index])
            next_blocks.append({"type": "text", "text": placeholder(block.get("attachment") or {})})
            continue
        if next_blocks is not None:
            next_blocks.append(block)
    if next_blocks is not None:
        return next_blocks
    return blocks or []


def project_offloaded_images(
    messages: list,
    placeholder,
) -> list:
    """Project the surface's offloaded occurrences into deterministic text for one request.

    上游 projectOffloadedImages：offloaded 集合是 durable surface fact，每个 route 发同样的 set。
    @param messages - derived request history.
    @param placeholder - build the model-visible replacement for one offloaded attachment.
    @returns the original list when nothing is offloaded, otherwise shallow message copies with placeholders.
    """
    result = []
    changed = False
    for message in messages:
        content = _replace_offloaded_images(message.get("content") or [], placeholder)
        if content is not (message.get("content") or []):
            replaced = dict(message)
            replaced["content"] = content
            result.append(replaced)
            changed = True
        else:
            result.append(message)
    return result if changed else messages


def _replace_images_for_text_model(blocks: list) -> list:
    """Replace every image occurrence for a text-only model."""
    next_blocks = None
    for index, block in enumerate(blocks or []):
        if block.get("type") == "image":
            if next_blocks is None:
                next_blocks = list(blocks[:index])
            next_blocks.append({"type": "text", "text": text_only_image_text(block.get("attachment") or {})})
            continue
        if next_blocks is not None:
            next_blocks.append(block)
    if next_blocks is not None:
        return next_blocks
    return blocks or []


def project_images_for_text_model(messages: list) -> list:
    """Project durable image history into deterministic text for an exact text-only model.

    上游 projectImagesForTextModel。
    @param messages - complete request history.
    @returns the original list without images, otherwise shallow message copies with stable placeholders.
    """
    if not any(content_has_image(message.get("content") or []) for message in messages or []):
        return messages
    result = []
    for message in messages:
        content = _replace_images_for_text_model(message.get("content") or [])
        if content is not (message.get("content") or []):
            replaced = dict(message)
            replaced["content"] = content
            result.append(replaced)
        else:
            result.append(message)
    return result


def _replace_files_with_handles(blocks: list, resolve_path) -> list:
    """Replace every file occurrence with handle text."""
    next_blocks = None
    for index, block in enumerate(blocks or []):
        if block.get("type") == "file":
            if next_blocks is None:
                next_blocks = list(blocks[:index])
            next_blocks.append({"type": "text", "text": file_handle_text(block.get("attachment") or {}, resolve_path(block.get("attachment") or {}))})
            continue
        if next_blocks is not None:
            next_blocks.append(block)
    return next_blocks if next_blocks is not None else list(blocks or [])


def project_files_to_text(messages: list, resolve_path) -> list:
    """Project durable file history into deterministic handle text for every model route.

    上游 projectFilesToText：无条件投影，file 永不原生 dispatch。
    """
    if not any(content_has_file(message.get("content") or []) for message in messages or []):
        return messages
    result = []
    for message in messages:
        content = _replace_files_with_handles(message.get("content") or [], resolve_path)
        if content is not (message.get("content") or []):
            replaced = dict(message)
            replaced["content"] = content
            result.append(replaced)
        else:
            result.append(message)
    return result


# ---------- requiredImageOffload ----------

def offloaded_image_prefix_count(
    lengths: list,
    budget: dict,
) -> int:
    """Number of oldest retained image occurrences one route budget removes.

    上游 offloadedImagePrefixCount（content.ts:277-299）。
    """
    total = sum(lengths)
    max_images = budget.get("maxImages")
    max_bytes = budget.get("maxBytes")
    excess_count = max(0, len(lengths) - max_images) if max_images is not None else 0
    excess_bytes = max(0, total - max_bytes) if max_bytes is not None else 0
    if excess_count == 0 and excess_bytes == 0:
        return 0
    count_quantum = budget.get("countQuantum") or 1
    byte_quantum = budget.get("byteQuantum") or 1
    remove_count = 0 if excess_count == 0 else ((excess_count + count_quantum - 1) // count_quantum) * count_quantum
    remove_bytes = 0 if excess_bytes == 0 else ((excess_bytes + byte_quantum - 1) // byte_quantum) * byte_quantum
    count = 0
    removed_bytes = 0
    for image_bytes in lengths:
        byte_target_met = remove_bytes == 0 or (byte_quantum == 1 and removed_bytes >= remove_bytes) or (byte_quantum > 1 and removed_bytes > remove_bytes)
        if count >= remove_count and byte_target_met:
            break
        removed_bytes += image_bytes
        count += 1
    return count


def required_image_offload(
    messages: list,
    budget: LlmImageRequestBudget,
    version_bytes,
) -> int:
    """How many more leading retained occurrences a route must offload before a derived request fits its budget.

    上游 requiredImageOffload（content.ts:311-325）。
    A route fails with IMAGE_OFFLOAD_REQUIRED carrying this count instead of offloading on its own.
    @param messages - derived request history carrying the surface's offloaded marks.
    @param budget - route representation, budgets, and removal quanta.
    @param version_bytes - exact request-version byte length of one retained occurrence.
    @returns how many more leading retained occurrences to offload.
    """
    representation = budget.representation
    lengths: list = []

    def visit_block(block: dict) -> None:
        # 上游 content.ts:326：已 offloaded 的 occurrence 不占预算（它已投影为
        # 占位文本），重复计量会多报所需 offload 数。
        if block.get("offloaded") is True:
            return
        bytes_val = version_bytes(block)
        if representation == "base64":
            bytes_val = base64_length(bytes_val)
        lengths.append(bytes_val)

    for message in messages:
        visit_image_blocks(message.get("content") or [], visit_block)

    budget_dict = {"maxImages": budget.maxImages, "maxBytes": budget.maxBytes,
                   "countQuantum": budget.countQuantum or 1, "byteQuantum": budget.byteQuantum or 1}
    return offloaded_image_prefix_count(lengths, budget_dict)


def visit_image_blocks(content: list, visit) -> None:
    """Visit every image occurrence of typed content in message order."""
    for block in content or []:
        if block.get("type") == "image":
            visit(block)


# ---------- 工具更新投影（上游 content.ts projectToolUpdates） ----------

def without_developer_messages(messages: list) -> list:
    """Remove every developer-role message（上游 withoutDeveloperMessages）。"""
    retained = [message for message in messages if message.get("role") != "developer"]
    return messages if len(retained) == len(messages) else retained


def _tool_declarations(tools: list | None, mode: str, history: dict) -> dict:
    """按路由模式构造 provider 声明表（上游 toolDeclarations）。"""
    declarations: dict = {tool.get("name"): tool for tool in history.get("tools") or []}
    for update in history.get("updates") or []:
        for tool in update.get("additions") or []:
            if tool.get("name") not in declarations:
                # 后续新增在各自记录位置激活这些定义。
                declarations[tool.get("name")] = {**tool, "deferLoading": True}

    if mode == "in-history":
        # removal 块停用工具但不丢弃其历史定义。
        return declarations
    if mode == "addition-only":
        # 无 removal 支持时，声明列表必须省略非活动工具。
        active = {tool.get("name") for tool in (tools or [])}
        for name in list(declarations.keys()):
            if name not in active:
                del declarations[name]
        return declarations
    raise ValueError(f"unknown tool update mode {mode!r}")


def project_tool_updates(
    messages: list,
    tools: list | None,
    tool_update: str | None,
    history: dict | None = None,
) -> dict:
    """按路由裁剪 developer 工具增删块与声明（上游 projectToolUpdates）。

    不支持的路由与不完整历史用「当前声明、无 developer 更新」。显式延后的
    baseline 工具在其首个保留的新增块之后才可用。
    @returns {"messages", "tools"}：投影后的请求输入与 provider 声明。
    """
    if tool_update is None:
        # 不支持的路由需要立即可用的工具，且不携带更新消息。
        immediate = tools
        if tools is not None and any(tool.get("deferLoading") is True for tool in tools):
            immediate = [{k: v for k, v in tool.items() if k != "deferLoading"} for tool in tools]
        return {"messages": without_developer_messages(messages), "tools": immediate}

    if history is None:
        # 仅有当前 schema 无法解析过去更新引用的定义。
        return {"messages": without_developer_messages(messages), "tools": tools}
    message_ids = {message.get("id") for message in messages if message.get("role") == "developer"}
    if any(update.get("messageId") not in message_ids for update in history.get("updates") or []):
        # 辅助前缀可能省略激活历史定义所需的更新。
        return {"messages": without_developer_messages(messages), "tools": tools}

    declarations = _tool_declarations(tools, tool_update, history)
    update_ids = {update.get("messageId") for update in history.get("updates") or []}
    # 延后的 baseline 工具仍需其首个新增块才可用。
    offered = {tool.get("name") for tool in history.get("tools") or [] if not tool.get("deferLoading")}
    projected: list = []
    for message in messages:
        if message.get("role") != "developer":
            projected.append(message)
            continue
        # 更早的声明系列不支配当前工具集。
        if message.get("id") not in update_ids:
            continue
        content: list = []
        for block in message.get("content") or []:
            btype = block.get("type")
            if btype == "tool-addition":
                # 只有已声明且尚未可用的工具需要激活。
                name = block.get("toolName")
                if name not in declarations or name in offered:
                    continue
                offered.add(name)
                content.append(block)
            elif btype == "tool-removal":
                # addition-only 路由无法经历史停用工具。
                if tool_update != "in-history":
                    continue
                if block.get("toolName") in offered:
                    offered.discard(block.get("toolName"))
                    content.append(block)
            else:
                # 其它核心/插件定义块保留内容与顺序。
                content.append(block)
        if not content:
            continue
        if len(content) == len(message.get("content") or []):
            projected.append(message)
        else:
            projected.append({**message, "content": content})

    unchanged = (len(projected) == len(messages)
                 and all(projected[index] is messages[index] for index in range(len(messages))))
    return {"messages": messages if unchanged else projected,
            "tools": list(declarations.values())}
