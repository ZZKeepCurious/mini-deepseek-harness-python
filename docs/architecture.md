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
│   ├── session_store.py   # SessionStore（ctx.sessions 服务：create/prepare/enter/announce + fork + flush）
│   ├── scope.py           # Context + PluginManager（vendor/cordis 语义）
│   ├── tools.py           # 工具注册表 + 执行管线
│   ├── system_prompt.py   # SystemPromptService（分节渲染，systemPrompt 服务）
│   └── agent_loop/        # agent.py（turn/step 状态机）+ tool_calls.py（并行调度）
├── llm/                   # packages/llm
│   ├── protocol.py        # StreamChunk / LlmAdapter / LlmFailure / BlockAssembler（协议层）
│   ├── deepseek.py        # DeepSeek wire 序列化 + SSE 适配器
│   ├── fake.py            # FakeLlmAdapter（教学扩展）
│   ├── retry_policy.py    # retry policy 解析（normal/always）
│   ├── retry.py           # agent/request-error 恢复 + 退避
│   └── token_meter.py     # TokenMeter 增量 fold + usage 折入锚
├── attachment/             # packages/attachment（attachment + attachment-local）
│   ├── types.py            # ImageAttachmentRef / SaveImageAttachment / ImageAttachmentLimits
│   ├── error.py            # AttachmentError + 13 错误码 + is_image_admission_error
│   ├── image.py            # 光栅探测（纯 stdlib 头部解析，简化标注）
│   └── store.py            # LocalAttachmentStore（sha256 内容寻址 + 完整性复验）
├── compaction/            # packages/compaction
│   ├── config.py          # 压缩规格解析（threshold / retain / retries）
│   ├── region.py          # selectCompactableRange + 压缩事务（surface replace 检查点）
│   ├── summarizer.py      # 前缀重放摘要 + 检查点框架
│   └── engine.py          # BasicCompactionEngine（pre-step 压力 / request-error overflow 接线）
├── jobs/                  # packages/jobs（seam + jobs-local + tool-jobs）
│   ├── types.py           # 终态 / 常量 / JobDoneBox（done 的 Promise 替身）
│   ├── registry.py        # LocalJobRegistry（ctx.jobs 服务 + owner 栅栏 + 结算/上限/teardown）
│   └── tools.py           # job_output / job_list / job_kill + 完成 notice 投递 + 字节封顶
├── plan/                  # packages/plan/plan-mode（状态机 + 审查 UI + 投影）
│   ├── config.py          # plan-mode 规格解析（section 校验，fail loud）
│   ├── mode.py            # PlanModeController（log-only plan/mode + plan:policy 节 + pre-step 提交）
│   ├── review.py          # exit_plan_mode 工具 + /plan 命令 + userQuestions 审查通道
│   └── projection.py      # plan 投影单元（command/run ↔ plan/mode 双事件折叠）
├── commands/              # packages/interaction/commands（命令契约）
│   └── __init__.py        # CommandRegistry + command/run|done 配对 + parse/route
├── goal/                  # packages/goal（goal + goal-round-driver + tool-goal + command-goal）
│   ├── domain.py          # goal/change 事件严格重放 fold + GoalError
│   ├── service.py         # GoalService（ctx.goals：compare-and-set 变更 + 激活）
│   ├── prompt.py          # goal round 提示词
│   ├── driver.py          # pull 式 GoalDriver（pre-step reservation 校验 + continue_rounds）
│   ├── tools.py           # get_goal / create_goal / update_goal + tool:goal 节
│   └── commands.py        # /goal 命令表面
├── skills/                # packages/skill（skill + skill-filesystem + tool-skill）
│   ├── registry.py        # SkillRegistry（ctx.skills 服务 + 分层注册 + 渲染/digest）
│   ├── filesystem.py      # FileSystemSkillProvider（六类根 + frontmatter）
│   └── tool_skill.py      # skill 工具 + /名字 手势 + durable catalog 注入
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
| `core/session_store.py` | `packages/core/session/src/index.ts`（SessionStore 部分） | 内存会话服务：create/prepare/enter/announce 生命周期 + get/list/fork（五错误码）+ flush 检查点 + `session/created|disposed|event|flush` 四事件；无 typert lookup、无 scope 过滤、flush 为同步近似（简化标注见模块 docstring） |
| `core/scope.py` | `vendor/cordis` + `packages/core/scope` | |
| `core/tools.py` | `packages/core/tools` | |
| `core/agent_loop/agent.py` | `packages/core/agent-loop/src/agent.ts` | 单一 async 驱动（`_pump_async`/`_run_step_async`）+ `followup`/`steer` 同步门面（无 driver 时经 `asyncio.run` 瞬态事件循环）+ 协作式取消（`_cancel_event` 每轮新建 + `call_soon_threadsafe` 跨线程置位）；agent/pre-step 决策经 `awaterfall` |
| `core/agent_loop/tool_calls.py` | `packages/core/agent-loop/src/tool-calls.ts` | |
| `core/agent_loop/inbox.py` | `packages/core/agent-loop/src/inbox.ts` | 双队列（followup→next-turn / steer→next-step）+ `agent/inbox/spliced` 持久化 |
| `core/system_prompt.py` | `packages/core/system-prompt/src/` | assemble waterfall + contexts/tools/variables 提供器 + `{{variable}}` 严格插值；scope 层叠、运行时上下文快照注入请求历史、assembly.tools→请求工具集成未复现（简化标注见模块 docstring） |
| `llm/protocol.py` | `packages/llm/llm/src/` | `stream(messages, tools, signal)` async 契约 + `StreamAborted` + `_aiter_from_thread` 线程桥（2026-08-18 asyncio 化重构） |
| `llm/deepseek.py` | `packages/llm/llm-deepseek/src/` | 阻塞 SSE 读经 executor 线程桥接为异步迭代（阻塞读不可中断，urlopen 120s 超时兜底；上游 fetch + AbortSignal） |
| `llm/fake.py` | 无 | 教学扩展 |
| `attachment/`（types + error + image + store） | `packages/attachment/attachment`（seam + types + error）+ `packages/attachment/attachment-local`（store + image） | 纯 stdlib 头部解析（上游 sharp 全解码）、普通写 + os.replace（上游 fsync + link 原子发布）、显式 root（上游 DSH_HOME/attachments/v1）；简化标注见模块 docstring |
| `llm/retry_policy.py` | `packages/llm/llm/src/retry-policy.ts` | |
| `llm/retry.py` | `packages/llm/llm-retry/src/` | async 恢复决策 + 事件驱动可取消等待（`asyncio.wait`；无 `.event` 信号回退轮询） |
| `llm/token_meter.py` | `packages/llm/token-meter/src/` | |
| `compaction/`（config + region + summarizer + engine） | `packages/compaction/compaction-basic/src/`（config / region / summarizer / index.ts） | 前缀重放无 KV cache 语义、无 toolResultPruner（简化标注见模块 docstring） |
| `jobs/`（types + registry + tools） | `packages/jobs/`（seam + jobs-local + tool-jobs） | `run_in_background` 触发入口未复现；无 scope 链/agent registry；execute 直接返回渲染文本（简化标注见模块 docstring） |
| `plan/`（config + mode + review + projection） | `packages/plan/plan-mode/src/` | 状态机 + plan:policy 节 + 审查 UI（exit_plan_mode / /plan / userQuestions）+ plan 投影；无 canonical value / presentCall（简化标注见模块 docstring） |
| `commands/` | `packages/interaction/commands/src/` | 命令注册/派发 + `command/run|done` 配对；无 commands/change 通知（简化标注见模块 docstring） |
| `goal/`（domain + service + prompt + driver + tools + commands） | `packages/goal/`（goal + goal-round-driver + tool-goal + command-goal） | 无 agent registry / Typert remote；push→pull 驱动；权威判定近似（简化标注见模块 docstring） |
| `skills/`（registry + filesystem + tool_skill） | `packages/skill/`（skill + skill-filesystem + tool-skill） | 无 chokidar watch、无 ctx.fs 适配、错误 `ValueError: ` 前缀、execute 直接返回渲染文本（简化标注见模块 docstring） |
| `boot/boot.py` | `packages/boot/app-boot` | |
| `boot/composition.py` | `packages/boot/app-boot` + `apps/cli/src/args.ts` | |
| `boot/dotenv.py` | `packages/boot/app-boot`（loadEnv） | |
| `cli/main.py` | `apps/cli/src/args.ts` | |
| `cli/headless.py` | `packages/bundle/headless` + `apps/cli` | |
| `cli/default_tools.py` | 无 | 教学扩展（上游是工具插件注册） |
| `cli/session_cmds.py` | 无 | 教学扩展（上游会话管理在 web 表层） |
| `preset/presets.py` | `packages/preset` + `apps/cli/config/agent-presets` | 数据目录 `preset/{minimal,standard}` |
| `extensions/dynamic.py` | `packages/extensions/*` | |
| `interaction/approval.py` | `packages/interaction/user-approval` | |
| `client/trajectory.py` | `packages/client/ui-trajectory` | |
| `protocol/acp.py` | `packages/acp/acp` | |
| `protocol/sdk.py` | `packages/sdk/protocol` + `sdk/server` | messageId 为真实消息 id（与 inbox 回执一致，官方 SDK 依赖）；互操作测试 `tests/test_upstream_sdk_interop.py`（需 pydantic + 上游 SDK 源码，缺则 skip） |
| `protocol/hooks.py` | `packages/hooks/hook-protocol` + `hooks-claude-code` | |
| `seams/sandbox_local.py` | `packages/sandbox/sandbox-local` + `sandbox-windows-acl` | |
| `seams/credentials_local.py` | `packages/credentials/credentials-local` | |
| `seams/subagent/`（`__init__.py` + `providers.py` + `worker.py` + `continuation.py` + `descriptor.py`） | `packages/subagent/subagent` + `subagent-fork-in-process` + `-acp` + `-dsh-sdk` + `subagent-spawn-in-process` + `subagent-in-process-driver` + `tool-subagent-control` + `tool-subagent-report` | 续跑 A8 为异步事件驱动（双路径：父有 driver → 投递即返回 + watchSettlement 结算 + steer 批内合并；无 driver → 回退同步 pump）。简化见 AGENTS.md 差异清单 |
| `demo.py` | `packages/examples/agent-spine-demo` | 教学入口，保留顶层（`python -m miniharness.demo`） |
| `example_plugins.py` | `examples/` | 教学示例，保留顶层 |

## 3. 依赖方向规则

分层如下（Python 没有编译期模块边界，规则由 `tests/test_dependencies.py` 的 import 方向断言钉死，违反即测试失败）：

| 层 | 内容 | 允许依赖 |
|---|---|---|
| L0 地基 | `core/session`、`core/scope` | 无（两者互不依赖） |
| L1 领域 | `llm/*`、`core/tools`、`core/system_prompt`、`core/session_store`、`attachment`、`boot/*` | 仅 L0 |
| L2 编排 | `core/agent_loop`、`compaction`、`jobs`、`plan`、`commands`、`goal`、`skills` | L0 + L1 |
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

**教学扩展（上游无对应，标注于此）**：`cli/default_tools.py`、`cli/session_cmds.py`（会话管理子命令；`--config` 属 `cli/main.py` 启动器标志，同为教学扩展）、`llm/fake.py`、`demo.py`、`example_plugins.py`。

顶层 `__all__` 收敛至 28 项（白名单 + `FakeLlmAdapter`），由 `tests/test_dependencies.py` 断言钉死；白名单每一项都能在 §2 映射表里找到上游对应。

**深路径契约（不在顶层 `__all__`，仅经子包深路径暴露，由 `tests/test_token_meter.py`、`tests/test_compaction.py`、`tests/test_jobs.py`、`tests/test_plan.py`、`tests/test_skills.py`、`tests/test_session_store.py` 钉死行为）**：`TokenMeter`、`install_compaction`、`CompactionEngine`、`compact_surface_region`、`select_compactable_range`、`inspect_compaction_entry_state`、`frame_summary`、`install_jobs`、`register_job_tools`、`LocalJobRegistry`、`JobDoneBox`、`fit_with_suffix`、`fit_completion_notice`、`install_system_prompt`、`SystemPromptService`、`install_plan_mode`、`PlanModeController`、`fold_plan_mode`、`resolve_config`、`install_skills`、`register_skill_tools`、`SkillRegistry`、`FileSystemSkillProvider`、`SkillTool`、`SKILL_GESTURE`、`render_skill_content`、`parse_skill_file`、`digest_catalog_entries`、`install_sessions`、`SessionStore`、`SessionForkError`、`SESSION_NOT_FOUND`、`SESSION_NOT_LIVE`、`SESSION_ALREADY_EXISTS`、`INVALID_BOUNDARY`、`OPEN_TURN`、`LocalAttachmentStore`、`AttachmentStore`、`ImageAttachmentRef`、`SaveImageAttachment`、`ImageAttachmentLimits`、`AttachmentError`、`is_image_admission_error`、`detect_image`、`probe_image`、`supports_acp_image_prompts`、`admit_acp_prompt`、`assistant_block_to_acp`。装配约定：`apply_retry_planner(ctx)` → `install_compaction(ctx)` → `install_jobs(ctx)` → `install_system_prompt(ctx)` →（可选）`install_plan_mode(ctx, config)` →（可选）`install_skills(ctx)` →（可选）`install_sessions(ctx)`（均幂等；`CONTEXT_WINDOW_EXCEEDED` 不在重试白名单，由压缩接管；作业工具注册经 `register_job_tools(reg, ctx.inject("jobs"))`，`default_tools` 在 `ctx.jobs` 存在时自动收编；skill 工具注册经 `register_skill_tools(reg, ctx.inject("skills"))`，`default_tools` 在 `ctx.skills` 存在时自动收编；plan 依赖 systemPrompt 服务，缺失 fail loud；会话经 `install_sessions(ctx)` 提供 `ctx.sessions`，headless / demo / resume 入口已接入）。