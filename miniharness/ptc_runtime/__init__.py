"""PTC（Programmatic Tool Calling）运行时族。

对应 dsh 真实源码：packages/ptc-runtime/{ptc-runtime,ptc-runtime-node} +
packages/experimental/ptc-runtime-python。

  * `types.py` —— seam 词汇类型（PtcRunRequest/Spec/Result/Failure + 绑定契约）；
  * `service.py` —— PtcRuntime Service Definition + 保留名常量与绑定校验；
  * `runtime.py` —— `PythonPtcRuntime`：CPython 子进程执行模型 Python，绑定经
    行 JSON 管道桥接，预算/中止/输出上限与正交失败分类。

载体差异（sharp→Pillow 类先例）：上游 Node 后端用 worker/subprocess + fd-3 wire；
mini 用 CPython 子进程 + stdin/stdout 行 JSON 协议。子进程非安全边界（上游同款声明）。
"""
from .service import (
    DUNDER_MEMBER,
    PORTABLE_RESERVED_WORDS,
    RESERVED_BINDING_GLOBALS,
    RESERVED_ERROR_MEMBERS,
    PtcRuntime,
    is_portable_identifier,
    validate_binding_namespaces,
)
from .types import (
    PtcBindingErrorClass,
    PtcBindingFunction,
    PtcBindingNamespace,
    PtcJsonValue,
    PtcRunFailure,
    PtcRunRequest,
    PtcRunResult,
    PtcRunSandbox,
    PtcRunSpec,
)
from .runtime import (
    DEFAULT_MAX_LOG_BYTES,
    DEFAULT_MAX_TIMEOUT_MS,
    DEFAULT_TIMEOUT_MS,
    MIN_LOG_MARKER_BYTES,
    PythonPtcRuntime,
    install_ptc_runtime,
)

__all__ = [
    "DEFAULT_MAX_LOG_BYTES",
    "DEFAULT_MAX_TIMEOUT_MS",
    "DEFAULT_TIMEOUT_MS",
    "DUNDER_MEMBER",
    "MIN_LOG_MARKER_BYTES",
    "PORTABLE_RESERVED_WORDS",
    "RESERVED_BINDING_GLOBALS",
    "RESERVED_ERROR_MEMBERS",
    "PtcBindingErrorClass",
    "PtcBindingFunction",
    "PtcBindingNamespace",
    "PtcJsonValue",
    "PtcRunFailure",
    "PtcRunRequest",
    "PtcRunResult",
    "PtcRunSandbox",
    "PtcRunSpec",
    "PtcRuntime",
    "PythonPtcRuntime",
    "install_ptc_runtime",
    "is_portable_identifier",
    "validate_binding_namespaces",
]
