"""本地 host 的纯协议翻译：服务端能力允许什么，以及它的
`Location`/`LocationLink`/`Hover` 载荷如何归一到 seam 的闭集结果 union。

无 I/O、无进程状态——每个函数都是纯变换。对齐
packages/lsp/lsp-stdio/src/translate.ts。
"""
from __future__ import annotations

from typing import Any

from ..lsp import LspError

__all__ = [
    "negotiate_position_encoding",
    "normalize_hover",
    "normalize_locations",
    "request_method",
    "supports_operation",
    "supports_transient_open",
]

_METHOD_BY_OPERATION = {
    "goToDefinition": "textDocument/definition",
    "findReferences": "textDocument/references",
    "goToImplementation": "textDocument/implementation",
    "hover": "textDocument/hover",
}

_CAPABILITY_BY_OPERATION = {
    "goToDefinition": "definitionProvider",
    "findReferences": "referencesProvider",
    "goToImplementation": "implementationProvider",
    "hover": "hoverProvider",
}


def request_method(operation: str) -> str:
    """每个 LSP 操作对应的 `textDocument/*` 请求方法。"""
    return _METHOD_BY_OPERATION[operation]


def supports_operation(capabilities: dict, operation: str) -> bool:
    """服务端是否声明支持所请求操作（`true` 或 options 对象即支持）。"""
    value = capabilities.get(_CAPABILITY_BY_OPERATION[operation])
    return value is not None and value is not False


def supports_transient_open(sync: Any) -> bool:
    """`textDocumentSync` 是否允许本 host 依赖的瞬态 `didOpen`/`didClose`。

    遗留枚举形（Full=1/Incremental=2）隐含 open/close；options 形要求显式
    `openClose: true`（协议把省略的 openClose 缺省为 false）。`bool` 在 Python 里是
    `int` 的子类，须先排除。
    """
    if sync is None or isinstance(sync, bool):
        return False
    if isinstance(sync, int):
        return sync in (1, 2)
    if isinstance(sync, dict):
        return sync.get("openClose") is True
    return False


def negotiate_position_encoding(encoding: Any) -> str:
    """归一位置编码：省略 → `utf-16`；非 `utf-16` → 协议错误。"""
    if encoding is None or encoding == "utf-16":
        return "utf-16"
    raise ValueError(
        f'server negotiated unsupported position encoding "{encoding}"; '
        "this host requires utf-16")


def normalize_locations(payload: Any) -> list[dict]:
    """把导航结果（`Location`/`Location[]`/`LocationLink[]`/`null`）归一为 locations。

    `Location` 直接映射；`LocationLink` 映射 `targetUri` + `targetSelectionRange`。
    """
    if payload is None:
        return []
    elements = payload if isinstance(payload, list) else [payload]
    locations: list[dict] = []
    for element in elements:
        if not isinstance(element, dict):
            raise _malformed("LSP navigation result contained a non-object entry")
        if _is_location_link(element):
            locations.append({"uri": element["targetUri"],
                              "range": _to_range(element["targetSelectionRange"])})
        elif _is_location(element):
            locations.append({"uri": element["uri"],
                              "range": _to_range(element["range"])})
        else:
            raise _malformed(
                "LSP navigation result contained neither a Location nor a LocationLink")
    return locations


def normalize_hover(payload: Any) -> dict | None:
    """把 `Hover`（或 `null`）归一为 hover；无内容 → None。"""
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise _malformed("LSP hover result was not an object")
    contents = _render_hover_contents(payload.get("contents"))
    if contents == "":
        return None
    range_ = payload.get("range")
    if range_ is None:
        return {"contents": contents}
    if not _is_range(range_):
        raise _malformed("LSP hover result contained a malformed range")
    return {"contents": contents, "range": _to_range(range_)}


def _to_range(range_: dict) -> dict:
    return {
        "start": {"line": range_["start"]["line"],
                  "character": range_["start"]["character"]},
        "end": {"line": range_["end"]["line"],
                "character": range_["end"]["character"]},
    }


def _is_location_link(value: dict) -> bool:
    return (isinstance(value.get("targetUri"), str)
            and _is_range(value.get("targetSelectionRange")))


def _is_location(value: dict) -> bool:
    return isinstance(value.get("uri"), str) and _is_range(value.get("range"))


def _is_range(value: Any) -> bool:
    return (isinstance(value, dict) and _is_position(value.get("start"))
            and _is_position(value.get("end")))


def _is_position(value: Any) -> bool:
    return (isinstance(value, dict) and _is_coordinate(value.get("line"))
            and _is_coordinate(value.get("character")))


def _is_coordinate(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _render_marked_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    return f"```{value['language']}\n{value['value']}\n```"


def _render_hover_contents(contents: Any) -> str:
    """把 `Hover.contents` 的三种编码渲染成一个字符串（入参是不可信 wire 数据）。"""
    if contents is None:
        raise _malformed("LSP hover result had no contents")
    if isinstance(contents, str):
        return contents
    if isinstance(contents, list):
        parts: list[str] = []
        for value in contents:
            if not _is_marked_string(value):
                raise _malformed(
                    "LSP hover contents contained a malformed MarkedString")
            parts.append(_render_marked_string(value))
        return "\n\n".join(parts)
    if not isinstance(contents, dict):
        raise _malformed(
            "LSP hover contents were not MarkupContent, MarkedString, or an array")
    if contents.get("kind") in ("markdown", "plaintext"):
        if not isinstance(contents.get("value"), str):
            raise _malformed("LSP hover MarkupContent value was not a string")
        return contents["value"]
    if isinstance(contents.get("language"), str) and isinstance(contents.get("value"), str):
        return _render_marked_string(
            {"language": contents["language"], "value": contents["value"]})
    raise _malformed("LSP hover contents were not MarkupContent, MarkedString, or an array")


def _is_marked_string(value: Any) -> bool:
    if isinstance(value, str):
        return True
    return (isinstance(value, dict) and isinstance(value.get("language"), str)
            and isinstance(value.get("value"), str))


def _malformed(message: str) -> LspError:
    return LspError(message, "LSP_MALFORMED_RESPONSE")
