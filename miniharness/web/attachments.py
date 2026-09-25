"""web 表面：unary 结果的二进制附件（对齐上游三处，已核实 rc.1）。

上游对照：
  * `packages/api/gateway/src/index.ts:991-1049` `encodeRpcResult` /
    `encodeRuntimeResult`——运行期 JSON 化：bytes 叶子写成 null 占位，旁路登记
    `{path, codec:'bytes', part}`；循环引用抛 `gateway: circular RPC result`。
    投影结果挂在 result 对象的 `attachments` 槽（同 `{ok, value, attachments?}`）。
  * 同文件 `:663-682` `invokeRpc`——try 包住投影，抛错折 `rpcFailure`
    （`:1351`）→ `gateway/internal`。
  * `packages/client/connection/src/rpc-host.ts:295-312` `fullResponse`——载体
    分帧：失败分支与无附件成功分支都是 200 + `application/json`；有附件时改走
    `multipart/form-data`，`metadata` 文本 part 承载信封，且把 result 对象的
    `attachments` 拆出**提升到信封顶层**（`{...body, attachments}`），二进制
    各占一个 `bytes-<i>` part。
  * `packages/client/connection/src/client/rpc.ts:83-139` `parseBinaryResponse`
    ——客户端沿 path 把 Blob 写回占位（本仓库客户端面在 `webui/src/wire/rpc.ts`）。

投影与分帧同处一个模块：mini 只有 HTTP 一个 unary 载体，`fullResponse` 的调用点
就是投影点；上游分属 gateway（投影）与 Connection（分帧）两层。附件 part 与
metadata 的先后对客户端无影响（上游按名取 part，不按序）。
"""
from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi.responses import Response, StreamingResponse
from requests_toolbelt import MultipartEncoder

from ..core.session.json import thaw

__all__ = [
    "ATTACHMENT_CODEC",
    "METADATA_PART",
    "RpcAttachment",
    "attachment_metadata",
    "dumps",
    "project_result_attachments",
    "result_response",
]

#: 附件描述符的 codec 闭集（上游 `{ path, codec: 'bytes' as const, part }`）。
ATTACHMENT_CODEC = "bytes"

#: 信封文本 part 名（上游 `parts.set('metadata', ...)`）。
METADATA_PART = "metadata"

_BINARY_TYPES = (bytes, bytearray, memoryview)
_STREAM_CHUNK_BYTES = 65536


@dataclass(frozen=True)
class RpcAttachment:
    """被搬出 JSON 的二进制叶子；`path` 是它在结果值里的定位路径。"""

    path: tuple[str | int, ...]
    data: bytes


def dumps(value: Any) -> str:
    """解冻（session 日志事件是 mappingproxy/tuple 冻结形态）后 wire 序列化。"""
    return json.dumps(thaw(value), ensure_ascii=False)


def project_result_attachments(value: Any) -> tuple[Any, tuple[RpcAttachment, ...]]:
    """运行期 JSON 投影：bytes 叶子换成 null 占位，并旁路登记附件。

    与上游 `encodeRuntimeResult` 同形：只递归 Mapping 与 list/tuple（键路径用
    字符串、下标路径用整数），其余标量原样返回；祖先链上重复出现同一容器即
    循环引用，抛 `TypeError('gateway: circular RPC result')`。

    @param value - 业务结果值（冻结形态的 session 结构也在内）。
    @returns 投影后的 JSON 值 + 按发现顺序排列的附件。
    """
    attachments: list[RpcAttachment] = []
    ancestors: set[int] = set()

    def extract(node: Any, path: tuple[str | int, ...]) -> Any:
        if isinstance(node, _BINARY_TYPES):
            attachments.append(RpcAttachment(path, bytes(node)))
            return None
        if not isinstance(node, (Mapping, list, tuple)):
            return node
        marker = id(node)
        if marker in ancestors:
            raise TypeError("gateway: circular RPC result")
        ancestors.add(marker)
        if isinstance(node, Mapping):
            projected: Any = {key: extract(item, path + (key,))
                              for key, item in node.items()}
        else:
            projected = [extract(item, path + (index,))
                         for index, item in enumerate(node)]
        ancestors.discard(marker)
        return projected

    return extract(value, ()), tuple(attachments)


def attachment_metadata(attachments: tuple[RpcAttachment, ...]) -> list[dict]:
    """附件描述符（wire 顺序即 part 索引）：part 名为 `bytes-<i>`。

    @param attachments - `project_result_attachments` 的附件序列。
    @returns 每个附件一条 `{path, codec, part}` 描述。
    """
    return [{"path": list(attachment.path), "codec": ATTACHMENT_CODEC,
             "part": f"bytes-{index}"}
            for index, attachment in enumerate(attachments)]


def result_response(envelope: dict) -> Response:
    """server-response 信封 → JSON 或 multipart 响应（`fullResponse` 同款）。

    业务失败与无附件的成功都是 200 + `application/json`；有附件时 metadata part
    携带原字节位置的 null 占位与 attachments 描述，二进制各自独立成 part。

    @param envelope - `{type, rpcId, result}` server-response 信封。
    @returns 可直接返回的 ASGI 响应。
    @raises TypeError - 结果值存在循环引用（调用方折 `gateway/internal`）。
    """
    result = envelope.get("result")
    if not isinstance(result, Mapping) or result.get("ok") is not True:
        return Response(dumps(envelope), status_code=200, media_type="application/json")
    value, attachments = project_result_attachments(result.get("value"))
    if not attachments:
        return Response(dumps(envelope), status_code=200, media_type="application/json")
    descriptors = attachment_metadata(attachments)
    metadata = {**envelope, "result": {"ok": True, "value": value},
                "attachments": descriptors}
    fields: dict[str, str | tuple[str, bytes, str]] = {METADATA_PART: dumps(metadata)}
    for descriptor, attachment in zip(descriptors, attachments, strict=True):
        fields[descriptor["part"]] = (descriptor["part"], attachment.data,
                                      "application/octet-stream")
    encoder = MultipartEncoder(fields=fields)
    return StreamingResponse(_chunks(encoder), status_code=200,
                             media_type=encoder.content_type,
                             headers={"content-length": str(encoder.len)})


def _chunks(encoder: MultipartEncoder) -> Iterator[bytes]:
    """分块出体：整份复制会让 `maxFileBytes` 级的附件再占一份内存。"""
    while True:
        chunk = encoder.read(_STREAM_CHUNK_BYTES)
        if not chunk:
            return
        yield chunk
