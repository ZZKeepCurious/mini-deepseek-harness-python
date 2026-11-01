# 走查 T5 · SDK stdio JSON-RPC

**动机**：确认 `protocol/sdk.py` 的线协议与官方 Python SDK 期望的帧序一致（进程级客户端，非浏览器）。

## 前置

- `pip install -e .`
- 协议细节：[章节 07 §7.6](../../chapters/07-sdk.md#sec-7-6)

## 命令

```bash
python -m unittest tests.test_sdk_protocol -v
```

进程级 stdio 由 `miniharness` SDK 子命令 / worker 启动（见 §7.6.4 与 `seams/subagent/worker.py`）；日常以单测模拟 stdin/stdout 帧序。

## 预期现象

- 换行分隔 JSON-RPC：请求 `session/prompt` 等 → 通知流 `session.event` → 最终 prompt 响应。
- idle 时结算 `session.status`；错误帧与上游 SDK 期望一致（fail-closed）。

## 机制

1. Worker 从 stdin 读换行分隔 JSON-RPC；`SdkRuntime` 处理 `session/prompt` 等最小方法集。
2. 回合内先推送 `session.event` 通知，再返回 prompt 响应；`session.status` 在 idle 时结算（见 §7.6.4）。

## 日志 / 通知 type

JSON-RPC 通知侧常见 `session.event`（会话日志事件投影）；方法响应侧 `session.status` 与 prompt 结果体（字段见 `protocol/sdk.py`）。

## 测试

`tests.test_sdk_protocol`；可选 `tests/test_upstream_sdk_interop.py`（需上游 SDK + pydantic，缺则 skip）。

## 经过的 mini 模块

`miniharness/protocol/sdk.py` · `miniharness/seams/subagent/worker.py`（sdk 子命令）· `core/agent_loop/`

## 关联

[report 06 §9](../../report/06-appendix.md)（官方 SDK 与 dsh 关系）
