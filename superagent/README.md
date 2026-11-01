# Super-agent 编排参照包

与 `miniharness/`、`webui/` 并列：镜像旁路 `dsh-superagent-prototype/src` 的 Engine、Store 与异步调度语义，供 [源码走读](../docs/design/super-agent/source_implement/index.md) 对照；进入 mini 产品仅经 [adopted.md](../docs/design/super-agent/adopted.md)。

- 不 `import miniharness`；不参与 A 线 wire 对齐。
- 领域层对齐旁路 `src/core/`（Engine、Repositories、Policy 端口）。
- `app/` 为应用层子集（SQLite Store、Effects、Passes、Runtime），不含 Cordis 与 npm `@deepseek-ai/dsh-*` 执行器。

## 布局

| 路径 | 说明 |
|------|------|
| `superagent/core/` | 领域模型与 Engine |
| `superagent/app/` | 持久化与调度 |
| `superagent/sources/` | Source 注册与测试用 FakeSource |
| `superagent/executor/` | FakeExecutor、LaunchDeliveryExecutor、ScriptedExecutor |
| `superagent/infra/` | 最小 Policy |

## 范围

| 包含 | 不包含 |
|------|--------|
| C1 域行为：`core-engine.spec.ts` + `stage.spec.ts` 对齐（`tests/superagent/`） | Cordis 插件、管理面板 `client/` |
| C2：`Runtime` + SQLite Store、Effects 提交后队列、Outbox `reply` effect | GitHub、飞书等真 Source API |
| C3：七条 flow 场景测、`ScriptedExecutor`、Fake Source 出站记录 | `DshExecutor` / 真实 DSH Session |
| `LaunchDeliveryExecutor`（launch/delivery 入队） | tools/http/views 管理面 |

## 测试与演示

在仓库根目录：

```sh
set PYTHONPATH=superagent
python -m unittest discover -s tests/superagent -t .
python examples/superagent_demo.py
python examples/superagent_demo.py --sqlite .data/superagent-demo.db
```
