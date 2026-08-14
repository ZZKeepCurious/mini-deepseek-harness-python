# Mini DeepSeek Harness（Python）

[English](README.md) | 中文

**Mini DeepSeek Harness** 是用 **纯 Python 标准库** 从零复现 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（`dsh`）——由 [DeepSeek AI](https://deepseek.com) 开发的开源 Agent 运行时——的**教学实现**。

上游项目整个系统建立在一个设计哲学之上：**一切皆插件**（everything is a plugin），其底层是 [Cordis](https://github.com/cordiverse/cordis)——一个依赖注入 + 事件总线框架，设计思想见论文 [_A Programming Paradigm for Spatiotemporal Composability_](https://github.com/cordiverse/paper)。我们对这一设计深表敬意。本仓库是我们的致敬之作：不止于阅读，而是亲手用 Python 重建其核心契约——事件溯源会话日志、插件事件总线、turn/step Agent Loop、能力接缝三角色（Service Definition / Service Provider / Consumer）——**零第三方依赖**，任何有 `python3` 的人都可以阅读、运行和修改它们。

> **这是学习项目，不是移植。** 与 DeepSeek AI 官方无关联。我们不追求功能对齐或替代品；我们追求的是理解并讲清楚这些思想。

> **免责声明**：本仓库的相当一部分内容——包括分析报告与教程手册——是在 AI 助手的辅助下总结、撰写与复现的，可能对上游源码与文档存在误读或不准确之处。请以上游仓库 `deepseek-harness` 的源码与文档为唯一权威参考。

## 配套文档

两份互补文档：

- **[分析报告](docs/report/DEEPSEEK-HARNESS-DEEP-LEARNING-GUIDE.html)**——对上游仓库的深度剖析：五层架构、`ctx` 服务地图、技术核心、关键流程，全部配 Mermaid 图。
- **[step-by-step 手册](docs/chapters/)**——系统如何从 0 长出来，一章一个主题：概念 → 最小可运行代码 → 不变量/测试 → 检查点练习。

完整路线图见 [ROADMAP.md](ROADMAP.md)。

## 已实现能力

| 能力 | 状态 | 上游对应 |
|---|---|---|
| 事件溯源会话（seq、deep-freeze、`derive_messages`、interrupted 修复） | ✅ | `packages/core/session` |
| 持久化（JSONL / SQLite、flush 栅栏、fail-closed 加载、崩溃恢复） | ✅ | `packages/session/session-persistence` |
| 插件事件总线（emit / waterfall / parallel / serial、作用域、依赖驱动激活） | ✅ | `vendor/cordis` + `core/scope` |
| 工具注册表 + 执行管线（schema 校验、pre/execute/post、timeout） | ✅ | `packages/core/tools` |
| Agent Loop（turn/step 状态机、pre-step 拒绝、工具回灌续跑） | ✅ | `core/agent-loop` |
| LLM 接缝（StreamChunk 协议、假模型、DeepSeek 官方 SSE 适配器） | ✅ | `llm/llm` + `llm/llm-deepseek` |
| boot 与组合（`apply_patch` 补丁层叠、启动断言） | ✅ | `packages/boot` |
| 能力接缝基础版（沙箱 / 凭据 / 子 agent） | ◐ | capability seams 文档 |
| 异步总线、真并行工具 + 屏障 | ⏳ | `core/agent-loop` |
| CLI、YAML 配置、官方 SDK 互操作 | ⏳ | `apps/dsh`、`python/` |

状态：**62 个单元测试全部通过**（仅标准库）。

## 快速开始

要求：Python 3.10+，无需安装任何第三方包。

```sh
# 跑全部测试
python -m unittest discover -s tests -t .

# 端到端演示（假模型 + 工具 + 崩溃恢复，无需 API key）
python -m miniharness.demo

# 假模型多轮对话
python examples/chat_demo.py
```

### 接真实 DeepSeek API（可选）

```sh
export DEEPSEEK_API_KEY=sk-...            # PowerShell: set DEEPSEEK_API_KEY=sk-...
python examples/real_api_demo.py
```

### 安装为 CLI

```sh
pip install -e .
miniharness
```

## 目录结构

```
mini-deepseek-harness-python/
├── miniharness/          # 核心包（仅标准库）
│   ├── session.py        # 事件溯源会话、投影、不变量
│   ├── bus.py            # Context 注册库 / 事件总线 / 作用域 / 插件激活
│   ├── tools.py          # 工具注册表 + 执行管线
│   ├── llm.py            # StreamChunk 协议 + 假模型 / DeepSeek 适配器
│   ├── loop.py           # Agent Loop 状态机
│   ├── persistence.py    # JSONL / SQLite 双后端 + 崩溃恢复
│   ├── boot.py           # 启动 + 补丁层叠
│   ├── seams.py          # 沙箱 / 凭据 / 子 agent 接缝
│   └── demo.py           # 端到端演示
├── tests/                # 62 个验收测试（unittest）
├── examples/             # 对话 & 真实 API 示例
└── docs/
    ├── README.md         # 手册索引（学习地图）
    ├── chapters/         # 00-setup ~ 06-advanced-seams 教程
    └── report/           # 分析报告（HTML，Mermaid 图）
```

## 致谢

- [DeepSeek AI](https://deepseek.com) 与 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 团队：创造了这个系统并开源。
- [Cordis](https://github.com/cordiverse/cordis) 项目：本仓库复现的插件范式的源头。

## 许可

[MIT](LICENSE)
