"""PTC 模式 `run_code` 工具（tools-presentation seam，L2）。

对应 dsh 真实源码：packages/core/tools/src/ptc.ts。

`ptc_runtime/`（L1）承载执行 seam + Python 后端；本层承载把注册表工具暴露给
模型的 `run_code` 工具与子派发事件日志（消费 core.tools 的管线，故为 L2）。
"""
from .run_code import (
    PYTHON_FLAVOR,
    RUN_CODE_DESCRIPTION_PARAM_DESCRIPTION,
    RUN_CODE_FLAVORS,
    RUN_CODE_NAME,
    TYPESCRIPT_FLAVOR,
    RunCodeFailedError,
    create_run_code_tool,
)

__all__ = [
    "PYTHON_FLAVOR",
    "RUN_CODE_DESCRIPTION_PARAM_DESCRIPTION",
    "RUN_CODE_FLAVORS",
    "RUN_CODE_NAME",
    "TYPESCRIPT_FLAVOR",
    "RunCodeFailedError",
    "create_run_code_tool",
]
