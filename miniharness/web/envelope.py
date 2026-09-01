"""web 表面：两信封 RPC（对齐 `packages/client/connection/src/rpc-schema.ts` + rpc.ts）。

契约（已核实，逐条对应上游 alpha.1）：
  * 判别联合只有两型，判别字段 = `type`：
      client-request  客户端发起（wire 载体：POST /api/<endpoint> body）
      server-response 对 client-request 的响应（wire 载体：该 POST 的响应体）
    `server-request` / `client-response`（apiproxy 四象限）在新契约中不存在：
    下游事件走 Gateway WebSocket mux（`web/stream_protocol.py`），服务端发起的
    可应答交互走 `$events` 远程事件流 + `$events/result` unary。
  * RpcId：发起方签发、响应回显，从不重铸（响应 rpcId 必与请求一致）。
  * RpcResult = {ok:true, value?} | {ok:false, error:RpcError}：成功分支的 value
    可选（业务返回 undefined 时整体省略该字段，`rpc-server.ts` 只回显既有字段）；
    业务方法绝不抛业务错误——一律经 result.ok 表达。
  * RpcError = {code, message, details}，code 是命名空间域闭集
    （RemoteErrorDetailsMap 键集，见 envelope.RPC_ERROR_CODES）；details 恒为对象。
  * transport_error：把载体层异常折进 RpcResult 错误分支，兜底码 'gateway/internal'
    （每个载体消费者用同一套折叠）。

纯契约层：零第三方依赖、零 HTTP。判定标准：消息能否在两型判别之间无损往返。
"""
from __future__ import annotations

import uuid
from typing import Any

__all__ = [
    "RPC_ERROR_CODES",
    "EnvelopeError",
    "rpc_id",
    "rpc_error",
    "transport_error",
    "rpc_result_ok",
    "rpc_result_error",
    "client_request",
    "server_response",
    "parse_message",
]

# RemoteErrorDetailsMap 的键集（alpha.2 起统一为 `<namespace>/<name>` 命名空间码：
# typert-protocol/types.ts:47-53 基础设施 gateway/* + 各域 merge-extensible 注册，
# 见 verified-diffs §3.4）。上游 `.details` 按 code 判别；mini 无 subagent 目录等域时
# 这些码仍可作兜底拒绝。
RPC_ERROR_CODES = frozenset({
    "agent-preset/conflict",
    "agent-preset/invalid",
    "agent-preset/not-found",
    "gateway/arguments-invalid",
    "gateway/bad-request",
    "gateway/cancelled",
    "gateway/input-invalid",
    "gateway/internal",
    "session/agent-busy",
    "session/attachment-invalid",
    "session/conflict",
    "session/fork-unavailable",
    "session/invalid-time-zone",
    "session/model-unavailable",
    "session/not-found",
    "session/queue-item-not-found",
    "session/steer-unavailable",
    "session/title-invalid",
    "session/workspace-attach-failed",
    "subagent/catalog-diagnostic",
    "subagent/not-found",
    "subagent/unauthorized",
    "workspace/not-found",
})

_TYPE_TAGS = frozenset({"client-request", "server-response"})

# RpcResult 成功分支缺省哨兵：业务无值（上游 undefined）时省略 value 字段
_ABSENT = object()


class EnvelopeError(ValueError):
    """信封校验失败（载体层捕获后按位置回复 bad-request）。"""


def rpc_id() -> str:
    """签发一个新的 RpcId（发起方用）：UUID 字符串。"""
    return str(uuid.uuid4())


def rpc_error(code: str, message: str, details: dict | None = None) -> dict:
    """构造 RpcError 分支：code 必须在域闭集内（越界 fail loud，契约错误）。"""
    if code not in RPC_ERROR_CODES:
        raise EnvelopeError(f"unknown rpc error code {code!r}")
    return {"code": code, "message": message, "details": details or {}}


def transport_error(error: Any) -> dict:
    """把载体层异常折进 RpcResult 错误分支（上游 rpcFailure 兜底 'gateway/internal'）。

    返回的是 RpcResult（{ok:false, error}），不是裸 RpcError——每个载体消费者
    用同一套折叠把抛出的值统一成 result 形态。
    """
    return rpc_result_error(rpc_error("gateway/internal", str(error), {}))


def rpc_result_ok(value: Any = _ABSENT) -> dict:
    """RpcResult 成功分支：{ok:true, value?}。value 缺省时省略（对齐 undefined）。"""
    return {"ok": True, "value": value} if value is not _ABSENT else {"ok": True}


def rpc_result_error(error: dict) -> dict:
    """RpcResult 失败分支：{ok:false, error: RpcError}。"""
    return {"ok": False, "error": error}


def client_request(rpc_id_: str, method: str, payload: Any) -> dict:
    """client-request 帧（wire 载体：POST /api/<endpoint> body）。"""
    return {"type": "client-request", "rpcId": rpc_id_, "method": method, "payload": payload}


def server_response(rpc_id_: str, result: dict) -> dict:
    """server-response 帧：rpcId 必回显请求的，不重铸。result 是 RpcResult。"""
    return {"type": "server-response", "rpcId": rpc_id_, "result": result}


def parse_message(obj: Any) -> dict:
    """校验一个 RpcMessage 的 wire 全形并原样返回；不合法抛 EnvelopeError。

    对齐 rpc-schema.ts 对 type/rpcId/method/result 的逐字段校验；未知附加键
    schemastery 对象 schema 默认忽略，这里同样不拒绝。payload/result.value
    值域留给上层（domain 方法按各自 args 再查）。
    """
    if not isinstance(obj, dict):
        raise EnvelopeError("rpc message must be a JSON object")
    type_ = obj.get("type")
    if type_ not in _TYPE_TAGS:
        raise EnvelopeError(f"unknown rpc message type {type_!r}")
    if "rpcId" not in obj or not isinstance(obj["rpcId"], str):
        raise EnvelopeError("rpc message must carry a string rpcId")
    if type_ == "client-request":
        method = obj.get("method")
        if not isinstance(method, str):
            raise EnvelopeError("client-request must carry a string method")
        if "payload" not in obj:
            raise EnvelopeError("client-request must carry a payload")
    else:
        result = obj.get("result")
        if not isinstance(result, dict):
            raise EnvelopeError("server-response must carry a result object")
        if "ok" not in result or result["ok"] is not True and result["ok"] is not False:
            raise EnvelopeError("server-response result must carry a boolean ok")
        if result["ok"]:
            pass  # value 可选且值域任意（rpcResultSchema 成功分支 value: z.unknown().optional()）
        else:
            error = result.get("error")
            if not isinstance(error, dict):
                raise EnvelopeError("server-response error result must carry an error object")
            if not isinstance(error.get("code"), str):
                raise EnvelopeError("rpc error must carry a string code")
            if not isinstance(error.get("message"), str):
                raise EnvelopeError("rpc error must carry a string message")
            if not isinstance(error.get("details"), dict):
                raise EnvelopeError("rpc error must carry an object details")
    return obj