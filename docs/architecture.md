# MiniHarness 架构说明

> 本页是 `miniharness/` 代码自身的"建筑图纸"：目录怎么组织、每个文件对应上游什么、依赖方向规则、公共 API 边界。
> 读者是改代码的人，以及想理解仓库布局的学习者。
> 与其它文档的分工：`docs/report/` 解读上游系统"是什么、为什么"；`docs/chapters/` 教你怎么从 0 到 1 实现；本页回答"仓库里的代码本身怎么摆、凭什么这么摆"。

## 1. 目录组织

### 1.1 原则：目录按上游包家族镜像

代码目录镜像上游的包族结构（`packages/` 下的 `core/`、`llm/`、`boot/`、`sandbox/`……），镜像到**家族粒度**（两级子包），不镜像到每个包，也不按主题平铺：

- 家族级镜像：10 个左右的子包，维护成本低，"去哪个目录找什么"与上游一致；
- 文件级镜像：只做契约密集处（session、llm、agent-loop），这几处上游"一个文件一个职责"本身就是知识点；
- 为什么不是 1:1 镜像全部 50 多个包？Python 一个仓库拆 50 个目录，光 `__init__.py` 就有 50 个，对教学项目是过度工程；
- 为什么不是主题平铺？平铺表达不了模块边界：无法声明"哪些是契约、哪些是实现细节"，依赖方向无法用测试约束，环压力只能靠延迟导入绕。

### 1.2 目录树

```
miniharness/
├── __init__.py            # 教学面再导出，只含契约层（__all__ == 28，见 §6）
├── core/                  # packages/core
│   ├── session/           # Session 本体 + types/invariant/json/message/repair/surface，__init__.py 聚合
│   │   │                  #   message.py 上游在 llm/llm/src/message.ts，mini 保留会话域（L0 不依赖 llm，简化标注）
│   │   └── persistence.py # JSONL / SQLite 持久化（简化标注：上游在独立包组 packages/session）
│   ├── scope.py           # Context + PluginManager（vendor/cordis 语义）
│   ├── tools.py           # 工具注册表 + 执行管线
│   └── agent_loop/        # agent.py（turn/step 状态机）+ tool_calls.py（并行调度）
├── llm/                   # packages/llm
│   ├── protocol.py        # StreamChunk / LlmAdapter / LlmFailure / BlockAssembler（协议层）
│   ├── deepseek.py        # DeepSeek wire 序列化 + SSE 适配器
│   ├── fake.py            # FakeLlmAdapter（教学扩展）
│   ├── retry_policy.py    # retry policy 解析（normal/always）
│   └── retry.py           # agent/request-error 恢复 + 退避
├── boot/                  # packages/boot
│   ├── boot.py            # 启动 + patch overlay
│   ├── composition.py     # YAML 配置 / !!js 插值 / dump 渲染
│   └── dotenv.py          # .env 解析（parse_dotenv）
├── cli/                   # apps/cli
│   ├── main.py            # launcher 选项（profile / patch / dump）
│   ├── headless.py        # 一次性任务入口
│   ├── default_tools.py   # headless 默认工具集（教学扩展）
│   └── session_cmds.py    # 会话 list / resume / delete（教学扩展）
├── preset/                # packages/preset + apps/cli/config/agent-presets
│   └── presets.py         # （数据目录 preset/{minimal,standard} 随迁）
├── extensions/            # packages/extensions
│   └── dynamic.py         # 动态插件生命周期
├── interaction/           # packages/interaction
│   └── approval.py        # 审批服务
├── protocol/              # packages/{acp, sdk, hooks}
│   ├── acp.py             # ACP 服务器子集
│   ├── sdk.py             # JSON-RPC 信封 + 最小运行服务
│   └── hooks.py           # hooks 桥（CC 配置 → 拦截决策）
├── seams/                 # packages/{sandbox, credentials, subagent}
│   ├── sandbox_local.py   # 真沙箱后端（平台链探测 / 失败即拒绝）
│   ├── credentials_local.py # 凭据四层
│   └── subagent/          # 协议面 __init__ + providers.py（三通道）+ worker.py（子进程）
├── client/                # packages/client
│   └── trajectory.py      # Trajectory 折叠引擎
├── demo.py                # 端到端演示（教学入口，python -m miniharness.demo）
└── example_plugins.py     # boot 演示插件（教学示例）
```

### 1.3 顶层再导出策略

- `miniharness/__init__.py` 保留"教学再导出"：`from miniharness import Session` 对学习者成立；
- 子包 `__init__.py` 做族内再导出：`from miniharness.llm import StreamChunk` 与 `from miniharness.llm.protocol import StreamChunk` 等价。文档与示例写浅路径，业务代码写深路径（可被依赖方向测试检查）；
- **聚合器必须显式 `__all__`**：无 `__all__` 时 `from .x import *` 会把子模块命名空间的所有公开名复制进包——包括与子模块同名的属性（子模块属性遮蔽包引用），也包括子模块内部导入的 stdlib 名（如 `json.py` 里的 `import json`）。星号导入只应复制契约名，因此子包与其子模块各写显式 `__all__`；
- 命名沿用上游：`sdk.py` 与上游包名一致；`session_cmds.py` 与 `sessions.py` 单复数混淆消除。

## 2. 模块 ↔ 上游映射

行级对照账本：每一行是"mini 路径 ↔ 上游对应"的权威归属。简化标注以各模块 docstring 为准，本表只列归属；改公共代码时先查本表。

| mini 路径 | 上游对应（唯一权威） | 备注 |
|---|---|---|
| `core/session/`（session + types/invariant/json/message/repair/surface，共 7 文件） | `packages/core/session/src/`（types/invariant/json/repair/surface 等 10 文件，index.ts 聚合）+ `packages/llm/llm/src/message.ts` | message 构造保留在会话域（L0 不依赖 llm，简化标注） |
| `core/session/persistence.py` | `packages/session/session-persistence-{jsonl,sqlite}` | 上游是独立包组，mini 并入会话域（简化标注） |
| `core/scope.py` | `vendor/cordis` + `packages/core/scope` | |
| `core/tools.py` | `packages/core/tools` | |
| `core/agent_loop/agent.py` | `packages/core/agent-loop/src/agent.ts` | |
| `core/agent_loop/tool_calls.py` | `packages/core/agent-loop/src/tool-calls.ts` | |
| `llm/protocol.py` | `packages/llm/llm/src/` | |
| `llm/deepseek.py` | `packages/llm/llm-deepseek/src/` | |
| `llm/fake.py` | 无 | 教学扩展 |
| `llm/retry_policy.py` | `packages/llm/llm/src/retry-policy.ts` | |
| `llm/retry.py` | `packages/llm/llm-retry/src/` | |
| `boot/boot.py` | `packages/boot/app-boot` | |
| `boot/composition.py` | `packages/boot/app-boot` + `apps/cli/src/args.ts` | |
| `boot/dotenv.py` | `packages/boot/app-boot`（loadEnv） | |
| `cli/main.py` | `apps/cli/src/args.ts` | |
| `cli/headless.py` | `bundle/headless` + `apps/cli` | |
| `cli/default_tools.py` | 无 | 教学扩展（上游是工具插件注册） |
| `cli/session_cmds.py` | 无 | 教学扩展（上游会话管理在 web 表层） |
| `preset/presets.py` | `packages/preset` + `apps/cli/config/agent-presets` | 数据目录 `preset/{minimal,standard}` |
| `extensions/dynamic.py` | `packages/extensions/*` | |
| `interaction/approval.py` | `packages/interaction/user-approval` | |
| `client/trajectory.py` | `packages/client/ui-trajectory` | |
| `protocol/acp.py` | `packages/acp/acp` | |
| `protocol/sdk.py` | `packages/sdk/protocol` + `sdk/server` | |
| `protocol/hooks.py` | `packages/hooks/hook-protocol` + `hooks-claude-code` | |
| `seams/sandbox_local.py` | `packages/sandbox/sandbox-local` + `sandbox-windows-acl` | |
| `seams/credentials_local.py` | `packages/credentials/credentials-local` | |
| `seams/subagent/`（`__init__.py` + `providers.py` + `worker.py`） | `packages/subagent/subagent` + `subagent-fork-in-process` + `-acp` + `-dsh-sdk` | |
| `demo.py` | `examples/agent-spine-demo` | 教学入口，保留顶层（`python -m miniharness.demo`） |
| `example_plugins.py` | `examples/` | 教学示例，保留顶层 |

## 3. 依赖方向规则

分层如下（Python 没有编译期模块边界，规则由 `tests/test_dependencies.py` 的 import 方向断言钉死，违反即测试失败）：

| 层 | 内容 | 允许依赖 |
|---|---|---|
| L0 地基 | `core/session`、`core/scope` | 无（两者互不依赖） |
| L1 领域 | `llm/*`、`core/tools`、`core/session`、`boot/*` | 仅 L0 |
| L2 编排 | `core/agent_loop` | L0 + L1 |
| L3 应用与入口 | `cli/*`、`protocol/*`、`seams/*`、`preset`、`extensions`、`interaction`、`client` | L0 ~ L2 |
| 教学层 | `demo.py`、`example_plugins.py` | 任意层，但不得被业务模块依赖 |

规则：

1. L_n 只依赖 L_{&lt;n}，禁止依赖同层或上层。两条显式例外：
   - `seams/subagent/worker.py` 依赖 `protocol/*`（同层）：worker 是 ACP / SDK 线协议的服务端载体，复用协议层的帧与信封实现；
   - `cli/main.py` 依赖 `demo`（教学层）：无 profile 时以 `demo` 兜底（教学扩展入口）。
2. `protocol/` 内三个模块互不依赖（acp、sdk、hooks 各自独立）。
3. `seams/` 内 sandbox、credentials、subagent 三个子域互不依赖。
4. `seams/credentials_local.py` 从 `boot/dotenv.py` 导入 `parse_dotenv`（L3 → L1）：凭据文档解析复用 boot 层的 `.env` 解析器，方向合法。

## 4. 公共 API 面

**白名单（契约层，改它需要对照上游 + 更新差异清单）**：`Session`、`Context`、`PluginManager`、`Tool`、`ToolRegistry`、`AgentLoop`、`StreamChunk`、`LlmAdapter`、`DeepSeekAdapter`、`LlmFailure`、`SessionPersistence`/`JsonlPersistence`/`SqlitePersistence`、`apply_patch`、`boot`、`run_headless`、`create_message` 与四个 block 构造、`derive_messages`、`turn_balance`、`repair_interrupted_turn`、`SESSION_FORMAT_VERSION`、`TOOL_NOT_STARTED`、`TOOL_OUTCOME_UNKNOWN`。

**黑名单（内部工具，不在顶层 `__all__`，只允许深路径 import）**：`deep_freeze`、`thaw`、`is_json_safe`、`now_ms`、`_http_error_code`、`_map_finish_reason`、`load_events_checked`、`repair_and_replay`、`balanced_after_replay`。

**教学扩展（上游无对应，标注于此）**：`cli/default_tools.py`、`cli/session_cmds.py`（含 `--config` 子命令）、`llm/fake.py`、`demo.py`、`example_plugins.py`。

顶层 `__all__` 收敛至 28 项（白名单 + `FakeLlmAdapter`），由 `tests/test_dependencies.py` 断言钉死；白名单每一项都能在 §2 映射表里找到上游对应。