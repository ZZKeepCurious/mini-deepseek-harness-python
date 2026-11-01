# 走查 T2 · 持久化、kill 与恢复

**动机**：确认 JSONL（及 generation 文件名）是 Session 的唯一磁盘真相源，崩溃后可 repair 并继续。

## 前置

- 第 05 章概念；`pip install -e .`
- 可选：设 `MINIHARNESS_HOME` 指向可写目录

## 命令

```bash
python -m miniharness.demo
```

记下输出里的会话目录（或在 `MINIHARNESS_HOME/sessions/` 下找最新 id）。对未闭合 turn 的文件，用同 id 恢复：

```bash
python -m miniharness.cli --profile headless --session <SESSION_ID> "continue after repair"
```

（教学扩展：`python -m miniharness.cli sessions list` 列目录，见 `cli/session_cmds.py`。）

## 预期现象

- 回放后 turn 括号平衡（`turn_balance >= 0`）；尾部未闭合 turn 经 `repair_interrupted_turn` 合成 `turn/end {kind:'interrupted'}`。
- fail-closed：未知事件 type 拒读（`ignorable` 豁免见第 05 章）。
- headless 退出码与 `turn/end` 的 `kind` 一致（completed → 0）。

## 经过的 mini 模块

| 模块 | 职责 |
|---|---|
| `core/session/persistence.py` | append、flush |
| `core/session/generation.py` | 多代文件名 |
| `core/session/released/` | 迁移链读入 |
| `boot/boot.py` | 启动时回放 |

## 日志中常见事件 type

`turn/start` → … →（中断时可能缺 `turn/end`）→ repair 追加 `step/end` / `turn/end {kind:'interrupted'}` → 恢复后继续 `turn/start` …

## 测试

```bash
python -m unittest tests.test_persistence_boot -v
python -m unittest tests.test_session -v
```

## 关联

[system-map Fig S3](../../system-map.md#fig-s3) · [章节 05](../../chapters/05-persistence-recovery-composition.md)
