"""本地附件存储：内容寻址（规范化字节 sha256）不可变对象存储。

对应 dsh 真实源码：packages/attachment/attachment/src/index.ts（AttachmentStore
seam，rc.2 已核实）+ packages/attachment/attachment-local/src/{store.ts,
index.ts}（LocalAttachmentStore 实现，rc.2 已核实）。

与上游一致：
  * save_images：先批量校验（张数 / 总字节 / 类型白名单），再逐张完整校验
    （含规范化证明——全部成员可受理的批次不会在发布阶段才被规范化字节上限
    拒绝），最后按序提交；校验失败不启动任何写入；
  * 存储是内容寻址：规范化字节的 sha256 即 attachmentId（`sha256:<hex>`），
    同对象去重；引用可携带 originalDimensions（源被规范化缩小时）；
  * read_image：按引用读取 + 摘要验证 + 头部字段复验（读取路径不做全量栅格
    解码，历史重放无逐请求像素放大）；
  * read_image_request：seam 缺省拒绝（ATTACHMENT_PROJECTION_UNSUPPORTED），
    本地后端以 variantId 确定身份生成/复用请求图版本；
  * 发布持久化链：临时文件 fsync → link 原子发布 → 目录条目 fsync；EEXIST
    去重路径复验既有对象摘要。Windows 无目录句柄，目录 fsync 跳过（NTFS
    元数据日志负责条目持久性，上游 win32 分支同款）。

载体简化：
  * 上游 CompressionLimiter FIFO 并发闸与 SharedRequest 单飞：mini 全链路
    同步（无并发交错窗口），执行天然串行已满足"同时至多 N 个原生变换"的
    上界语义——登记为架构不适用（verified-diffs §3.9），非缺功能；
  * ensureDurableHome 进程级记忆简化为每次发布独立走持久化目录链（幂等）。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import uuid

from .error import (
    ATTACHMENT_CORRUPT,
    ATTACHMENT_FILES_UNSUPPORTED,
    ATTACHMENT_NOT_FOUND,
    ATTACHMENT_PROJECTION_UNSUPPORTED,
    ATTACHMENT_READ_FAILED,
    ATTACHMENT_WRITE_FAILED,
    IMAGE_TOO_LARGE,
    IMAGES_TOO_LARGE,
    INVALID_ATTACHMENT_REF,
    INVALID_IMAGE,
    IMAGE_TYPE_MISMATCH,
    TOO_MANY_IMAGES,
    UNSUPPORTED_IMAGE_TYPE,
    AttachmentError,
)
from .image import detect_image, probe_image
from .normalization import NormalizationPolicy, normalize_image
from .request_image import read_request_image_file, request_image_variant_id
from .types import (
    DEFAULT_MAX_IMAGE_BYTES,
    DEFAULT_MAX_IMAGE_DIMENSION,
    DEFAULT_MAX_IMAGE_PIXELS,
    DEFAULT_MAX_IMAGES_PER_MESSAGE,
    DEFAULT_MAX_MESSAGE_IMAGE_BYTES,
    AttachmentId,
    Dimensions,
    EncodedFileAttachment,
    FileAttachmentRef,
    ImageAttachmentLimits,
    ImageAttachmentRef,
    ImageRequestPolicy,
    RequestImageAttachment,
    SaveFileAttachment,
    SaveFileStreamAttachment,
    SaveImageAttachment,
    StoredImageAttachment,
)

__all__ = [
    "DEFAULT_IMAGE_LIMITS",
    "DEFAULT_NORMALIZED_IMAGE_MAX_BYTES",
    "DEFAULT_NORMALIZED_IMAGE_MAX_DIMENSION",
    "DEFAULT_NORMALIZED_IMAGE_MAX_PIXELS",
    "AttachmentStore",
    "LocalAttachmentStore",
    "StoredImageAttachment",
    "commit_prepared_image_file",
    "normalized_image_path",
    "prepare_image_file",
    "read_image_file",
    "save_image_file",
    "validate_image_file",
]

#: 持久规范化图片的总像素预算（大源被收编而非拒绝；极端纵横比保住短边分辨率，
#: 上游 DEFAULT_NORMALIZED_IMAGE_MAX_PIXELS，alpha.1 新增）
DEFAULT_NORMALIZED_IMAGE_MAX_PIXELS = 2048 * 2048
#: 持久规范化图片的长边封顶（总像素预算之后应用，上游
#: DEFAULT_NORMALIZED_IMAGE_MAX_DIMENSION，alpha.1 由 2048 极长边规则改为 8192 封顶）
DEFAULT_NORMALIZED_IMAGE_MAX_DIMENSION = 8192
#: 单张持久规范化图片的编码字节目标（上游 DEFAULT_NORMALIZED_IMAGE_MAX_BYTES；
#: 阶梯每个质量都超限时保留最小阶梯输出，不再是独立拒绝上限）
DEFAULT_NORMALIZED_IMAGE_MAX_BYTES = 4 * 1024 * 1024

DEFAULT_IMAGE_LIMITS = ImageAttachmentLimits()

_ID_PREFIX = "sha256:"


def normalized_image_path(root: str, ref: ImageAttachmentRef) -> str:
    """一个规范化附件在宿主文件系统下的绝对不可变对象路径（上游
    normalizedImagePath；alpha.1 新增，把宿主位置与模型访问路径分离）。"""
    sha256 = _ensure_reference(ref)
    return os.path.join(root, "objects", sha256[:2], sha256)


def _display_name(value: str | None) -> str | None:
    """清洗显示名：去掉双分隔符的本地路径成分 + 控制字符 + 超长截断。

    对齐上游 displayName（store.ts）：POSIX 宿主把 `\\` 当普通字符，不
    手动剥离会让 Windows 客户端完整本地路径泄漏进引用与会话日志。
    """
    if value is None:
        return None
    leaf = value[max(value.rfind("/"), value.rfind("\\")) + 1:]
    clean = "".join(ch for ch in leaf if not (0 <= ord(ch) < 0x20 or ord(ch) == 0x7F))
    clean = clean.strip()[:255]
    return clean if clean else None


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ensure_reference(ref: ImageAttachmentRef) -> str:
    value = str(ref.attachmentId)
    if not value.startswith(_ID_PREFIX) or len(value) != len(_ID_PREFIX) + 64:
        raise AttachmentError("Attachment reference is invalid.", INVALID_ATTACHMENT_REF)
    hexpart = value[len(_ID_PREFIX):]
    if any(c not in "0123456789abcdef" for c in hexpart):
        raise AttachmentError("Attachment reference is invalid.", INVALID_ATTACHMENT_REF)
    return hexpart


def _inspect_metadata(
    data: bytes,
    declared_media_type: str,
    limits: ImageAttachmentLimits,
):
    if len(data) == 0:
        raise AttachmentError("Image is empty.", INVALID_IMAGE)
    detected = detect_image(
        data, max_pixels=limits.maxImagePixels, max_dimension=limits.maxImageDimension
    )
    if detected.media_type != declared_media_type:
        raise AttachmentError(
            "Declared image type does not match its bytes.", IMAGE_TYPE_MISMATCH
        )
    return detected


def prepare_image_file(
    input_: SaveImageAttachment,
    limits: ImageAttachmentLimits,
    policy: NormalizationPolicy,
) -> tuple[bytes, ImageAttachmentRef]:
    """解码、规范化并验证一个提交图片，不触碰存储（上游 prepareImageFile）。

    @returns 规范化字节与其不可变引用事实（原子发布的就绪形态）。"""
    if len(input_.data) > limits.maxImageBytes:
        raise AttachmentError("Image exceeds the configured byte limit.", IMAGE_TOO_LARGE)
    detected = _inspect_metadata(input_.data, input_.mediaType, limits)
    normalized = normalize_image(input_.data, detected, policy)
    sha256 = _digest(normalized.data)
    name = _display_name(input_.name)
    downscaled = (
        detected.width != normalized.width or detected.height != normalized.height
    )
    ref = ImageAttachmentRef(
        attachmentId=AttachmentId(f"{_ID_PREFIX}{sha256}"),
        mediaType=normalized.mediaType,
        width=normalized.width,
        height=normalized.height,
        bytes=len(normalized.data),
        **({"name": name} if name is not None else {}),
        **(
            {
                "originalDimensions": Dimensions(
                    width=detected.width, height=detected.height
                )
            }
            if downscaled
            else {}
        ),
    )
    return normalized.data, ref


def validate_image_file(
    input_: SaveImageAttachment,
    limits: ImageAttachmentLimits,
    policy: NormalizationPolicy,
) -> None:
    """不触碰存储地跑完一个图片的完整受理策略（含规范化证明）。"""
    prepare_image_file(input_, limits, policy)


def _sync_directory(path: str) -> None:
    """让目录条目持久（只读句柄 fsync）。Windows 无法打开目录句柄——NTFS
    元数据日志负责条目持久性，跳过（上游 syncDirectory win32 分支同款）。"""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_durable_directory(path: str, boundary: str) -> None:
    """创建私有目录树并把边界之上的每级祖先条目刷持久。"已存在"≠"已持久"
    （并发首存者可能尚未 fsync 其父目录）；重复 fsync 无害。"""
    target = os.path.abspath(path)
    stop = os.path.abspath(boundary)
    os.makedirs(target, mode=0o700, exist_ok=True)
    level = target
    while level != stop:
        parent = os.path.dirname(level)
        _sync_directory(parent)
        if parent == level:
            return
        level = parent


def commit_prepared_image_file(
    root: str, prepared: tuple[bytes, ImageAttachmentRef]
) -> ImageAttachmentRef:
    """把一个已验证的规范化图片发布到版本化附件根下（上游同名函数）。"""
    normalized, ref = prepared
    sha256 = _ensure_reference(ref)
    if _digest(normalized) != sha256 or len(normalized) != ref.bytes:
        raise AttachmentError(
            "Prepared attachment bytes do not match their reference.",
            ATTACHMENT_CORRUPT,
        )
    bucket = os.path.join(root, "objects", sha256[:2])
    staging = os.path.join(root, "tmp")
    home = os.path.dirname(os.path.dirname(os.path.abspath(root)))
    _ensure_durable_directory(bucket, home)
    _ensure_durable_directory(staging, home)
    temporary = os.path.join(staging, uuid.uuid4().hex)
    target = normalized_image_path(root, ref)
    handle_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        handle_fd = os.open(temporary, flags, 0o600)
        with os.fdopen(handle_fd, "wb") as handle:
            handle_fd = None
            handle.write(normalized)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            with open(target, "rb") as existing:
                if _digest(existing.read()) != sha256:
                    raise AttachmentError(
                        "Stored attachment failed integrity verification.",
                        ATTACHMENT_CORRUPT,
                    )
        # 目标条目刷持久 + 关闭并发建桶窗口，引用才允许到达会话检查点；
        # 去重路径同样重复两次 fsync（可能先于对方到达其持久化边界观察到
        # 对方的 link）
        _sync_directory(bucket)
        _sync_directory(os.path.join(root, "objects"))
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
            "Unable to persist image attachment.", ATTACHMENT_WRITE_FAILED
        ) from error
    return ref


def save_image_file(
    root: str,
    input_: SaveImageAttachment,
    limits: ImageAttachmentLimits,
    policy: NormalizationPolicy,
) -> ImageAttachmentRef:
    """一次解码规范化 + 发布（上游 saveImageFile）。"""
    return commit_prepared_image_file(root, prepare_image_file(input_, limits, policy))


def read_image_file(root: str, ref: ImageAttachmentRef) -> StoredImageAttachment:
    """按引用读取并验证一个内容寻址图片（上游 readImageFile）。"""
    sha256 = _ensure_reference(ref)
    path = normalized_image_path(root, ref)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError as error:
        raise AttachmentError(
            "Attachment object is missing.", ATTACHMENT_NOT_FOUND
        ) from error
    except OSError as error:
        raise AttachmentError(
            "Unable to read image attachment.", ATTACHMENT_READ_FAILED
        ) from error
    if _digest(data) != sha256:
        raise AttachmentError(
            "Stored attachment failed integrity verification.", ATTACHMENT_CORRUPT
        )
    # 摘要证明了这些正是受理时完整解码过的字节：读取路径只重导头部字段
    try:
        metadata = probe_image(data)
    except AttachmentError as error:
        if error.code == INVALID_IMAGE:
            raise AttachmentError(
                "Stored attachment metadata does not match its reference.",
                ATTACHMENT_CORRUPT,
            ) from error
        raise
    if (
        metadata.media_type != ref.mediaType
        or len(data) != ref.bytes
        or metadata.width != ref.width
        or metadata.height != ref.height
    ):
        raise AttachmentError(
            "Stored attachment metadata does not match its reference.",
            ATTACHMENT_CORRUPT,
        )
    return StoredImageAttachment(ref=ref, data=data)


class AttachmentStore:
    """不可变二进制附件服务（上游 AttachmentStore seam 的 mini 载体）。

    以鸭子类型承载（上游为 cordis Service）；实现提供 image_limits 属性与
    save_images / save_image / read_image / read_image_request；alpha.1 起
    另有 verbatim 文件族 save_file / save_file_stream / read_file_stream /
    file_host_path（不支持的后端保留基类拒绝）与 admit_prompt_content /
    admit_encoded_file / is_attachment_error。mini 保持与上游同名方法。
    """

    image_limits: ImageAttachmentLimits = DEFAULT_IMAGE_LIMITS

    def save_image(self, input_: SaveImageAttachment) -> ImageAttachmentRef:
        raise NotImplementedError

    def read_image(self, ref: ImageAttachmentRef) -> StoredImageAttachment:
        raise NotImplementedError

    def read_image_request(
        self, ref: ImageAttachmentRef, policy: ImageRequestPolicy
    ) -> RequestImageAttachment:
        """生成或复用一个确定性模型请求图版本；挂载的后端不能派生时拒绝。"""
        raise AttachmentError(
            "The mounted attachment provider cannot derive model-request images.",
            ATTACHMENT_PROJECTION_UNSUPPORTED,
        )

    # ---------- prompt 内容受理（上游 admitPromptContent，alpha.1 移入 seam） ----------

    def admit_prompt_content(self, content: list) -> list:
        """受理一个 Host prompt，把每个上传图片替换为其持久引用。

        text 与持久 file 引用（`{'type':'file','attachment':FileAttachmentRef}`
        形态）原样按序穿透；无 image 部分的 prompt 不发生任何存储操作。
        上游签名收 `AttachmentAdmissionPart[]`（file 部分已经
        resolvePromptFileReceipts 解析过 receipt）。
        """
        from .admission import admit_encoded_images

        if all(part.get("type") != "image" for part in content
               if isinstance(part, dict)):
            return [dict(part) for part in content]
        images = [part for part in content
                  if isinstance(part, dict) and part.get("type") == "image"]
        refs = admit_encoded_images(self, [
            EncodedImageAttachment(mediaType=p["mediaType"], data=p["data"],
                                   **({"name": p["name"]} if p.get("name") is not None else {}))
            for p in images
        ])
        out: list = []
        next_ref = 0
        for part in content:
            ptype = part.get("type") if isinstance(part, dict) else None
            if ptype == "image":
                out.append({"type": "image", "attachment": refs[next_ref].to_dict()})
                next_ref += 1
            else:
                out.append(dict(part))
        return out

    def admit_encoded_file(self, input_: "EncodedFileAttachment") -> "FileAttachmentRef":
        """解码并 verbatim 持久化一个 canonical base64 文件上传（上游同款）。"""
        from .admission import admit_encoded_file

        return admit_encoded_file(self, input_)

    def is_attachment_error(self, error: object) -> bool:
        """按稳定 code 判定失败是否属于本附件能力（上游 isAttachmentError）。"""
        from .error import is_attachment_error

        return is_attachment_error(error)

    # ---------- verbatim 文件族（不支持的后端保留基类拒绝，上游同款） ----------

    def save_file(self, input_: "SaveFileAttachment") -> "FileAttachmentRef":
        """verbatim 持久化一个文件；挂载的后端不支持时拒绝。"""
        raise AttachmentError(
            "The mounted attachment provider cannot store verbatim files.",
            ATTACHMENT_FILES_UNSUPPORTED,
        )

    def save_file_stream(self, input_: "SaveFileStreamAttachment") -> "FileAttachmentRef":
        """从有界字节块 verbatim 持久化一个文件；不支持的后端拒绝。"""
        raise AttachmentError(
            "The mounted attachment provider cannot stream verbatim files.",
            ATTACHMENT_FILES_UNSUPPORTED,
        )

    def read_file_stream(self, ref: "FileAttachmentRef"):
        """按有界块读取并验证一个 verbatim 文件；不支持的后端拒绝。"""
        raise AttachmentError(
            "The mounted attachment provider cannot read verbatim files.",
            ATTACHMENT_FILES_UNSUPPORTED,
        )
        yield  # pragma: no cover - 生成器标记，使函数体惰性求值

    def file_host_path(self, ref: "FileAttachmentRef") -> str | None:
        """verbatim 文件对象在宿主文件系统中的绝对路径；非宿主文件后端 None。"""
        return None

    def _validate_image_batch(self, inputs: list[SaveImageAttachment]) -> None:
        """批量门：任何成员提交前的共享入口（上游 validateImageBatch）。"""
        limits = self.image_limits
        if len(inputs) > limits.maxImagesPerMessage:
            raise AttachmentError(
                "Image batch exceeds the configured image-count limit.", TOO_MANY_IMAGES
            )
        total = sum(len(i.data) for i in inputs)
        if total > limits.maxMessageImageBytes:
            raise AttachmentError(
                "Image batch exceeds the configured aggregate image-byte limit.",
                IMAGES_TOO_LARGE,
            )
        for input_ in inputs:
            if input_.mediaType not in limits.mediaTypes:
                raise AttachmentError(
                    f"Image type {input_.mediaType} is not accepted by this deployment.",
                    UNSUPPORTED_IMAGE_TYPE,
                )

    def save_images(self, inputs: list[SaveImageAttachment]) -> list[ImageAttachmentRef]:
        """批量受理：批量门 → 逐张完整校验（含规范化证明）→ 按序提交。"""
        self._validate_image_batch(inputs)
        for input_ in inputs:
            self.validate_image(input_)
        refs: list[ImageAttachmentRef] = []
        for input_ in inputs:
            refs.append(self.save_image(input_))
        return refs


class LocalAttachmentStore(AttachmentStore):
    """内容寻址本地附件存储（对齐 attachment-local 的 LocalAttachmentStore）。

    @param root: 存储根目录（显式传入；上游为 DSH_HOME/attachments/v1）。
    @param limits: 部署图片策略，缺省为 DEFAULT_IMAGE_LIMITS。
    @param normalization_policy: provider 无关规范化策略
        （上游缺省 maxPixels=2048² / maxDimension=8192 / maxBytes=4MiB）。
    @param image_compression_concurrency: 实例并发上限配置位（1..8，越界
        fail loud 措辞逐字；同步载体下执行天然串行，见模块 docstring）。
    """

    def __init__(
        self,
        root: str | None = None,
        limits: ImageAttachmentLimits | None = None,
        normalization_policy: NormalizationPolicy | None = None,
        image_compression_concurrency: int = 2,
    ):
        self.image_limits = limits or DEFAULT_IMAGE_LIMITS
        self.normalization_policy = normalization_policy or NormalizationPolicy(
            maxPixels=DEFAULT_NORMALIZED_IMAGE_MAX_PIXELS,
            maxDimension=DEFAULT_NORMALIZED_IMAGE_MAX_DIMENSION,
            maxBytes=DEFAULT_NORMALIZED_IMAGE_MAX_BYTES,
        )
        concurrency = image_compression_concurrency
        if (
            not isinstance(concurrency, int)
            or isinstance(concurrency, bool)
            or concurrency < 1
            or concurrency > 8
        ):
            raise ValueError(
                "attachment-local: imageCompressionConcurrency must be an integer from 1 through 8"
            )
        self.image_compression_concurrency = concurrency
        self.root = root if root is not None else tempfile.mkdtemp(prefix="mini-attachment-")

    def validate_image(self, input_: SaveImageAttachment) -> None:
        validate_image_file(input_, self.image_limits, self.normalization_policy)

    def save_image(self, input_: SaveImageAttachment) -> ImageAttachmentRef:
        return save_image_file(self.root, input_, self.image_limits, self.normalization_policy)

    def read_image(self, ref: ImageAttachmentRef) -> StoredImageAttachment:
        return read_image_file(self.root, ref)

    def read_image_request(
        self, ref: ImageAttachmentRef, policy: ImageRequestPolicy
    ) -> RequestImageAttachment:
        stored = self.read_image(ref)
        return read_request_image_file(self.root, stored, policy)

    # ---------- verbatim 文件族（attachment-local file-store，alpha.1） ----------
    # file_store → store 单向依赖（上游同向：file-store.ts import store.ts），
    # 本侧方法体内延迟导入避免模块环。

    def save_file(self, input_: SaveFileAttachment) -> FileAttachmentRef:
        from .file_store import save_file_verbatim

        return save_file_verbatim(self.root, input_)

    def save_file_stream(self, input_: SaveFileStreamAttachment) -> FileAttachmentRef:
        from .file_store import save_file_stream_verbatim

        return save_file_stream_verbatim(self.root, input_)

    def read_file_stream(self, ref: FileAttachmentRef):
        from .file_store import read_file_stream_verbatim

        return read_file_stream_verbatim(self.root, ref)

    def file_host_path(self, ref: FileAttachmentRef) -> str | None:
        from .file_store import stored_file_path

        return stored_file_path(self.root, ref)

    def variant_id_for(
        self, ref: ImageAttachmentRef, policy: ImageRequestPolicy
    ) -> str:
        """不读字节的请求版本身份（缓存键/上传索引键预览）。"""
        return str(request_image_variant_id(ref, policy))
