"""测试用桩 LSP 服务器：LSP base protocol over stdio。

只实现本仓 LSP host 依赖的最小子集：`initialize`/`initialized`/`shutdown`/`exit`
生命周期 + `textDocument/definition`/`references`/`implementation`/`hover` 四操作。
`--mode <name>` 可选行为：
  * `normal`（缺省）：四操作全支持、textDocumentSync=1（Full，隐含 openClose）。
  * `no-definition`：不声明 definitionProvider（触发 LSP_UNSUPPORTED_OPERATION）。
  * `no-openclose`：textDocumentSync={change:1}（无 openClose，触发瞬态 open 不支持）。
  * `utf-8`：positionEncoding='utf-8'（触发协商拒绝）。
  * `malformed`：definition 返回畸形载荷（触发 LSP_MALFORMED_RESPONSE）。
  * `crash-on-open`：收到 didOpen 即退出（触发传输失败/重试路径）。
  * `exit-ignoring`：shutdown 不作答（触发拆除宽限→强杀）。
"""
from __future__ import annotations

import json
import sys


def _read_message(stream) -> dict | None:
    length = None
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.rstrip(b"\r\n")
        if line == b"":
            break
        if line.lower().startswith(b"content-length:"):
            try:
                length = int(line.split(b":", 1)[1].strip())
            except ValueError:
                return None
    if length is None:
        return None
    body = stream.read(length)
    if body is None or len(body) < length:
        return None
    return json.loads(body.decode("utf-8"))


def _write_message(message: dict) -> None:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()


def main(argv: list[str]) -> int:
    mode = "normal"
    if len(argv) >= 2 and argv[0] == "--mode":
        mode = argv[1]

    capabilities: dict = {
        "textDocumentSync": 1,
        "definitionProvider": True,
        "referencesProvider": True,
        "implementationProvider": True,
        "hoverProvider": True,
    }
    if mode == "no-definition":
        capabilities.pop("definitionProvider", None)
    elif mode == "no-openclose":
        capabilities["textDocumentSync"] = {"change": 1}
    elif mode == "utf-8":
        capabilities["positionEncoding"] = "utf-8"

    while True:
        message = _read_message(sys.stdin.buffer)
        if message is None:
            return 0
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            _write_message({"jsonrpc": "2.0", "id": request_id,
                            "result": {"capabilities": capabilities}})
        elif method == "initialized":
            continue
        elif method == "shutdown":
            if mode == "exit-ignoring":
                continue  # 永不作答，逼出拆除宽限→强杀
            _write_message({"jsonrpc": "2.0", "id": request_id, "result": None})
        elif method == "exit":
            return 0
        elif method == "textDocument/didOpen":
            if mode == "crash-on-open":
                return 1
        elif method == "textDocument/definition":
            if mode == "malformed":
                _write_message({"jsonrpc": "2.0", "id": request_id, "result": 42})
            else:
                uri = (message.get("params") or {}).get("textDocument", {}).get("uri", "")
                _write_message({"jsonrpc": "2.0", "id": request_id, "result": {
                    "uri": uri,
                    "range": {"start": {"line": 0, "character": 0},
                              "end": {"line": 0, "character": 3}},
                }})
        elif method == "textDocument/references":
            uri = (message.get("params") or {}).get("textDocument", {}).get("uri", "")
            _write_message({"jsonrpc": "2.0", "id": request_id, "result": [
                {"uri": uri, "range": {"start": {"line": 1, "character": 2},
                                       "end": {"line": 1, "character": 5}}},
                {"targetUri": uri, "targetSelectionRange": {
                    "start": {"line": 2, "character": 0},
                    "end": {"line": 2, "character": 4}}},
            ]})
        elif method == "textDocument/implementation":
            _write_message({"jsonrpc": "2.0", "id": request_id, "result": None})
        elif method == "textDocument/hover":
            _write_message({"jsonrpc": "2.0", "id": request_id, "result": {
                "contents": {"kind": "markdown", "value": "**stub hover**"}}})
        elif request_id is not None:
            _write_message({"jsonrpc": "2.0", "id": request_id, "result": None})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
