"""通用 stdio LSP provider 的文件系统 seam 源访问。

对齐 packages/lsp/lsp-stdio/src/host.ts：经 `ctx.fs` 规范工作区、按 workspace 约束读取
查询源（完整文档字节上限由本层拥有；流式/常规文件校验/UTF-8 校验由 fs provider 拥有）。
"""
from __future__ import annotations

from typing import Any

from .abort import throw_if_aborted

__all__ = ["HostSource", "HostWorkspace", "canonicalize_workspace", "read_host_source"]

#: 一个在执行世界中被规范的工作区。
HostWorkspace = dict  # {target, canonical_path, file_url}
#: 一个已校验的源，以及发给语言服务器的确切 URI。
HostSource = dict  # {file_url, text}


def _message_of(error: Any) -> str:
    return str(error)


async def canonicalize_workspace(fs: Any, workspace_root: str, signal: Any = None) -> HostWorkspace:
    """经 `ctx.fs` 解析并校验一个工作区（对齐上游 canonicalizeWorkspace）。"""
    throw_if_aborted(signal)
    try:
        target = await fs.resolve(
            workspace_root, {"signal": signal} if signal is not None else {})
    except Exception as error:  # noqa: BLE001 - 收敛为域错误前先复查取消
        throw_if_aborted(signal)
        raise RuntimeError(
            f'workspace root "{workspace_root}" cannot be resolved: {_message_of(error)}'
        ) from error
    throw_if_aborted(signal)
    info = await fs.stat(target, signal)
    throw_if_aborted(signal)
    if info is None or info.type != "directory":
        raise RuntimeError(f'workspace root "{workspace_root}" is not a directory')
    return {
        "target": target,
        "canonical_path": fs.process_path(target),
        "file_url": fs.file_url(target),
    }


async def read_host_source(fs: Any, file_path: str, workspace: HostWorkspace,
                           max_document_bytes: int, signal: Any = None) -> HostSource:
    """经 `ctx.fs` 解析、约束并读取一个查询源（对齐上游 readHostSource）。"""
    throw_if_aborted(signal)
    opts: dict = {"cwd": workspace["canonical_path"]}
    if signal is not None:
        opts["signal"] = signal
    try:
        target = await fs.resolve(file_path, opts)
    except Exception as error:  # noqa: BLE001
        throw_if_aborted(signal)
        raise RuntimeError(
            f'source "{file_path}" cannot be resolved: {_message_of(error)}'
        ) from error
    throw_if_aborted(signal)
    if not fs.contains(workspace["target"], target):
        raise RuntimeError(f'source "{file_path}" resolves outside the workspace')
    chunks: list[str] = []
    total = 0
    stream = fs.stream_text(target, signal)
    try:
        async for chunk in stream:
            throw_if_aborted(signal)
            total += len(chunk.encode("utf-8"))
            if total > max_document_bytes:
                break
            chunks.append(chunk)
    except Exception as error:  # noqa: BLE001
        throw_if_aborted(signal)
        raise RuntimeError(
            f'source "{file_path}" could not be read: {_message_of(error)}'
        ) from error
    finally:
        # 提前 break 时 async for 不会自动关闭异步生成器（文件句柄会滞留到 GC）；
        # 显式 aclose，确保源流随本函数结束即释放。
        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                pass
    if total > max_document_bytes:
        raise RuntimeError(
            f'source "{file_path}" exceeds the {max_document_bytes}-byte limit; '
            f"reading stopped after {total} bytes")
    throw_if_aborted(signal)
    return {"file_url": fs.file_url(target), "text": "".join(chunks)}
