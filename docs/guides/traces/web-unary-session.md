# 走查 T3 · Web unary（session 族）

**动机**：确认两信封 RPC 如何把 HTTP POST 映射到 `WebApi.ROUTES` 里的 handler；会话状态仍以 Session 事件日志为唯一数据源，handler 不另存第二份真相。

## 前置

```bash
pip install -e ".[web]"
```

## 命令

本地验证以测试夹具中的真实 uvicorn 为准（无需手启服务器）：

```bash
python -m unittest tests.test_web_server tests.test_web_api -v
```

手启可选（另开终端）：

```bash
python -m miniharness.cli --profile web
# POST /api/session/create 等，信封见 interface-wire §2
```

## 预期现象

- HTTP 200 + `server-response` 信封；业务失败时 `result.ok=false` 且 `error.code` 在 `RPC_ERROR_CODES` 内。
- `session/create` 经 `SessionStore` 写入 Session；响应 JSON 或 multipart（附件见 wire §3.1）。
- 端点全集以 `len(WebApi.ROUTES)` 为准（现行 **101** 条，`miniharness/web/api.py`）。

## 机制

1. 客户端 POST `/api/<namespace>/<method>`（canonical 形式见 [interface-wire.md §2](../../interface-wire.md#wire-2)）。
2. `miniharness/web/server.py` 解信封 → `web/api.py` `WebApi.dispatch`。
3. Handler 只读写 Session / Store，不在 wire 层另存对话真相源。

## 经过的 mini 模块

`web/server.py` → `web/envelope.py` → `web/api.py` → `core/session_store.py` → `core/session/`

## 日志 / RPC 侧常见 type

unary 响应体内的 session 事件 type 与第 01 章一致（如 `session/created` 服务公告、`session/event` 推送）；错误码见 `envelope.py`。

## 测试

`tests.test_web_server` · `tests.test_web_api`

## 关联

[wire-tour.md](../wire-tour.md) · [章节 07 §7.5](../../chapters/07-web-transport.md#sec-7-5)
