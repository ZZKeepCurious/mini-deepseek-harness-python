"""boot 族：上游 packages/boot（app-boot + cmdline）。

聚合器显式 __all__（架构文档 §4.3 规约）：只导出契约名，星号导入不复制
子模块同名属性；dotenv 一并再导出（parse_dotenv 归属本族，见 dotenv.py）。
"""
from .boot import *  # noqa: F401,F403
from .composition import *  # noqa: F401,F403
from .dotenv import *  # noqa: F401,F403

__all__ = [
    "apply_patch",
    "boot",
    "compose_with_origins",
    "evaluate_js_expr",
    "load_composition",
    "load_document",
    "load_dotenv_file",
    "load_plugin",
    "load_patch_list",
    "parse_dotenv",
    "render_composition_dump",
    "resolve_js_exprs",
]