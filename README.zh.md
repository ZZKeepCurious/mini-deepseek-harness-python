# Mini DeepSeek Harness（Python）

[English](README.md) | 中文

**Mini DeepSeek Harness** 是用 **Python（stdlib 优先，关键协议层精选第三方）** 从零复现 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（`dsh`，由 [DeepSeek AI](https://deepseek.com) 开发的开源 Agent 运行时）的**教学实现**。

上游项目整个系统建立在一个设计哲学之上：**一切皆插件**（everything is a plugin），其底层是 [Cordis](https://github.com/cordiverse/cordis)，一个依赖注入 + 事件总线框架，设计思想见论文 [_A Programming Paradigm for Spatiotemporal Composability_](https://github.com/cordiverse/paper)。我们对这一设计深表敬意。本仓库是我们的致敬之作：不止于阅读，而是亲手用 Python 重建其核心约定——事件溯源会话日志、插件事件总线、turn/step Agent Loop、能力扩展口三角色（Service Definition / Service Provider / Consumer），**stdlib 优先**（除 httpx 传输层与可选 pyyaml 外不依赖第三方），任何有 `python3` 的人都可以阅读、运行和修改它们。

> **这是学习项目，不是移植。** 与 DeepSeek AI 官方无关联。我们不追求功能对齐或替代品；我们追求的是理解并讲清楚这些思想。

> **免责声明**：本仓库的相当一部分内容（包括分析报告与教程手册）是在 AI 助手的辅助下总结、撰写与复现的，可能对上游源码与文档存在误读或不准确之处。请以上游仓库 `deepseek-harness` 的源码与文档为唯一权威参考。

## 配套文档

两份互补文档：

- **[分析报告](docs/report/index.md)**——对上游仓库的深度剖析：五层架构、`ctx` 服务地图、技术核心、关键流程，全部配 Mermaid 图（首页阅读地图 + 六个主题子页）。站点由 MkDocs 构建并部署到 GitHub Pages：https://zzkeepcurious.github.io/mini-deepseek-harness-python/
- **[step-by-step 手册](docs/chapters/)**——系统如何从 0 长出来，一章一个主题：概念 → 最小可运行代码 → 硬性规定/测试 → 检查点练习。

## 已实现能力

| 能力 | 上游对应 |
|---|---|
| 事件溯源会话（信封 `{type,seq,time,data}`、1 起 turn/step、deep-freeze、`derive_messages`、interrupted 修复） | `packages/core/session` |
| 持久化（JSONL / SQLite、header + `SESSION_FORMAT_VERSION=0` fail-closed、flush 栅栏、崩溃恢复） | `packages/session/session-persistence` |
| 插件事件总线（emit / waterfall / parallel / serial、作用域、依赖驱动激活） | `vendor/cordis` + `core/scope` |
| 工具注册表 + 执行管线（schema 校验、pre/execute/post、timeout） | `packages/core/tools` |
| Agent Loop（async 驱动 turn/step 状态机 + 同步门面、pre-step 拒绝、工具回灌续跑） | `core/agent-loop` |
| LLM 扩展口（async `stream(messages, tools, signal)` 契约、假模型、DeepSeek 官方 SSE 适配器（阻塞读经线程桥接）） | `llm/llm` + `llm/llm-deepseek` |
| 模型请求重试/退避（normal/always 策略、`agent/request-error`、`llm/retry` 审计对、事件驱动可取消等待） | `llm/llm-retry` + `llm/llm/src/retry-policy.ts` |
| token 计量（增量 fold、usage 折入锚、4 字符/token 启发式） | `llm/token-meter` |
| 上下文压缩（pre-step 压力 + `CONTEXT_WINDOW_EXCEEDED` 恢复、surface-replace 检查点事务） | `compaction/compaction-basic` |
| 后台作业（`job_output`/`job_list`/`job_kill`、完成 notice、per-owner 上限；无 `job/*` 会话事件） | `packages/jobs`（jobs-local + tool-jobs） |
| plan 模式（log-only `plan/mode` 状态、plan:policy prompt 分节注入、in-turn queued 提交） | `packages/plan/plan-mode` |
| plan 审查 UI（`/plan` 命令、`exit_plan_mode` 审查工具、userQuestions 通道、plan 投影单元） | `packages/plan/plan-mode` |
| 命令表面（`/` 命令注册表、`command/run` + `command/done` 配对） | `packages/interaction/commands` |
| 目标（`goal/change` 事件溯源 fold、GoalService、自动续跑 goal round、`get_goal`/`create_goal`/`update_goal` 三工具、`/goal` 命令） | `packages/goal`（goal + goal-round-driver + tool-goal + command-goal） |
| system prompt 分节（有序节注册 + 渲染进每次请求） | `core/system-prompt` |
| boot 与组合（YAML/JSON 补丁、`!!js` 环境变量插值、启动断言） | `packages/boot` |
| headless 一次性任务入口（`--profile headless "task"`：stdout 最终文本、退出码按 turn/end reason） | `packages/bundle/headless` + `apps/cli` |
| 启动器选项（`--patch`、`--dump-config` / `--dump-default-config`、只读组合导出） | `apps/cli/src/args.ts` |
| 会话管理服务（`ctx.sessions`：create/prepare/enter/announce 生命周期、fork 五错误码、flush 检查点、`session/created|disposed|event|flush` 四事件） | `packages/core/session`（SessionStore） |
| 会话管理 CLI（`miniharness sessions` 列表/恢复/删除；mini 教学扩展） | web 表面（上游） |
| 能力扩展口（沙箱后端 / 凭据四层 / 子 agent ACP+SDK+fork 三通道） | capability seams 文档 |
| 可继续子代理（`start_continuable`/`send_message`、durable 子会话 + 冷恢复、结算投递、异步事件驱动（A8：投递即返回 + watchSettlement + steer 批内合并）、`send_message`/`interrupt_agent`/`list_agents` 控制工具） | `packages/subagent`（subagent + subagent-in-process-driver + tool-subagent-control + tool-subagent-report） |
| 预设 / Agent 干预 / 轨迹折叠 / 动态插件 / 审批 | `packages/preset` + `core/agent` + `interaction` |
| 协议入口（ACP / JSON-RPC SDK / hooks 桥） | `acp` + `sdk` + `hooks` |
| 官方 Python SDK 互操作（上游 `DeepSeekHarness` 经 `launch_args_override` 驱动 mini worker；`tests/test_upstream_sdk_interop.py`，缺 pydantic/上游源码自动 skip） | `python/sdk` |
| 异步事件总线、真并行工具 + 屏障 | `core/agent-loop` |
| CI（GitHub Actions、Python 3.10~3.13、integration 标签真实 API 测试） | — |

规划中：web 表面降级后置。

状态：**834 个单元测试全绿**（stdlib 优先；`httpx` 承载 DeepSeek SSE 传输，可选 `pyyaml` 用于 YAML 配置）。

## 快速开始

要求：Python 3.10+，stdlib 优先（`httpx` 承载 DeepSeek SSE 传输，可选 `pyyaml` 用于 YAML 配置）。

```sh
# 跑全部测试
python -m unittest discover -s tests -t .

# 端到端演示（假模型 + 工具 + 崩溃恢复，无需 API key）
python -m miniharness.demo

# 假模型多轮对话
python examples/chat_demo.py

# plan + goal 演示（/plan、exit_plan_mode 审查、/goal、goal round 自动续跑）
python examples/plan_goal_demo.py --approve
# 一次性任务（对齐 `dsh --profile headless "task"`，需 DEEPSEEK_API_KEY）
python -m miniharness.cli --profile headless "run the tests"

# 只读组合导出（对齐 `dsh --dump-config`）
python -m miniharness.cli --dump-config

# 会话列表 / 恢复 / 删除
python -m miniharness.cli sessions

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
├── miniharness/             # 核心包（stdlib 优先，家族布局，见 docs/architecture.md）
│   ├── core/                # 上游 packages/core
│   │   ├── session/         # types / json / message / invariant / repair / surface / session
│   │   │   └── persistence.py
│   │   ├── scope.py         # Context / PluginManager
│   │   ├── tools.py         # 工具注册表 + 执行管线
│   │   └── agent_loop/      # agent.py + tool_calls.py
│   ├── llm/                 # 上游 packages/llm
│   │   ├── protocol.py      # StreamChunk / LlmAdapter / LlmFailure / BlockAssembler
│   │   ├── deepseek.py      # DeepSeek wire 序列化 + SSE 适配器
│   │   ├── fake.py          # FakeLlmAdapter（无 API key）
│   │   ├── retry_policy.py  # retry policy 解析（normal/always）
│   │   ├── retry.py         # agent/request-error 恢复 + 退避
│   │   └── token_meter.py   # TokenMeter 增量 fold + usage 折入锚
│   ├── compaction/          # 上游 packages/compaction
│   │   ├── engine.py        # pre-step 压力 + request-error overflow 恢复
│   │   ├── region.py        # selectCompactableRange + 检查点事务
│   │   ├── summarizer.py    # 前缀重放摘要 + 检查点框架
│   │   └── config.py        # 规格解析（threshold / retain / retries）
│   ├── boot/                # 上游 packages/boot
│   │   ├── boot.py          # 启动 + patch overlay
│   │   ├── composition.py   # YAML 配置 / !!js 插值 / dump 渲染
│   │   └── dotenv.py        # .env 解析（parse_dotenv）
│   ├── cli/                 # apps/cli
│   │   ├── main.py          # launcher 选项（profile / patch / dump）
│   │   ├── headless.py      # 一次性任务入口
│   │   ├── default_tools.py # headless 默认工具集
│   │   └── session_cmds.py  # 会话 list / resume / delete
│   ├── protocol/            # acp / sdk / hooks 桥
│   ├── seams/               # 沙箱 / 凭据 / 子 agent 扩展口
│   ├── preset/  extensions/  interaction/  client/
│   ├── demo.py              # 端到端演示
│   └── example_plugins.py   # boot 演示插件
├── tests/                   # 验收测试（unittest）
├── examples/                # 对话 & 真实 API 示例
└── docs/
    ├── index.md            # 手册索引（学习地图）
    ├── architecture.md      # 架构说明与上游对应
    ├── chapters/            # 00-setup ~ 12-handbook 教程
    └── report/              # 分析报告（MkDocs Markdown + Mermaid 图）
```

## 致谢

- [DeepSeek AI](https://deepseek.com) 与 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 团队：创造了这个系统并开源。
- [Cordis](https://github.com/cordiverse/cordis) 项目：本仓库复现的插件范式的源头。

## 许可

[MIT](LICENSE)