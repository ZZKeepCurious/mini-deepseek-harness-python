# 走查 T1 · headless 一条任务

**动机**：确认 CLI 如何把任务文本接到 Agent loop，并在 stdout / 退出码上结算。

## 前置

- 仓库根目录，`pip install -e .`
- 可选：`DEEPSEEK_API_KEY`（无 key 时部分路径用假模型或跳过真 API 测试）

## 命令

```bash
python -m miniharness.cli --profile headless "echo hello via agent"
```

或使用教学扩展的会话子命令观察落盘目录：`miniharness sessions list`（数据在 `MINIHARNESS_HOME/sessions/`）。

## 预期

- 进程不开监听端口；stdout 出现最后一条非空 assistant 文本（带换行）。
- 退出码：`turn/end` 的 `kind` 为 `completed` → 0，否则 → 1（见 [章节 07 §7.2](../../chapters/07-headless.md#sec-7-2)）。

## 经过的 mini 模块

| 阶段 | 模块 |
|---|---|
| 参数与 profile | `miniharness/cli/main.py` |
| 一次性 runner | `miniharness/cli/headless.py` |
| 回合驱动 | `miniharness/core/agent_loop/agent.py` |
| 日志 | `miniharness/core/session/` |

## 日志中常见事件 type

`turn/start` → `step/start` → `user/message` → `assistant/message`（或 `assistant/attempt`）→ 可能有 `tool/call` / `tool/result` → `step/end` → `turn/end`。

## 测试

- `python -m unittest tests.test_cli -v`
- headless 集成见 `tests/test_credentials_adapter_wire.py` 等
