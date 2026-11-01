# 走查 T4 · Web follow 与审批

**动机**：确认 Remote 流（mux + follow）与控制面（`$events` + `$events/result`）如何结算工具 ask，而不在浏览器侧另存审批状态。

## 前置

`pip install -e ".[web]"`；概念见 [章节 09](../../chapters/09-agent-intervention.md) 与 [interface-wire §6](../../interface-wire.md#wire-6)。

## 命令

```bash
python -m unittest tests.test_web_approvals tests.test_web_events tests.test_web_mux -v
cd webui && pnpm test
```

## 预期现象

- WS `/api/remote.mux` 上 `session/follow` 先 snapshot 再按 `seq` 递增推送；客户端去重（见 `webui/src/wire/follow.ts`）。
- 工具 ask 经 `$events` waterfall 到客户端；POST `$events/result` 后工具继续或 denied；Session 落 `approval/asked` + `approval/decided`。
- fail-closed：非法审批 outcome → `unavailable`，不误放行（wire §10）。

## 机制（缩略）

```mermaid
sequenceDiagram
  participant Client as webui_or_test_client
  participant Mux as web_mux
  participant Loop as agent_loop
  participant Ev as web_events
  Client->>Mux: WS remote.mux open session/follow
  Mux->>Client: snapshot and event frames by seq
  Loop->>Ev: approval or tool ask projection
  Client->>Ev: POST api events/result
  Ev->>Loop: resume tool or deny
```

1. **follow**：`session/follow` 流（§4.1）。
2. **control**：`session/control` 宿主级 live control（§4.2）。
3. **审批**：`tools/pre-execute` ask → web 桥 → `$events/result`（§6）。

## 经过的 mini 模块

`web/mux.py` · `web/streams.py` · `web/events.py` · `web/approvals.py` · `interaction/approval.py`

## 日志 / 事件 type 清单

Session：`approval/asked`、`approval/decided`、`tool/call`、`tool/result`（deny 路径）。Remote：`$events` ready/emit/waterfall/cancel 帧（见 wire §4.3）。

## 测试

`tests.test_web_approvals` · `tests.test_web_events` · `tests.test_web_mux` · webui vitest

## 关联

[system-map Fig S4](../../system-map.md#fig-s4)
