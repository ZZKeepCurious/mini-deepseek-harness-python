"""web 路由层统一 `{args}` 边界校验（对齐上游 typert gateway 边界语义）。

对齐 `packages/api/gateway/src/index.ts` `assertExactArguments`（:1112-1138）+ `decode`
（:1140-1162）在 JSON 边界上对每个 Remote 方法的参数做两件事：

  * 字段集合精确匹配：缺 required / 多 unexpected → `gateway/arguments-invalid`，
    消息 `args fields do not match the descriptor: missing "x"; unexpected "y"`
    （missing 子句在前，`JSON.stringify` 双引号，`; ` 连接）。
  * 顶层字段 JSON 类型校验：类型错 → `gateway/input-invalid`，
    消息 `wire field "x" failed boundary validation`。

两者都是 `TypertGatewayFaultDetails = {endpoint, field?}`（remote-error-codes.ts）：
`arguments-invalid` 只带 `endpoint`，`input-invalid` 额外带 `field`。完整 wire 消息
在调用方头上加前缀 `typert gateway: <endpoint>: `（对齐 index.ts TypertGatewayError
`typert gateway: ${endpoint}: ${message}`）。

设计口径：本层只做「字段集合 + 顶层 JSON 类型」的边界准入；枚举（mode）、范围
（非负 int / -1 界 / 正整数）、非空字符串、跨字段语义（create workspaceId+cwd 互斥）
留在 handler 以业务码表达（对齐上游把业务语义留在 session-controller 而非 gateway）。
嵌套对象（content / address / action）只校验其是 object/array，不做深层结构校验
——深层语义由 handler 的既有校验承担。

每个方法的参数集来自上游 `packages/api/session-controller/src/types.ts` 的
request 接口（不带 `?` 即 required）。mini 单适配器对 `cursor`（list）上限忽略，
但为保持 wire 兼容仍允许其出现（optional str）。
"""
from __future__ import annotations

import json
from typing import Any

__all__ = [
    "BoundaryReject",
    "validate_args",
    "boundary_error_message",
]

STR = "str"
INT = "int"
OBJ = "object"
ARR = "array"


class BoundaryReject(Exception):
    """路由层边界拒绝：dispatch / open_stream 捕获后折进 RPC/流错误。

    @param code - `gateway/arguments-invalid` 或 `gateway/input-invalid`。
    @param message - 去掉 `typert gateway: <endpoint>: ` 前缀的内层原因。
    @param details - `TypertGatewayFaultDetails`（{endpoint, field?}）。
    """

    def __init__(self, code: str, message: str, details: dict):
        self.code = code
        self.message = message
        self.details = details


#: 每方法参数规格：字段 → (类型, required)。类型见模块级 STR/INT/OBJ/ARR。
_SPECS: dict[str, dict[str, tuple[str, bool]]] = {
    "session.list": {
        "cursor": (STR, False),
    },
    "session.search": {
        "query": (STR, True),
    },
    "session.create": {
        "workspaceId": (STR, False),
        "cwd": (STR, False),
        "sessionId": (STR, False),
        "agentPreset": (STR, False),
    },
    "session.selectModel": {
        "sessionId": (STR, True),
        "provider": (STR, True),
        "model": (STR, True),
        "reasoningEffort": (STR, False),
    },
    "session.modelCatalog": {},
    "session.canOpenWorkspacePath": {},
    "session.openWorkspacePath": {
        "path": (STR, True),
    },
    "session.rename": {
        "sessionId": (STR, True),
        "title": (STR, True),
    },
    "session.fork": {
        "sessionId": (STR, True),
        "atSeq": (INT, False),
    },
    "session.prompt": {
        "requestId": (STR, True),
        "sessionId": (STR, True),
        "mode": (STR, True),
        "content": (ARR, True),
        "clientTimeZone": (STR, False),
    },
    "session.attachment": {
        "sessionId": (STR, True),
        "attachmentId": (STR, True),
    },
    "session.updateQueue": {
        "sessionId": (STR, True),
        "itemId": (STR, True),
        "action": (OBJ, True),
    },
    "session.cancel": {
        "sessionId": (STR, True),
    },
    "session.page": {
        "address": (OBJ, True),
        "throughSeq": (INT, True),
        "beforeSeq": (INT, False),
        "maxMessages": (INT, False),
    },
    "session.follow": {
        "address": (OBJ, True),
        "maxMessages": (INT, False),
    },
    "session.control": {},
}


def _check(kind: str, value: Any) -> bool:
    if kind == STR:
        return isinstance(value, str)
    if kind == INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == OBJ:
        return isinstance(value, dict)
    if kind == ARR:
        return isinstance(value, list)
    return False


def validate_args(endpoint: str, args: Any) -> None:
    """校验方法级 args（已由调用方保证是 dict）。不通过抛 BoundaryReject。

    @param endpoint - `<namespace>/<method>`（如 `session.prompt`），用于详情与消息。
    """
    spec = _SPECS.get(endpoint)
    if spec is None:
        return
    if not isinstance(args, dict):
        raise BoundaryReject(
            "gateway/arguments-invalid", "args must be a plain object",
            {"endpoint": endpoint})
    extra = [key for key in args if key not in spec]
    missing = [key for key, (_, required) in spec.items()
               if required and key not in args]
    if extra or missing:
        clauses = []
        if missing:
            clauses.append("missing " + ", ".join(json.dumps(key) for key in missing))
        if extra:
            clauses.append("unexpected " + ", ".join(json.dumps(str(key)) for key in extra))
        raise BoundaryReject(
            "gateway/arguments-invalid",
            f"args fields do not match the descriptor: {'; '.join(clauses)}",
            {"endpoint": endpoint})
    for key, (kind, _) in spec.items():
        if key not in args:
            continue
        if not _check(kind, args[key]):
            raise BoundaryReject(
                "gateway/input-invalid",
                f'wire field "{key}" failed boundary validation',
                {"endpoint": endpoint, "field": key})


def boundary_error_message(endpoint: str, message: str) -> str:
    """完整 wire 消息：`typert gateway: <endpoint>: <message>`（对齐 index.ts）。"""
    return f"typert gateway: {endpoint}: {message}"
