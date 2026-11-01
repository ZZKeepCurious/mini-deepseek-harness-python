# 学习路径

<p class="lead">克隆本仓库时 <code>miniharness/</code> 已是完整运行时；手册既可<strong>走读对照</strong>成品代码，也可在独立目录<strong>空白复现</strong>章内骨架。系统模块关系见 <a href="../system-map.md">system-map.md</a>。</p>

## 两种模式

| 模式 | 适合 | 做法 | 验收 |
|---|---|---|---|
| **A 走读对照** | 快速吃透 mini 与上游约定 | 先跑该章测试 → 读 `miniharness/` 对应包 → 做检查点练习（改测试或小 patch） | 章末 `unittest` + 练习 |
| **B 空白复现** | 证明能亲手写出核心 | 另建空目录，只按章内代码骨架实现；完成一节再与 mini / 上游 diff | 与章内手算/测试用例一致 |

全绿 `python -m unittest discover -s tests -t .` 表示**仓库**对约定的回归，不等于你已亲手实现；亲手实现以章末练习与可选空白复现为准。

## 按目标选章

| 目标 | 建议章节 | 补充阅读 | Capstone |
|---|---|---|---|
| headless 闭环 + 持久化 | 00 → 01 → 04 → 05 → [07 §7.2](../chapters/07-headless.md#sec-7-2) | [trace T1](traces/headless-one-task.md) | `python -m miniharness.cli --profile headless "task"` |
| 浏览器 / wire 集成 | [07 §7.5](../chapters/07-web-transport.md#sec-7-5)、[wire-tour.md](wire-tour.md)、[interface-wire.md](../interface-wire.md) | T3、T4 | `--profile web` + 仓库根 `webui/README.md` |
| 工具 / 策略插件 | 02 → 03 → 06 | `tests/test_seams.py` | pre-execute waterfall 练习（章 02） |
| loader / preset / 组合 | 05 → 08 | report 04 议题 1 | `--dump-config` |
| 上游对照 | report 01～03 + 对应章「回到 dsh」 | 上游 `deepseek-harness/packages/` | 可选：官方 Python SDK（report 06） |
| Cordis / scope / 配置引擎 | 02 → 13 → 14 → 15 | architecture §2 | 各章单测 |

## 主线顺序（实现内核）

与 [system-map Fig S6](../system-map.md#fig-s6) 一致：

00 → 01 → 02 → 03 → 04 → 05 →（Capstone）→ 按需 06～15。

## Capstone 清单

在通过 05 章验收后，至少完成下列之一：

```bash
python -m miniharness.demo
python examples/chat_demo.py
python examples/plan_goal_demo.py --approve
python -m miniharness.cli --profile web   # 需 pip install ".[web]"
```

Web UI 开发：`cd webui && pnpm install && pnpm test && pnpm build`（见仓库根 `webui/README.md`）。

## 与报告路线图的关系

- 上游 monorepo 分阶段路线：[report/05 §6](../report/05-roadmap.md#roadmap-6)（P0～P6，pnpm 世界）。
- mini 分阶段路线：同页 **Mini P0～P5** 表（本仓库命令与路径）。
