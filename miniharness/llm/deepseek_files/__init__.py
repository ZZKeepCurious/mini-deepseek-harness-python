"""DeepSeek 请求侧图片/文件基础设施（上游 packages/llm/llm-deepseek/src/common）。

承载 `dsh-v0.1.6-alpha.1` A 组 #1-3 的 Files API 执行簇：

  * file_id —— Files API 标识与命名空间品牌类型；
  * defaults / models / types / model_info —— provider 限额、缺省目录与能力解析；
  * image_tokens / request_pricing —— provider vision-token 与请求图定价；
  * files_api —— Files API 传输（Messages 协议 /v1/files）；
  * upload_index —— durable attachment→file-id 索引（filelock + 原子写）；
  * file_store —— 上传复用、失效与配额恢复；
  * request_files —— 请求级解析、stale-id 恢复与规范化图片诊断。

载体差异：httpx 异步传输替代 fetch/FormData；filelock + os.replace 替代
dsh-atomic-write（见各模块 docstring）。
"""
from .defaults import *  # noqa: F401,F403
from .file_id import *  # noqa: F401,F403
from .file_store import *  # noqa: F401,F403
from .files_api import *  # noqa: F401,F403
from .image_tokens import *  # noqa: F401,F403
from .model_info import *  # noqa: F401,F403
from .models import *  # noqa: F401,F403
from .request_files import *  # noqa: F401,F403
from .request_pricing import *  # noqa: F401,F403
from .types import *  # noqa: F401,F403
from .upload_index import *  # noqa: F401,F403

__all__ = [
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_FILES_API_TIMEOUT_MS",
    "DEFAULT_FILE_EXPIRY_SECONDS",
    "DEFAULT_FILE_QUOTA_CLEANUP_BATCH",
    "DEFAULT_FILE_REFRESH_MARGIN_SECONDS",
    "DEFAULT_IMAGE_OFFLOAD_BYTE_QUANTUM",
    "DEFAULT_IMAGE_OFFLOAD_COUNT_QUANTUM",
    "DEFAULT_INLINE_IMAGE_OFFLOAD_BYTE_QUANTUM",
    "DEFAULT_LOW_DETAIL_IMAGE_PIXEL_BUDGET",
    "DEFAULT_MAX_IMAGES_PER_REQUEST",
    "DEFAULT_MAX_INLINE_REQUEST_IMAGE_BYTES",
    "DEFAULT_MAX_REQUEST_FILES_BYTES",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODELS",
    "DEFAULT_REQUEST_IMAGE_MAX_BYTES",
    "DEFAULT_STREAM_IDLE_TIMEOUT_MS",
    "MAX_FILE_EXPIRY_SECONDS",
    "MAX_FILE_UPLOAD_BYTES",
    "MAX_IMAGE_BYTES",
    "MAX_STORED_FILE_BYTES",
    "MAX_STORED_FILE_COUNT",
    "MESSAGES_FILES_BETA",
    "MIN_FILE_EXPIRY_SECONDS",
    "REQUEST_IMAGE_MAX_DIMENSION",
    "REASONING_EFFORTS",
    "DeepSeekCatalogModel",
    "DeepSeekConnectionOptions",
    "DeepSeekFileConnection",
    "DeepSeekFileId",
    "DeepSeekFileObject",
    "DeepSeekFilePage",
    "DeepSeekFilePolicy",
    "DeepSeekFileReference",
    "DeepSeekFileScope",
    "DeepSeekFileStore",
    "DeepSeekFilesClient",
    "DeepSeekFilesError",
    "DeepSeekUploadIndex",
    "DeepSeekUploadRecord",
    "FileResolutionFailure",
    "ImageWireLocation",
    "RequestDefaults",
    "RequestFiles",
    "UploadIndexCommit",
    "catalog_model_info",
    "deep_seek_file_scope",
    "deep_seek_image_request_pricing",
    "deep_seek_image_tokens",
    "deep_seek_request_image_dimensions",
    "detail_names_file_id",
    "is_files_quota_error",
    "model_info",
    "normalized_image_diagnostic",
    "normalized_image_facts",
    "parse_file_object",
    "parse_messages_file",
    "provider_error_detail",
    "provider_rejected_file_id",
    "provider_rejected_normalized_image",
    "resolve_request_image_max_bytes",
    "resolve_request_image_target",
    "stale_mappings",
]
