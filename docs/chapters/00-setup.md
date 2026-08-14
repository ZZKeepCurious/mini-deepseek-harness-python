# 第 00 章：环境准备

> 目标：30 分钟内把 MiniHarness 跑起来，知道代码在哪里、测试怎么跑、每个文件对应什么。

## 0.1 前置知识（建议先过一遍报告第 6 章 P0）

- Python 3.10+（本教程只用标准库：`unittest`、`json`、`sqlite3`、`urllib`、`threading`、`dataclasses`）
- 基本的事件/回调/上下文概念
- 了解一个 Agent 回合的 wire 形状：`user message → assistant（可能带 tool call）→ tool result → assistant`

不需要 Node.js、不需要 TypeScript、不需要安装任何 pip 包。

## 0.2 目录结构

```
mini-deepseek-harness-python/        ← 仓库根
├── README.md                        ← 项目总览
├── ROADMAP.md                       ← 完整路线图（从 0 到 1 复现 dsh）
├── miniharness/                     ← 可运行代码包（随章节逐步构建）
│   ├── __init__.py              ← 汇总导出
│   ├── session.py               ← 第 1 章：事件溯源会话
│   ├── bus.py                   ← 第 2 章：Context + 事件总线 + 作用域
│   ├── tools.py                 ← 第 3 章：工具注册表 + 执行管线
│   ├── llm.py                   ← 第 4 章：StreamChunk 协议 + 适配器
│   ├── loop.py                  ← 第 4 章：Agent Loop 状态机
│   ├── persistence.py           ← 第 5 章：JSONL / SQLite + 恢复
│   ├── boot.py                  ← 第 5 章：启动与组合
│   ├── seams.py                 ← 第 6 章：进阶接缝
│   ├── example_plugins.py       ← 第 5 章 boot 演示插件
│   └── demo.py                  ← 端到端演示（无 key 可跑）
├── tests/                       ← 每章验收测试（unittest）
│   ├── test_session.py
│   ├── test_bus.py
│   ├── test_tools.py
│   ├── test_loop.py
│   ├── test_persistence_boot.py
│   └── test_seams.py
└── docs/                        ← 文档
    ├── README.md                ← 本手册索引（学习地图）
    ├── chapters/                ← 00-setup.md ~ 06-advanced-seams.md（本章所在目录）
    └── report/                  ← 《DeepSeek Harness 深度学习指南与技术报告》HTML
```

## 0.3 跑起来

```bash
# 在仓库根目录
python -m unittest discover -s tests -t .   # 全部 62 个测试，应当 OK
python -m miniharness.demo                  # 端到端演示（假模型 + 工具 + 持久化）
```

如果 `python` 不是 3.10+，用 `python3`。

## 0.4 三条学习纪律（贯穿全书）

1. **先跑测试，再读代码**：每个文件都配了验收测试，测试即"不变量清单"。先看测试想验证什么，再去看实现。
2. **每章完成"检查点练习"**：练习都是 10~20 行的小改动，改完必须让测试通过或新增测试钉住你的行为。
3. **每章末尾做"回到 dsh"对照**：打开真实仓库对应源码，只读关键 50 行，体会"契约一样、实现简化"在哪里。

## 0.5 我们的简化立场（诚实声明）

| MiniHarness 简化 | 真实 dsh |
|---|---|
| 同步事件总线（parallel 用 list 模拟） | 异步（`@deepseek-ai/cordis` 基于 fiber/async） |
| `provides` 声明式依赖 | apply 期间动态注册 |
| 工具并发执行退化为串行 | `isConcurrencySafe` 并行池 + 串行屏障 |
| 不逐 chunk 落 `assistant/chunk` | 每个 chunk 都是 durable 事件 |
| JSON 配置 + 补丁 | YAML cordis.yml（同样的 id/insert/replace 语义） |

简化不改变契约；每一处简化在对应章节都标注了。

## 检查点

- [ ] `python -m unittest discover -s tests -t .` 全部通过
- [ ] `python -m miniharness.demo` 输出一次完整的回合日志
- [ ] 能说出 `session.py / bus.py / tools.py / llm.py / loop.py / persistence.py / boot.py` 各自管什么
