"""`lsp` 工具的纯格式化与坐标转换：一基↔零基 UTF-16 光标转换、按文件分组的位置渲染
（`file:` URI 解析）、完整结果封顶、UI 呈现。无 I/O——UI 可在实时流与重放上调用
presenter，故只依赖工具参数。对齐 packages/lsp/tool-lsp/src/render.ts。
"""
from __future__ import annotations

import ntpath
import posixpath
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from ..lsp import LSP_OPERATIONS

__all__ = [
    "DEFAULT_MAX_LOCATIONS",
    "DEFAULT_MAX_RESULT_CHARS",
    "LSP_OPERATIONS",
    "format_hover",
    "format_locations",
    "parse_lsp_args",
    "present_lsp_call",
    "render_uri",
]

#: 渲染位置的缺省上限（超出追加省略标记）。
DEFAULT_MAX_LOCATIONS = 100
#: 完整渲染结果的缺省字符上限（含截断元数据）。
DEFAULT_MAX_RESULT_CHARS = 16_000

_ELLIPSIS = "\u2026"
_DRIVE_PATH = re.compile(r"^/[a-z](?::|%3A)", re.IGNORECASE)


def parse_lsp_args(args: dict) -> dict:
    """校验并转换模型参数：`operation` 属四者之一；`line`/`character` 是正的一基整数，
    转成 seam 的零基位置。
    """
    operation = args.get("operation")
    if operation not in LSP_OPERATIONS:
        raise ValueError(f"operation must be one of {', '.join(LSP_OPERATIONS)}")
    file_path = args.get("file_path")
    if not isinstance(file_path, str) or file_path.strip() == "":
        raise ValueError("file_path must be a non-empty string")
    line = _one_based(args.get("line"), "line")
    character = _one_based(args.get("character"), "character")
    return {
        "operation": operation,
        "filePath": file_path,
        # 模型从 1 计；seam（及协议）从 0 计。
        "position": {"line": line - 1, "character": character - 1},
    }


def _one_based(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer (one-based)")
    return value


def format_locations(locations: list[dict], workspace_uri: str, max_locations: int,
                     max_result_chars: int) -> str:
    """按文件分组渲染 locations，把零基位置转回一基 `path:line:character` 条目。

    `file:` URI 在工作区内 → 工作区相对路径；其外 → URI 派生的绝对路径；非 `file:` URI
    原样保留。先应用 `max_locations` 并在按数截断时追加省略标记，再应用完整结果上限。
    """
    if not locations:
        return _bound_result("No results.", max_result_chars, "locations")
    shown = locations[:max_locations]
    omitted = len(locations) - len(shown)
    grouped: dict[str, list[str]] = {}
    for location in shown:
        path = render_uri(location["uri"], workspace_uri)
        line = location["range"]["start"]["line"] + 1
        character = location["range"]["start"]["character"] + 1
        grouped.setdefault(path, []).append(f"{path}:{line}:{character}")
    lines: list[str] = []
    for entries in grouped.values():
        lines.extend(entries)
    if omitted > 0:
        plural = "" if omitted == 1 else "s"
        lines.append(f"{_ELLIPSIS} {omitted} more location{plural} omitted "
                     f"(limit {max_locations}).")
    return _bound_result("\n".join(lines), max_result_chars, "locations")


def format_hover(hover: dict | None, max_result_chars: int) -> str:
    """渲染 hover 结果，最后应用 `max_result_chars` 且标记落在上限内。"""
    text = "No hover information." if hover is None else hover["contents"]
    return _bound_result(text, max_result_chars, "hover")


def _bound_result(text: str, max_chars: int, label: str) -> str:
    """封顶一份完整渲染结果（含截断通知本身）。"""
    if len(text) <= max_chars:
        return text
    notice = f"\n{_ELLIPSIS} {label} truncated (limit {max_chars} characters)."
    if len(notice) >= max_chars:
        return notice[:max_chars]
    return f"{text[:max_chars - len(notice)]}{notice}"


def render_uri(uri: str, workspace_uri: str) -> str:
    """不套用宿主路径规则地解析一个位置 URI（对齐上游 renderUri）。

    合法 `file:` URI 若在 provider 的规范工作区 URI 下 → 工作区相对；否则 → URI 派生的
    绝对路径；畸形与非 `file:` URI 原样保留。
    """
    if not uri.startswith("file:"):
        return uri
    try:
        target = urlsplit(uri)
        workspace = urlsplit(workspace_uri)
    except ValueError:
        return uri
    if workspace.scheme != "file":
        return uri
    windows_world = bool(workspace.netloc) or bool(_DRIVE_PATH.match(workspace.path))
    target_windows = windows_world and (
        bool(target.netloc) or bool(_DRIVE_PATH.match(target.path)))
    workspace_path = _file_path(workspace, windows_world)
    target_path = _file_path(target, target_windows)
    if workspace_path is None or target_path is None:
        return uri
    if windows_world != target_windows:
        return target_path
    pathmod = ntpath if windows_world else posixpath
    relative = pathmod.relpath(target_path, workspace_path)
    if relative == ".":
        rendered = "."
    elif _is_outside(relative, pathmod):
        rendered = target_path
    else:
        rendered = relative
    return rendered.replace("\\", "/") if windows_world else rendered


def _is_outside(relative: str, pathmod: Any) -> bool:
    return (relative == ".." or relative.startswith(f"..{pathmod.sep}")
            or pathmod.isabs(relative))


def _file_path(url: Any, windows: bool) -> str | None:
    """把一个 file URL 解码为其执行世界的路径；畸形 URL 失败 → None。"""
    raw_path = url.path
    lower = raw_path.lower()
    # 编码的路径分隔符（%2f/%5c）会被 fileURLToPath 拒绝。
    if "%2f" in lower or "%5c" in lower:
        return None
    if windows:
        if url.netloc:
            return None  # UNC authority：本载体不支持
        try:
            path = unquote(raw_path)
        except ValueError:
            return None
        if path.startswith("/"):
            path = path[1:]
    else:
        if url.netloc and url.netloc != "localhost":
            return None
        try:
            path = unquote(raw_path)
        except ValueError:
            return None
    if "\0" in path:
        return None
    return path


def present_lsp_call(args: dict) -> dict:
    """挂起 `lsp` 调用的 UI 呈现（通用搜索卡；标题带操作与一基光标）。"""
    return {
        "card": "generic",
        "kind": "search",
        "title": f"LSP {args['operation']} {args['file_path']}:"
                 f"{args['line']}:{args['character']}",
        "locations": [{"path": args["file_path"], "line": args["line"]}],
    }
