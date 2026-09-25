"""unary 结果二进制附件：运行期投影 + multipart 分帧（对齐 rc.1 附件契约）。

上游对照：`packages/api/gateway/src/index.ts` `encodeRpcResult` /
`encodeRuntimeResult`（bytes 叶子 → null 占位 + `{path, codec:'bytes', part}`）、
`packages/client/connection/src/rpc-host.ts` `fullResponse`（有附件走
`multipart/form-data`）、`packages/client/connection/src/client/rpc.ts`
`parseBinaryResponse`（客户端沿 path 写回占位）。
测 multipart 体用 stdlib `email` 解析，不复用被测编码器。
"""
import asyncio
import json
import os
import tempfile
import unittest
from email.parser import BytesParser

from miniharness.core.session.json import deep_freeze
from miniharness.core.scope import Context
from miniharness.fs import install_local_fs
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.seams.sandbox_policy import SandboxPolicyService
from miniharness.web.api import WebApi
from miniharness.web.attachments import (
    attachment_metadata,
    dumps,
    project_result_attachments,
    result_response,
)
from miniharness.web.server import create_app
from miniharness.workspace_files import install_workspace_files

try:
    from fastapi.testclient import TestClient
    _HAVE_TC = True
except Exception:  # noqa: BLE001
    _HAVE_TC = False


def _body(response) -> bytes:
    if hasattr(response, "body_iterator"):
        async def collect() -> bytes:
            return b"".join([chunk async for chunk in response.body_iterator])

        return asyncio.run(collect())
    if hasattr(response, "body"):
        return response.body
    return response.content


def _multipart(response) -> dict:
    """按 stdlib email 拆 multipart 体：{part 名: (content-type, 原始字节)}。"""
    message = BytesParser().parsebytes(
        b"Content-Type: " + response.headers["content-type"].encode("ascii")
        + b"\r\nMIME-Version: 1.0\r\n\r\n" + _body(response))
    return {part.get_param("name", header="content-disposition"):
            (part.get_content_type(), part.get_payload(decode=True))
            for part in message.get_payload()}


def _reassemble(response):
    """客户端重组（`parseBinaryResponse` 同款）：沿 path 把 part 写回占位。"""
    parts = _multipart(response)
    envelope = json.loads(parts["metadata"][1].decode("utf-8"))
    value = envelope["result"]["value"]
    for descriptor in envelope["attachments"]:
        payload = parts[descriptor["part"]][1]
        node = value
        for segment in descriptor["path"][:-1]:
            node = node[segment]
        node[descriptor["path"][-1]] = payload
    return envelope, value


class ProjectResultAttachmentsTest(unittest.TestCase):
    def test_root_bytes_becomes_placeholder(self):
        value, attachments = project_result_attachments(b"\x00\x01")
        self.assertIsNone(value)
        self.assertEqual([(a.path, a.data) for a in attachments], [((), b"\x00\x01")])

    def test_nested_paths_in_discovery_order(self):
        value, attachments = project_result_attachments(
            {"data": b"\x01", "nested": {"deep": [b"\x02", {"x": b"\x03"}]}})
        self.assertEqual(value, {"data": None, "nested": {"deep": [None, {"x": None}]}})
        self.assertEqual([(a.path, a.data) for a in attachments],
                         [(("data",), b"\x01"), (("nested", "deep", 0), b"\x02"),
                          (("nested", "deep", 1, "x"), b"\x03")])

    def test_bytearray_and_memoryview_are_bytes_leaves(self):
        value, attachments = project_result_attachments(
            {"a": bytearray(b"\x04"), "b": memoryview(b"\x05")})
        self.assertEqual(value, {"a": None, "b": None})
        self.assertEqual([a.data for a in attachments], [b"\x04", b"\x05"])

    def test_value_without_bytes_is_unchanged(self):
        payload = {"text": "héllo", "n": 1, "f": 1.5, "flag": True, "none": None,
                   "items": [1, "two", {"three": 3}]}
        value, attachments = project_result_attachments(payload)
        self.assertEqual(value, payload)
        self.assertEqual(attachments, ())

    def test_frozen_session_forms_are_normalized(self):
        value, attachments = project_result_attachments(
            deep_freeze({"data": [b"\x06"]}))
        self.assertEqual(value, {"data": [None]})
        self.assertEqual([(a.path, a.data) for a in attachments],
                         [(("data", 0), b"\x06")])

    def test_circular_result_rejected(self):
        mapping: dict = {}
        mapping["self"] = mapping
        items: list = []
        items.append(items)
        for value in (mapping, items, {"wrap": mapping}):
            with self.assertRaises(TypeError) as caught:
                project_result_attachments(value)
            self.assertEqual(str(caught.exception), "gateway: circular RPC result")

    def test_repeated_sibling_reference_is_not_circular(self):
        shared = {"n": 1}
        value, attachments = project_result_attachments({"x": shared, "y": shared})
        self.assertEqual(value, {"x": {"n": 1}, "y": {"n": 1}})
        self.assertEqual(attachments, ())

    def test_dumps_thaws_frozen_forms(self):
        self.assertEqual(dumps(deep_freeze({"a": [1, 2]})), '{"a": [1, 2]}')


class AttachmentMetadataTest(unittest.TestCase):
    def test_part_names_follow_attachment_order(self):
        _, attachments = project_result_attachments([b"\x01", {"x": b"\x02"}])
        self.assertEqual(attachment_metadata(attachments),
                         [{"path": [0], "codec": "bytes", "part": "bytes-0"},
                          {"path": [1, "x"], "codec": "bytes", "part": "bytes-1"}])


class ResultResponseTest(unittest.TestCase):
    def _envelope(self, result: dict) -> dict:
        return {"type": "server-response", "rpcId": "r1", "result": result}

    def test_failure_is_json(self):
        response = result_response(self._envelope(
            {"ok": False, "error": {"code": "session/not-found", "message": "no",
                                    "details": {}}}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "application/json")
        self.assertEqual(json.loads(_body(response))["result"]["ok"], False)

    def test_success_without_bytes_is_json(self):
        response = result_response(self._envelope({"ok": True, "value": {"a": 1}}))
        self.assertEqual(response.media_type, "application/json")
        self.assertEqual(json.loads(_body(response))["result"]["value"], {"a": 1})

    def test_void_success_is_json(self):
        response = result_response(self._envelope({"ok": True}))
        self.assertEqual(response.media_type, "application/json")
        self.assertNotIn("value", json.loads(_body(response))["result"])

    def test_success_with_bytes_is_multipart(self):
        response = result_response(self._envelope(
            {"ok": True, "value": {"text": "hi", "data": b"\x00\xff"}}))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.media_type.startswith("multipart/form-data; boundary="))
        parts = _multipart(response)
        self.assertEqual(set(parts), {"metadata", "bytes-0"})
        self.assertEqual(parts["bytes-0"],
                         ("application/octet-stream", b"\x00\xff"))
        envelope = json.loads(parts["metadata"][1].decode("utf-8"))
        self.assertEqual(envelope["rpcId"], "r1")
        self.assertEqual(envelope["result"], {"ok": True,
                                              "value": {"text": "hi", "data": None}})
        self.assertEqual(envelope["attachments"],
                         [{"path": ["data"], "codec": "bytes", "part": "bytes-0"}])

    def test_multiple_attachments_keep_index_order(self):
        response = result_response(self._envelope(
            {"ok": True, "value": {"rows": [b"one", b"two"]}}))
        parts = _multipart(response)
        envelope = json.loads(parts["metadata"][1].decode("utf-8"))
        self.assertEqual([row["path"] for row in envelope["attachments"]],
                         [["rows", 0], ["rows", 1]])
        self.assertEqual([row["part"] for row in envelope["attachments"]],
                         ["bytes-0", "bytes-1"])
        self.assertEqual([parts[row["part"]][1] for row in envelope["attachments"]],
                         [b"one", b"two"])

    def test_content_length_matches_body(self):
        response = result_response(self._envelope(
            {"ok": True, "value": {"data": b"x" * 1024}}))
        self.assertEqual(response.headers["content-length"],
                         str(len(_body(response))))

    def test_circular_result_raises(self):
        mapping: dict = {}
        mapping["self"] = mapping
        with self.assertRaisesRegex(TypeError, "gateway: circular RPC result"):
            result_response(self._envelope({"ok": True, "value": mapping}))


@unittest.skipUnless(_HAVE_TC, "fastapi TestClient unavailable")
class ReadBytesHttpTest(unittest.TestCase):
    """`workspaceFiles/readBytes` 走真实 ASGI：HTTP 层 multipart + 客户端重组。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = os.path.join(self._tmp.name, "work")
        os.makedirs(self.work)
        self.ctx = Context(name="web-attachments-test")
        self.addCleanup(self.ctx.dispose)
        SandboxPolicyService(self.ctx, {"mode": "danger-full-access"})
        install_local_fs(self.ctx, {"cwd": self.work})
        install_workspace_files(self.ctx)
        self.api = WebApi(self.ctx, FakeLlmAdapter())
        self.client = TestClient(create_app(self.api, self.api.gateway))
        self.addCleanup(self.client.close)
        self.session_id = self._value(self._post("session.create", {"cwd": self.work},
                                                 "c1"))["sessionId"]
        self.file = os.path.join(self.work, "blob.bin")
        self.payload = bytes(range(256)) * 4
        with open(self.file, "wb") as handle:
            handle.write(self.payload)

    def _post(self, endpoint, args, rpc_id="r1"):
        return self.client.post(f"/api/{endpoint}", json={
            "type": "client-request", "rpcId": rpc_id, "method": endpoint,
            "payload": {"args": args}})

    def _value(self, response):
        data = response.json()
        self.assertTrue(data["result"]["ok"], data["result"].get("error"))
        return data["result"].get("value")

    def _read_bytes(self, path, options=None):
        args = {"workspaceFileScopeId": self.session_id, "path": path}
        if options is not None:
            args["options"] = options
        return self._post("workspaceFiles/readBytes", args, "rb1")

    def test_whole_file_roundtrips_through_attachment(self):
        response = self._read_bytes(self.file)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("multipart/form-data"))
        envelope, value = _reassemble(response)
        self.assertEqual(envelope["rpcId"], "rb1")
        self.assertEqual(value["data"], self.payload)
        self.assertEqual((value["offset"], value["eof"], value["bytes"]),
                         (0, True, len(self.payload)))

    def test_range_window_roundtrips(self):
        envelope, value = _reassemble(
            self._read_bytes(self.file, {"range": {"offset": 10, "length": 8}}))
        self.assertEqual(value["data"], self.payload[10:18])
        self.assertEqual(envelope["attachments"],
                         [{"path": ["data"], "codec": "bytes", "part": "bytes-0"}])

    def test_json_methods_stay_json(self):
        response = self._post("session.list", {}, "l1")
        self.assertEqual(response.headers["content-type"].split(";")[0],
                         "application/json")
        self.assertIn("items", self._value(response))
