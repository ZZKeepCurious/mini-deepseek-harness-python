# 第 00 章：环境准备

> 目标：把 MiniHarness 跑起来，知道代码在哪里、测试怎么跑、每个文件对应什么。读完整本手册不装 Node.js、不装 TypeScript、不装任何 pip 包。

## 0.1 前置知识

- Python 3.10+。本教程只用标准库：`unittest`、`json`、`sqlite3`、`urllib`、`threading`、`dataclasses`。
- 基本的事件 / 回调 / 上下文概念。
- 了解一个 Agent 回合的 wire 形状：`user message → assistant（可能带 tool call）→ tool result → assistant`。

如果你不熟悉最后一条，先去报告第 6 章（学习路线图）把 P0 部分过一遍，那里有完整的回合样例。

## 0.2 目录结构

```
mini-deepseek-harness-python/        ← 仓库根
├── README.md                        ← 项目总览
├── ROADMAP.md                       ← 完整路线图（从 0 到 1 复现 dsh）
├── miniharness/                     ← 可运行代码包（随章节逐步构建，家族布局见 architecture.md §1）
│   ├── core/
│   │   ├── session/             ← 第 1 章：事件溯源会话
│   │   ├── scope.py             ← 第 2 章：Context + 事件总线 + 作用域
│   │   ├── tools.py             ← 第 3 章：工具注册表 + 执行管线
│   │   └── agent_loop/          ← 第 4 章：Agent Loop 状态机
│   ├── llm/                     ← 第 4 章：StreamChunk 协议 + 适配器 + 重试
│   ├── boot/                    ← 第 5 章：启动与组合
│   ├── seams/                   ← 第 6 章：进阶扩展口
│   ├── cli/ protocol/ preset/ extensions/ interaction/ client/
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

每个 `miniharness/` 文件的"第 N 章"标注，就是它对应的手册章节。想快速定位一个概念：先在手册找章节，再按章节号找文件。

## 0.3 跑起来

```bash
# 在仓库根目录
python -m unittest discover -s tests -t .   # 全部测试，应当 OK
python -m miniharness.demo                  # 端到端演示（假模型 + 工具 + 持久化）
```

如果 `python` 不是 3.10+，用 `python3`。跑完 `demo` 会输出一个完整回合的日志，最后提示"演示完成"并给出临时目录路径——那是一个可回放的会话存档，第 5 章会解释它。

## 0.4 三条学习纪律

1. **先跑测试，再读代码**。每个文件都配了验收测试，测试就是"始终成立的性质"清单。先看测试想验证什么，再去看实现，比顺着代码读效率高。
2. **每章完成"检查点练习"**。练习都是 10~20 行的小改动，改完要么让测试通过，要么新增测试钉住你的行为。
3. **每章末尾做"回到 dsh"对照**。打开真实仓库对应源码，只读关键 50 行，体会"约定一样、实现简化"在哪里。

## 0.5 简化立场：与上游的差异

MiniHarness 是教学实现，不是移植。下面是简化清单，每一条在对应章节都有详细说明。读手册前先扫一眼，避免在简化处花时间找"上游为什么没有"。

| MiniHarness 简化 | 真实 dsh |
|---|---|
| 同步事件总线（parallel 用 list 模拟） | 异步（`@deepseek-ai/cordis` 基于 fiber/async） |
| `provides` 声明式依赖 | apply 期间动态注册 |
| 同步路径工具逐工具串行执行（async 路径为并行池 + 串行屏障，第 12 章） | `isConcurrencySafe` 并行池 + 串行屏障 |
| 请求/回合同步阻塞式流式（不与在飞任务交错） | 逐 chunk durable 事件 |
| JSON/YAML 配置 + 补丁（pyyaml 可选，缺省退化 JSON；`!!js` 仅 `process.env.<NAME>` 子集） | YAML cordis.yml（同样的 id/insert/replace 语义） |
| LLM 失败以异常抛出（finish 带内 `{kind:'error'|'aborted'}` 与异常同走 `agent/request-error` waterfall） | `LlmError` 编码 `CONTEXT_WINDOW_EXCEEDED` / `EMPTY_RESPONSE`（可重试）等 |
| `agent/turn-stopping`、`system-prompt/assemble` waterfall 已实现（turn-stopping 为串行终点检查点，见第 4/13 章） | 上游都有 |
| JSONL 明文行；SQLite 列 `(session_id, seq, type, data)` | JSONL 默认 checksum+Zstandard 压缩；SQLite 列 `(session_id, seq, type, time, data, source_event_seqs, surface_op)` |
| `assistant/message` source 带 `{kind, provider, model}`（消息无 `id` 语义差异待核） | 消息 `{id, role, content, source}` 全字段 |

## 检查点

- [ ] `python -m unittest discover -s tests -t .` 全部通过
- [ ] `python -m miniharness.demo` 输出一次完整的回合日志
- [ ] 能说出 `core/session`、`core/scope`、`core/tools`、`llm/`、`core/agent_loop`、`core/session/persistence`、`boot/` 各自管什么