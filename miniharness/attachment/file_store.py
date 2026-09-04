"""verbatim 文件的内容寻址本地存储（alpha.1 新增，文件附件能力）。

对应 dsh 真实源码：packages/attachment/attachment-local/src/file-store.ts。

与上游一致：
  * 文件字节原样存储（无规范化、无限额——任意字节内容与长度都接受）；
  * attachmentId = `sha256:<hex>`（原样字节摘要）；同对象去重；
  * 摘要命名目录 + 清洗显示名作叶名（`files/<sha256[:2]>/<sha256>/<name>`），
    模型与用户得到的路径以真实文件名结尾；规范对象本体在
    `file-objects/<sha256[:2]>/<sha256>`（每个摘要一份，所有显示名共享）；
  * 引用校验：attachmentId 形状 + name 必须恰好等于 file_leaf_name(name)
    （引用里的名字被污染即拒绝）；
  * 读取按块迭代并验证字节数与摘要；完整性失败在最后一个块之后抛出。

载体简化：上游为 async（createReadStream/Promise），mini 同步载体逐块
迭代（Iterable[bytes]/生成器）；发布持久化链复用 store.py 的
临时文件 fsync → link 原子发布 → 目录条目 fsync（Windows 跳过目录 fsync，
上游 win32 分支同款）。
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Iterator

from .error import (
    ATTACHMENT_CORRUPT,
    ATTACHMENT_NOT_FOUND,
    ATTACHMENT_READ_FAILED,
    ATTACHMENT_WRITE_FAILED,
    INVALID_ATTACHMENT_REF,
    AttachmentError,
)
from .store import (
    _ensure_durable_directory,
    _sync_directory,
)

__all__ = [
    "file_leaf_name",
    "read_file_stream_verbatim",
    "save_file_stream_verbatim",
    "save_file_verbatim",
    "stored_file_path",
]

_ID_PREFIX = "sha256:"
_FILE_ID_PATTERN = re.compile(r"^sha256:([a-f0-9]{64})$")
_WINDOWS_DEVICE_NAME = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.I)


def _is_windows_device_name(name: str) -> bool:
    dot = name.find(".")
    stem = re.sub(r"[. ]+$", "", name if dot < 0 else name[:dot])
    return _WINDOWS_DEVICE_NAME.match(stem) is not None


def _utf8_prefix(value: str, max_bytes: int) -> str:
    bytes_used = 0
    prefix = ""
    for ch in value:
        ch_bytes = len(ch.encode("utf-8"))
        if bytes_used + ch_bytes > max_bytes:
            break
        prefix += ch
        bytes_used += ch_bytes
    return prefix


def file_leaf_name(value: str | None) -> str:
    """把调用方显示名清洗为安全的存储叶名（上游 fileLeafName）。

    两种分隔符都手动剥离：POSIX 宿主把 `\\` 当普通字符，不剥离会把 Windows
    客户端的完整本地路径泄漏进引用与会话日志；Windows 文件名禁用字符换成
    `_`，让一个引用在所有受支持宿主上保持有效。
    """
    if value is None:
        return "file"
    leaf = value[max(value.rfind("/"), value.rfind("\\")) + 1:]
    clean = re.sub(r"[\x00-\x1f\x7f]", "", leaf)
    clean = re.sub(r'[<>:"|?*]', "_", clean).strip()
    clean = re.sub(r"[. ]+$", "", clean)
    if _is_windows_device_name(clean):
        clean = f"_{clean}"
    clean = re.sub(r"[. ]+$", "", _utf8_prefix(clean, 255))
    return clean if clean not in ("", ".", "..") else "file"


def _ensure_file_reference(ref) -> str:
    """校验文件引用形状并返回摘要 hex（上游 ensureFileReference）。"""
    match = _FILE_ID_PATTERN.match(str(ref.attachmentId))
    if match is None or ref.name != file_leaf_name(ref.name):
        raise AttachmentError("File attachment reference is invalid.",
                              INVALID_ATTACHMENT_REF)
    return match.group(1)


def stored_file_path(root: str, ref) -> str:
    """一个已存文件的绝对不可变对象路径（上游 storedFilePath）。"""
    sha256 = _ensure_file_reference(ref)
    return os.path.join(root, "files", sha256[:2], sha256, ref.name)


def _stored_file_object_path(root: str, sha256: str) -> str:
    """一个摘要的规范对象路径（所有显示名共享；上游 storedFileObjectPath）。"""
    return os.path.join(root, "file-objects", sha256[:2], sha256)


def _publish_object(root: str, object_path: str, write, sha256: str) -> None:
    """临时文件写入 → fsync → link 原子发布 → 去重路径复验摘要。"""
    bucket = os.path.dirname(object_path)
    staging = os.path.join(root, "tmp")
    home = os.path.dirname(os.path.dirname(os.path.abspath(root)))
    _ensure_durable_directory(bucket, home)
    _ensure_durable_directory(staging, home)
    temporary = os.path.join(staging, uuid.uuid4().hex)
    handle_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        handle_fd = os.open(temporary, flags, 0o600)
        with os.fdopen(handle_fd, "wb") as handle:
            handle_fd = None
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, object_path)
        except FileExistsError:
            # 已有对象按同摘要发布：复验既有字节（store.py 去重路径同款）
            digest = hashlib.sha256()
            with open(object_path, "rb") as existing:
                for chunk in iter(lambda: existing.read(1 << 16), b""):
                    digest.update(chunk)
            if digest.hexdigest() != sha256:
                raise AttachmentError(
                    "Stored attachment failed integrity verification.",
                    ATTACHMENT_CORRUPT,
                )
        _sync_directory(bucket)
        _sync_directory(os.path.dirname(bucket))
        os.unlink(temporary)
    except AttachmentError:
        raise
    except OSError as error:
        if handle_fd is not None:
            os.close(handle_fd)
            handle_fd = None
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise AttachmentError(
            "Unable to persist file attachment.", "ATTACHMENT_WRITE_FAILED"
        ) from error


def _publish_alias(root: str, object_path: str, alias_path: str,
                   sha256: str) -> None:
    """把规范对象硬链接别名到显示名路径（上游 publishImmutableAlias）。"""
    alias_bucket = os.path.dirname(alias_path)
    _ensure_durable_directory(alias_bucket, os.path.dirname(os.path.dirname(
        os.path.abspath(root))))
    try:
        os.link(object_path, alias_path)
    except FileExistsError:
        # 别名已存在：内容寻址保证同摘要同内容，无需重验
        pass
    _sync_directory(alias_bucket)


def save_file_verbatim(root: str, input_) -> "object":
    """一个文件字节原样提交到版本化附件根下（上游 saveFileVerbatim）。

    @returns 持久的内容寻址 FileAttachmentRef。
    """
    from .types import AttachmentId, FileAttachmentRef

    sha256 = hashlib.sha256(input_.data).hexdigest()
    ref = FileAttachmentRef(
        attachmentId=AttachmentId(f"{_ID_PREFIX}{sha256}"),
        name=file_leaf_name(input_.name),
        bytes=len(input_.data),
    )
    object_path = _stored_file_object_path(root, sha256)
    data = input_.data

    def write(handle):
        handle.write(data)

    _publish_object(root, object_path, write, sha256)
    _publish_alias(root, object_path, stored_file_path(root, ref), sha256)
    return ref


def save_file_stream_verbatim(root: str, input_) -> "object":
    """从有界字节块把一个文件字节原样提交（上游 saveFileStreamVerbatim）。

    data 迭代先落临时 spool 文件并增量摘要（不整文件驻留内存、实现施加
    背压），摘要齐备后再发布规范对象与显示名别名。取消信号（若携带
    .aborted 属性）在每个块边界检查。
    """
    from .types import AttachmentId, FileAttachmentRef

    name = file_leaf_name(input_.name)
    signal = getattr(input_, "signal", None)

    def _aborted() -> bool:
        return bool(getattr(signal, "aborted", False))

    if _aborted():
        raise AttachmentError("File upload was aborted.", ATTACHMENT_WRITE_FAILED)

    home = os.path.dirname(os.path.dirname(os.path.abspath(root)))
    staging = os.path.join(root, "tmp")
    _ensure_durable_directory(staging, home)
    spool = os.path.join(staging, uuid.uuid4().hex)
    spool_fd: int | None = None
    digest = hashlib.sha256()
    total = 0
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        spool_fd = os.open(spool, flags, 0o600)
        with os.fdopen(spool_fd, "wb") as handle:
            spool_fd = None
            for chunk in input_.data:
                if _aborted():
                    raise AttachmentError("File upload was aborted.",
                                          ATTACHMENT_WRITE_FAILED)
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise AttachmentError("File stream chunks must be bytes.",
                                          ATTACHMENT_WRITE_FAILED)
                digest.update(chunk)
                total += len(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        sha256 = digest.hexdigest()
        ref = FileAttachmentRef(
            attachmentId=AttachmentId(f"{_ID_PREFIX}{sha256}"),
            name=name,
            bytes=total,
        )
        object_path = _stored_file_object_path(root, sha256)

        def write(dest):
            with open(spool, "rb") as src:
                while True:
                    block = src.read(1 << 16)
                    if not block:
                        break
                    dest.write(block)

        _publish_object(root, object_path, write, sha256)
        _publish_alias(root, object_path, stored_file_path(root, ref), sha256)
        return ref
    except AttachmentError:
        raise
    except OSError as error:
        raise AttachmentError("Unable to persist file attachment.",
                              ATTACHMENT_WRITE_FAILED) from error
    finally:
        if spool_fd is not None:
            os.close(spool_fd)
        try:
            os.unlink(spool)
        except FileNotFoundError:
            pass


def read_file_stream_verbatim(root: str, ref,
                              chunk_size: int = 1 << 16) -> Iterator[bytes]:
    """按有界块读取一个已存文件并验证字节数与摘要（上游 readFileStreamVerbatim）。

    按序产出精确存储字节；完整性失败（EOF 后字节数或摘要不符）抛
    ATTACHMENT_CORRUPT。对象缺失抛 ATTACHMENT_NOT_FOUND。
    """
    sha256 = _ensure_file_reference(ref)
    path = stored_file_path(root, ref)
    digest = hashlib.sha256()
    total = 0
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
                yield chunk
    except FileNotFoundError as error:
        raise AttachmentError("File attachment object is missing.",
                              ATTACHMENT_NOT_FOUND) from error
    except OSError as error:
        raise AttachmentError("Unable to read file attachment.",
                              ATTACHMENT_READ_FAILED) from error
    if total != ref.bytes or digest.hexdigest() != sha256:
        raise AttachmentError(
            "Stored file attachment failed integrity verification.",
            ATTACHMENT_CORRUPT,
        )


def read_file_verbatim(root: str, ref) -> "object":
    """一次性读取并验证一个已存文件（上游流式读取的全量便捷形态）。"""
    chunks: list[bytes] = []
    for chunk in read_file_stream_verbatim(root, ref):
        chunks.append(chunk)
    return b"".join(chunks)
