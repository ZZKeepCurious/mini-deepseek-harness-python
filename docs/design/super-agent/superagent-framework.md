# Super-agent Python 参照框架

<p class="lead">仓库顶层 <code>superagent/</code> 与 <code>miniharness/</code>、<code>webui/</code> 同级，在 Python 中镜像旁路 <code>dsh-superagent-prototype/src</code> 的编排核（Engine、Store、Passes/Effects/Outbox），供阅读走读与对照旁路行为。</p>

## 边界

- 不是 A 线 wire 产物；默认不 import 进 `miniharness`。
- 进入 mini 产品能力的路径仅 [adopted.md](adopted.md) 显式登记后的立项。
- 源码叙事：[source_implement/index.md](source_implement/index.md)。

## 运行

```sh
set PYTHONPATH=superagent
python -m unittest discover -s tests/superagent -t .
python examples/superagent_demo.py
python examples/superagent_demo.py --sqlite .data/superagent-demo.db
```

## 包布局

| 目录 | 旁路对应 |
|------|----------|
| `superagent/superagent/core/` | `src/core/` |
| `superagent/superagent/app/` | `app/store`、`effects`、`passes`、`outbox`、`runtime` |
| `superagent/superagent/sources/` | `sources/registry`、FakeSource |
| `superagent/superagent/executor/` | FakeExecutor、`LaunchDeliveryExecutor`、`ScriptedExecutor` |
| `superagent/superagent/infra/` | 最小 Policy |

说明与范围表见仓库根目录 `superagent/README.md`（与本文同步维护）。
