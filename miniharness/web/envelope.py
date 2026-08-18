"""web 表面：四象限 RPC 信封（对齐 `packages/host/apiproxy/src/api/rpc.ts`）。

契约（已核实，逐条对应上游）：
  * 四种消息组成判别联合，判别字段 = `type`：
      client-request  客户端发起（wire 载体：POST /api/<method> body）
      server-response 对 client-request 的响应（wire 载体：该 POST 的响应体）
      server-request  服务端发起（wire 载体：下游事件流帧；可应答的交互
                      （approval/question）rpcId 稳定且重连重放复用）
      client-response 对 server-request 的响应（wire 载体：POST /api/respond body）
  * RpcId：发起方签发、响应回显，从不重铸（响应 rpcId 必与请求一致）。
  * RpcResult = {ok:true, value} | {ok:false, error:RpcError}：业务方法绝不抛
    业务错误——一律经 result.ok 表达。
  * RpcError = {code, message, details}，code 是 39 码闭集（RpcErrorDetailsMap
    键集）；details 按 code 判别（internal 显式 {}）。
  * transport_error：把载体层异常折进 RpcResult 错误分支，兜底码 'internal'
    （每个载体消费者用同一套折叠）。
  * RpcReceipt = {accepted:true} | {accepted:false, reason:'not-pending'|'bad-response'}
    ——POST /api/respond 的响应体，属载体层（不是 RpcMessage）。

纯契约层：零第三方依赖、零 HTTP。判定标准：消息能否在四种判别之间无损往返。
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
    "server_request",
    "client_response",
    "parse_message",
    "rpc_receipt_accepted",
    "rpc_receipt_rejected",
]

# RpcErrorDetailsMap 的键集（上游 39 码闭集；details 按 code 判别）
RPC_ERROR_CODES = frozenset({
    "bad-request",
    "cancelled",
    "session-not-found",
    "model-unavailable",
    "session-conflict",
    "invalid-time-zone",
    "workspace-attach-failed",
    "workspace-not-found",
    "workspace-invalid-path",
    "workspace-name-conflict",
    "workspace-move-invalid",
    "directory-unreadable",
    "directory-exists",
    "directory-create-failed",
    "directory-picker-unavailable",
    "agent-preset-read-only",
    "agent-preset-locked",
    "agent-preset-conflict",
    "agent-preset-not-found",
    "agent-preset-invalid",
    "agent-busy",
    "attachment-error",
    "queue-item-not-found",
    "steer-unavailable",
    "command-error",
    "unknown-command",
    "settings-rejected",
    "settings-conflict",
    "credential-rejected",
    "model-discovery-failed",
    "title-invalid",
    "fork-unavailable",
    "subagent-parent-unavailable",
    "subagent-not-found",
    "subagent-catalog-diagnostic",
    "subagent-not-resumable",
    "subagent-unauthorized",
    "subagent-delivery-unavailable",
    "internal",
})

_TYPE_TAGS = frozenset({"client-request", "server-response", "server-request", "client-response"})


class EnvelopeError(ValueError):
    """信封校验失败（载体层捕获后按位置回复 bad-request）。"""


def rpc_id() -> str:
    """签发一个新的 RpcId（发起方用）：UUID 字符串。"""
    return str(uuid.uuid4())


def rpc_error(code: str, message: str, details: dict | None = None) -> dict:
    """构造 RpcError 分支：code 必须在 39 码闭集内（越界 fail loud，契约错误）。"""
    if code not in RPC_ERROR_CODES:
        raise EnvelopeError(f"unknown rpc error code {code!r}")
    return {"code": code, "message": message, "details": details or {}}


def transport_error(error: Any) -> dict:
    """把载体层异常折进 RpcResult 错误分支（上游 transportError）：兜底 'internal'。

    返回的是 RpcResult（{ok:false, error}），不是裸 RpcError——每个载体消费者
    用同一套折叠把抛出的值统一成 result 形态。
    """
    return rpc_result_error(rpc_error("internal", str(error), {}))


def rpc_result_ok(value: Any) -> dict:
    """RpcResult 成功分支：{ok: true, value}。"""
    return {"ok": True, "value": value}


def rpc_result_error(error: dict) -> dict:
    """RpcResult 失败分支：{ok: false, error: RpcError}。"""
    return {"ok": False, "error": error}


def client_request(rpc_id_: str, method: str, payload: Any) -> dict:
    """client-request 帧（wire 载体：POST /api/<method> body）。"""
    return {"type": "client-request", "rpcId": rpc_id_, "method": method, "payload": payload}


def server_response(rpc_id_: str, result: dict) -> dict:
    """server-response 帧：rpcId 必回显请求的，不重铸。result 是 RpcResult。"""
    return {"type": "server-response", "rpcId": rpc_id_, "result": result}


def server_request(rpc_id_: str, method: str, payload: Any) -> dict:
    """server-request 帧（wire 载体：下游事件流帧）。可应答帧 rpcId 稳定复用。"""
    return {"type": "server-request", "rpcId": rpc_id_, "method": method, "payload": payload}


def client_response(rpc_id_: str, result: dict) -> dict:
    """client-response 帧（wire 载体：POST /api/respond body）。rpcId 回显。"""
    return {"type": "client-response", "rpcId": rpc_id_, "result": result}


def parse_message(obj: Any) -> dict:
    """校验一个 RpcMessage 的 wire 全形并原样返回；不合法抛 EnvelopeError。

    严格校验：必须是 dict、type 在四者内、判别字段存在；client-request 要求
    method 是字符串。payload/result 值域留给上层（domain 方法按各自 schema 再查）。
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
    elif type_ == "server-request":
        method = obj.get("method")
        if not isinstance(method, str):
            raise EnvelopeError("server-request must carry a string method")
        if "payload" not in obj:
            raise EnvelopeError("server-request must carry a payload")
    elif type_ in ("server-response", "client-response"):
        if "result" not in obj or not isinstance(obj["result"], dict):
            raise EnvelopeError(f"{type_} must carry a result object")
        result = obj["result"]
        if "ok" not in result:
            raise EnvelopeError(f"{type_} result must carry an ok flag")
        if result["ok"] is not True and not (result["ok"] is False and isinstance(result.get("error"), dict)):
            raise EnvelopeError(f"{type_} result has an invalid ok/error shape")
    return obj


def rpc_receipt_accepted() -> dict:
    """POST /api/respond 的载体层回执：{accepted: true}。"""
    return {"accepted": True}


def rpc_receipt_rejected(reason: str) -> dict:
    """回执拒绝：{accepted: false, reason: 'not-pending'|'bad-response'}。"""
    if reason not in ("not-pending", "bad-response"):
        raise EnvelopeError(f"unknown receipt rejection reason {reason!r}")
    return {"accepted": False, "reason": reason}