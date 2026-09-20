# MiniHarness 架构说明

> 本页是 `miniharness/` 代码自身的"建筑图纸"：目录怎么组织、每个文件对应上游什么、依赖方向规则、公共 API 边界。
> 读者是改代码的人，以及想理解仓库布局的学习者。
> 与其它文档的分工：`docs/report/` 解读上游系统"是什么、为什么"；`docs/chapters/` 教你怎么从 0 到 1 实现；本页回答"仓库里的代码本身怎么摆、凭什么这么摆"。

## 1. 目录组织

### 1.1 原则：目录按上游包家族镜像

代码目录镜像上游的包族结构（`packages/` 下的 `core/`、`llm/`、`boot/`、`sandbox/`……），镜像到**家族这一级**（两级子包），不镜像到每个包，也不按主题平铺：

- 家族级镜像：约 20 个子包，维护成本低，"去哪个目录找什么"与上游一致；
- 文件级镜像：只做约定密集处（session、llm、agent-loop），这几处上游"一个文件一个职责"本身就是知识点；
- 为什么不是 1:1 镜像全部近 50 个包？Python 一个仓库分成 50 个目录，光 `__init__.py` 就有 50 个，对教学项目是过度工程；
- 为什么不是主题平铺？平铺表达不了模块边界：无法声明"哪些是约定、哪些是实现细节"，依赖方向无法用测试约束，环压力只能靠延迟导入绕。

### 1.2 目录树

```
miniharness/
├── __init__.py            # 教学面再导出，只含约定层（__all__ == 28，见 §4）
├── core/                  # packages/core
│   ├── session/           # Session 本体 + types/invariant/json/message/repair/surface/projections，__init__.py 聚合
│   │   │                  #   message.py 上游在 llm/llm/src/message.ts，mini 保留会话域（L0 不依赖 llm，简化标注）
│   │   ├── projections.py # message 投影（SessionMessageProjection）：image/offload 的拆分校验 + 图片卸载覆盖表（对应 session/surface.ts + compaction-image-offload/projection.ts）
│   │   ├── zstd_frames.py # zstd 拼接帧容器扫描/解码/截断前缀恢复（python-zstandard）
│   │   ├── persistence.py # JSONL(zstd 帧容器/明文) / SQLite 持久化（上游独立包组 packages/session；V2 一行一事件）+ _find/list_headers 走多代解析
│   │   ├── generation.py  # generation 读侧：canonical 文件名/目录多代选择/migrate-on-open（对应 session-persistence-jsonl/src/generation.ts）
│   │   └── released/      # released 只读词表 + v0/v1 codec + 相邻迁移链迁移器（不对应 packages/session/session-format-*/）
│   ├── session_store.py   # SessionStore（ctx.sessions 服务：create/prepare/enter/announce + fork + flush）
│   ├── scope.py           # Context + RegistryService（vendor/cordis 语义）
│   ├── dsh_scope.py       # dsh-scope 原语（scopeParents 图 + scopeTarget 载波 + createScope，对应 packages/core/scope）
│   ├── hmr.py             # Cordis HMR 服务（vendor/hmr：register_config watch + 单飞刷新 + config-update-failed 外泄）
│   ├── schema.py           # schemastery 配置引擎全量移植（vendor/schemastery）
│   ├── home_paths.py       # harness 根解析（$DSH_HOME > ~/.dsh；packages/util/home-paths）
│   ├── tool_timeout.py     # 工具调用超时约定常量（TOOL_TIMEOUT / timeout_error_message，L0，packages/guard 的超时执行器共享面）
│   ├── tools.py            # 工具注册表 + 执行管线
│   ├── system_prompt.py   # SystemPromptService（分节渲染，systemPrompt 服务）
│   └── agent_loop/        # agent.py（turn/step 状态机，V2 内嵌流写入磁盘）+ assistant_stream.py（AssistantStreamAttempt）+ resident_loop.py（常驻单循环）+ tool_calls.py（并行调度）+ inbox.py（双队列收件箱）
├── llm/                   # packages/llm
│   ├── protocol.py        # StreamChunk / LlmAdapter / LlmFailure / BlockAssembler（协议层）
│   │                        + 图像定价/模态/发现类型（LlmImageRequestPrice/Pricing 等）
│   ├── assistant_stream.py # AssistantStreamRecord codec：Accumulator 压缩 / expand 解码 / validate 校验（V2 流内嵌）
│   ├── deepseek.py        # DeepSeek wire 序列化 + SSE 适配器
│   ├── fake.py            # FakeLlmAdapter（教学扩展）
│   ├── retry_policy.py    # retry policy 解析（normal/always）
│   ├── retry.py           # agent/request-error 恢复 + 退避
│   ├── token_meter.py     # TokenMeter 增量 fold + usage 折入锚
│   └── deepseek_files/    # DeepSeek Files API 执行簇（file-id/defaults/models/types/model-info/
│                          #   image-tokens/request-pricing/files-api/upload-index/file-store/request-files）
├── ptc_runtime/           # packages/ptc-runtime（seam + Python 后端，L1）
│   ├── types.py           # PtcRunRequest/Spec/Result/Failure + 绑定契约（PtcBindingNamespace）
│   ├── service.py         # PtcRuntime Service Definition + 保留名常量 + 绑定校验
│   └── runtime.py         # PythonPtcRuntime（CPython 子进程 + 行 JSON 绑定协议）
├── ptc/                    # packages/core/tools/src/ptc.ts（L2，tools-presentation seam）
│   └── run_code.py        # run_code 工具（子派发 tool/ptc-dispatch* 事件 + 精心挑选外层结果）
├── attachment/             # packages/attachment（attachment + attachment-local）
│   ├── types.py            # ImageAttachmentRef（含 originalDimensions）/ FileAttachmentRef / SaveImage·SaveFile·SaveFileStreamAttachment / ImageAttachmentLimits / ImageRequestTarget / RequestImageAttachment
│   ├── error.py            # AttachmentError + 17 错误码（含 INVALID_FILE_BASE64 / ATTACHMENT_FILES_UNSUPPORTED）+ is_attachment_error
│   ├── encoding.py         # 共享质量阶梯 [85,75,60] + encodeFirstWithinLimit 惰性候选执行
│   ├── normalization.py    # provider 无关规范化管线（直通/总像素预算+长边封顶/按 alpha 分流编码）
│   ├── projection.py       # requestImageDimensions 纯请求投影几何（alpha.1 抽到 seam 包）
│   ├── request_image.py    # variantId 确定身份的请求图缓存版本（request-image-v6，按路由目标）
│   ├── admission.py        # canonical base64 wire 受理入口（图片批次 + 文件单个）
│   ├── file_store.py       # verbatim 文件内容寻址存储（files/<sha2>/<sha>/<name> 别名 + file-objects 规范对象）
│   └── store.py            # LocalAttachmentStore（规范化字节 sha256 内容寻址 + 完整性复验 + verbatim 文件族 + admit_prompt_content）
├── seams/session_checkpoint.py  # 语义持久化检查点策略（packages/session/session-checkpoint-policy）
├── storage/                # packages/storage（storage hub + storage-domain + storage-json）
│   ├── hub.py              # Storage hub（ctx.storage：backend 注册表 + 可挂载数据形态，不碰 IO）
│   ├── registry.py         # BackendRegistry（具名 backend 表 + stale disposer 守卫）
│   ├── spec.py             # 域声明（define_domain / domain_table / DomainSpec / descriptor_of）
│   ├── domain.py           # DomainImpl + KvTable + DomainGlobal（单写链 + domain/changed 发射）
│   ├── facility.py         # DomainFacility（route 解析 + open 校验 + backup-and-skip 政策）
│   ├── events.py           # DomainChanged（put 带新值 / delete 无值）
│   ├── error.py            # StorageError / DomainError 错误码闭集
│   └── jsonbackend/        # JSON 持久 medium：atomic.py / format.py / single_unit.py / per_record_unit.py
├── compaction/            # packages/compaction
│   ├── config.py          # 压缩规格解析（threshold / retain / retries）
│   ├── region.py          # selectCompactableRange + 压缩事务（surface replace 检查点）
│   ├── summarizer.py      # 前缀重放摘要 + 检查点框架
│   ├── tool_result_pruner.py # 可选 tool-result 裁剪阶段（ctx.toolResultPruner 消费者）
│   └── engine.py          # BasicCompactionEngine（pre-step 压力 / request-error overflow 接线）
├── jobs/                  # packages/jobs（seam + jobs-local + tool-jobs）
│   ├── types.py           # 终态 / 常量 / JobDoneBox（done 的 Promise 替身）
│   ├── registry.py        # LocalJobRegistry（ctx.jobs 服务 + owner 栅栏 + 结算/上限/teardown）
│   └── tools.py           # job_output / job_list / job_kill + 完成 notice 投递 + 可见输出封顶（finalizeContent）
├── plan/                  # packages/plan/plan-mode（状态机 + 审查 UI + 投影）
│   ├── config.py          # plan-mode 规格解析（section 校验，fail loud）
│   ├── mode.py            # PlanModeController（log-only plan/mode + plan:policy 节 + pre-step 提交）
│   ├── review.py          # exit_plan_mode 工具 + /plan 命令 + userQuestions 审查通道
│   └── projection.py      # plan 投影单元（command/run ↔ plan/mode 双事件折叠）
├── commands/              # packages/interaction/commands（命令约定）
│   └── __init__.py        # CommandRegistry + command/run|done 配对 + parse/route
├── goal/                  # packages/goal（goal + goal-round-driver + tool-goal + command-goal）
│   ├── domain.py          # goal/change 事件严格重放 fold + GoalError
│   ├── service.py         # GoalService（ctx.goals：compare-and-set 变更 + 激活）
│   ├── prompt.py          # goal round 提示词
│   ├── driver.py          # GoalDriver（pre-step reservation 校验 + continue_rounds + driver 模式事件驱动续跑）
│   ├── tools.py           # get_goal / create_goal / update_goal + tool:goal 节
│   └── commands.py        # /goal 命令表面
├── skills/                # packages/skill（skill + skill-filesystem + tool-skill）
│   ├── registry.py        # SkillRegistry（ctx.skills 服务 + 分层注册 + 渲染/digest）
│   ├── filesystem.py      # FileSystemSkillProvider（六类根 + frontmatter）
│   └── tool_skill.py      # skill 工具 + /名字 手势 + durable catalog 注入
├── guard/                  # packages/guard（循环卫生守卫：超时执行器 + 重复调用提醒）
│   ├── timeout_policy.py   # TimeoutPolicy 插件（上游 timeout-policy，仅注册超时执行器约定面）
│   └── repeat_tool_reminder.py # RepeatToolReminder（重复工具调用提醒：预拒绝计数 + 决策折叠）
├── identity/               # packages/identity/anonymous-user-id（harness-home 匿名用户 id）
├── telemetry/             # packages/session/{session-stats,session-telemetry,session-telemetry-otel} + packages/llm/token-meter
│   ├── folds.py           # fold_session_stats / fold_token_usage / derive_turn_token_usage（纯 fold）
│   ├── service.py         # UsageStatsService（ctx.usageStats）+ projection_values 自由函数
│   ├── session_telemetry.py # SessionTelemetryBackend + Coordinator（live/on-demand 采集 + 脱敏 waterfall）
│   └── session_telemetry_otel.py # OTel 后端（LoggerProvider + OTLP 导出；FEEDBACK_ONLY/DISABLED）
├── session_query/         # packages/session-query（session-query + session-query-sqlite + tool-session-query）
│   ├── config.py          # 配置常量 + SessionQueryError 错误码闭集
│   ├── extraction.py      # 一方事件语义文本抽取
│   ├── documents.py       # 事件记录 + 语义文档投影（surface 分类）
│   ├── sqlite.py          # FTS5 检索索引（bm25 + snippet）
│   ├── service.py         # SessionQuery（ctx.sessionQuery：search/search_events/read_event/trace_event/lineage）
│   └── tool.py            # 模型侧五工具（session_search/event_search/trace/event_trace/event_read）
├── boot/                  # packages/boot
│   ├── boot.py            # 启动 + patch overlay
│   ├── composition.py     # YAML 配置 / !!js 插值 / dump 渲染
│   └── dotenv.py          # .env 解析（parse_dotenv）
├── loader/                # vendor/loader + vendor/include（cordis 组件系统，L0）
│   ├── model.py           # ENTRY_KEY / GROUP_KEY / SEP 载波键
│   ├── utils.py           # !!js 求值 + baseUrl 上溯 + 瞬态事件循环结算
│   ├── patch.py           # apply_entry_patches（replace / insert / name 门）
│   ├── entry.py           # Entry（update / disabled / init）+ module 旧方言桥
│   ├── group.py           # EntryGroup（一级子列表宿主）+ Group 插件
│   ├── tree.py            # EntryTree（扁平 store + resolve / import_ / write）
│   ├── loader.py          # Loader 服务（internal/config|update|plugin 钩子）
│   └── include.py         # Include 子树（文件读写 + initial + !!js 原样回写）
├── fs/                    # packages/fs（文件系统 seam + 本地/沙箱后端 + 模型侧工具）
│   ├── types.py           # 目标/版本标识、元数据、写/编辑意图与结果、FsErrorCode 闭集
│   ├── service.py         # FileSystem（ctx.fs 抽象契约）
│   ├── local.py           # LocalFileSystem（realpath 身份 / 原子写 / 字面编辑）
│   ├── sandbox.py         # SandboxedFileSystem（每次调用沙箱围栏）
│   ├── observation_policy.py # 观测态策略（fs/write-intent、fs/edit-intent、fs/observed）
│   ├── diff.py            # write/edit 结果态 hunk diff
│   ├── tools.py           # read / write / edit 工具（tool-fs）
│   ├── str_replace_editor.py # str_replace_editor 工具
│   ├── search.py          # glob / grep 工具（stdlib 承载）
│   └── present.py         # present 工具
├── todo/                  # packages/todo
│   └── __init__.py        # to_todo_list / fold_todos / install_todo_tool（模型侧 todo_write）
├── spill/                 # packages/spill（spill + spill-local + spill-policy）
│   └── __init__.py        # SpillStore seam + LocalSpillStore + 大结果落盘策略（tools/post-execute）
├── workspace/             # packages/workspace/workspace（工作区实体 + 注册表服务）
│   └── __init__.py        # Workspace / WorkspaceService（ctx.workspaces）+ paths 规范化
├── settings/              # packages/settings（settings + settings-file）
│   └── __init__.py        # SettingsProvider + SettingsScope + redact_secrets + 文件 provider
├── cli/                   # apps/cli
│   ├── main.py            # launcher 选项（profile / patch / dump）
│   ├── headless.py        # 一次性任务入口
│   ├── default_tools.py   # headless 默认工具集（教学扩展）
│   ├── session_cmds.py    # 会话 list / resume / delete / stats（教学扩展；stats 为遥测可视化终端）
│   └── preset_cmds.py     # presets list / show / select / delete（教学扩展，web Remote 等价本地入口）
├── preset/                # packages/preset + apps/cli/config/agent-presets
│   ├── presets.py         # shipped root / 分层 roster / 投影 / 锁 / cordis 翻译（数据目录 preset/{minimal,standard} 随迁）
├── extensions/            # packages/extensions
│   └── dynamic.py         # 动态插件生命周期
├── interaction/           # packages/interaction
│   └── approval.py        # 审批服务
├── protocol/              # packages/{acp, sdk, hooks}
│   ├── acp.py             # ACP 服务器子集
│   ├── sdk.py             # JSON-RPC 信封 + 最小运行服务
│   └── hooks.py           # hooks 桥（CC 配置 → 拦截决策）
├── seams/                 # packages/{sandbox, credentials, authorization, subprocess, subagent}
│   ├── sandbox_local.py   # 真沙箱后端（平台链探测 / 失败即拒绝）
│   ├── sandbox_policy.py  # 沙箱策略服务（部署缺省 + 会话日志覆盖决议）
│   ├── landlock_run.py    # Landlock 自限制执行器（native/landlock-run 的 ctypes 载体）
│   ├── sandbox_windows_acl/ # Windows ACL 写限制沙箱（ctypes FFI 物化上游 windows-acl-restrict-poc；非 win32 import 即抛 OSError）
│   ├── credentials_local.py # 凭据四层 + CredentialsService（provide="credentials"，发 credentials/record-updated）
│   ├── authorization.py    # 授权服务（install_authorization opt-in；registerFlow/list/describe/cancel/begin + authorization/settled）
│   ├── subprocess_env.py  # 子进程环境清洗切片（凭据形 + DSH_* 名剔除，packages/subprocess）
│   └── subagent/          # __init__ + descriptor + providers（三通道）+ worker（子进程）+ continuation（续跑管理）+ tool（模型侧委托工具）
├── shell/                 # packages/shell/{shell, bash-local, bash-sandbox}
│   ├── bash_local.py      # 本地 bash 执行器（ctx.shell 缺省 provider）
│   ├── bash_sandbox.py    # 沙箱消费执行器（confine 包裹 + 三路归因）
│   └── helpers.py         # spawn 归因 / denial / runner 失败分类（helpers.ts）
├── mcp/                   # packages/mcp/{mcp-client, mcp-resources}（mini 子集）
│   ├── types.py           # Config schema / ReconnectConfig / RECONNECT_DEFAULTS / 常量
│   ├── client.py          # apply(ctx, config) 同步门面：resolve 配置 → 链接生成
│   ├── connection.py      # 代连接 supervisor（stdio / streamable-http）+ 重连 + 工具归属注册 + 生命线 ping
│   ├── transport.py       # 传输工厂（stdio_client / streamable_http_client）
│   ├── server_context.py  # MCP server 挂载（静态 server / CLI 定位 / 配置解析 → 联系请求）
│   ├── tools.py           # sync_tools（公共工具名 hash / bridge 定义 / output 投影 / 图片受理）
│   ├── fixture_server.py  # stdio 夹具（教学扩展，tests 用）
│   └── resources/         # packages/mcp/mcp-resources
│       ├── render.py      # output render（手动 JSON 序列化的紧凑 JSON 投影）
│       ├── tools.py       # list/read 三个共享资源工具（register_resource_tools）
│       └── runtime.py     # ResourceRuntime（createResource → 生命周期 + disposer）
├── client/                # packages/client
│   └── trajectory.py      # Trajectory 折叠引擎
├── web/                   # packages/api/gateway + packages/client/connection + session-controller + remotes + host/frontend-static + host/webserver（mini 子集）
│   ├── envelope.py        # 两信封 RPC（client-request / server-response，rpc-schema.ts：connection 层错误闭集 + transport_error 折叠）
│   ├── api.py             # WebApi 会话服务（unary 方法 + 路由表）
│   ├── stream_protocol.py # Remote 流 wire 语法（open/cancel/item/end/error 帧 + $events/result payload）
│   ├── mux.py             # WS /api/remote.mux 单路径承载全部 Remote 流（RemoteStreamMuxConnection）
│   ├── events.py          # $events 注册表（api-session/* 转发源 + waterfall + $events/result 结算）
│   ├── streams.py         # GatewayStreams（$events 装配 + session/follow/control 流分发表）
│   ├── approvals.py       # 审批桥（async tools/ask 闸门 ↔ approval/request waterfall + $events/result）
│   ├── downloads.py       # GET /api/session.export 会话日志导出（zip 打包 root + 后代 + 媒体）
│   ├── frontend.py        # 静态服务约定（遍历 403 / SPA 回退 200 / MIME）
│   ├── inventory.py       # pluginInventory/list 投影（Loader 条目四字段 + preset 组合行 flatten）
│   ├── static/            # vanilla SPA 教学参照前端（index.html / app.js / style.css，无构建步；消费旧 SSE wire，对新后端不工作——真实对接见仓库顶层 `webui/`）
│   ├── server.py          # FastAPI 载体（unary {args} 解包 + $events/result 特判 + WS + 静态，stream-server.ts / handler.ts）
│   └── launcher.py        # web profile 启动器（build_app / run_web）
├── demo.py                # 端到端演示（教学入口，python -m miniharness.demo）
└── example_plugins.py     # boot 演示插件（教学示例）
```

### 1.3 顶层再导出策略

- `miniharness/__init__.py` 保留"教学再导出"：`from miniharness import Session` 对学习者成立；
- 子包 `__init__.py` 做族内再导出：`from miniharness.llm import StreamChunk` 与 `from miniharness.llm.protocol import StreamChunk` 等价。文档与示例写浅路径，业务代码写深路径（可被依赖方向测试检查）；
- **聚合器必须显式 `__all__`**：无 `__all__` 时 `from .x import *` 会把子模块命名空间的所有公开名复制进包——包括与子模块同名的属性（子模块属性遮蔽包引用），也包括子模块内部导入的 stdlib 名（如 `json.py` 里的 `import json`）。星号导入只应复制约定名，因此子包与其子模块各写显式 `__all__`；
- 命名沿用上游：`sdk.py` 与上游包名一致；`session_cmds.py` 与 `sessions.py` 单复数混淆消除。

## 2. 模块 ↔ 上游映射

行级对照账本：每一行是"mini 路径 ↔ 上游对应"的权威归属。简化标注以各模块 docstring 为准，本表只列归属；改公共代码时先查本表。

| mini 路径 | 上游对应（唯一权威） | 备注 |
|---|---|---|
| `core/session/`（session/types/invariant/json/message/repair/surface/seq_ranges + generation/persistence/zstd_frames 等） | `packages/core/session/src/`（types/invariant/surface/repair/seq-ranges/preparation/request-header/known-event-types/index 9 文件）+ `packages/llm/llm/src/message.ts` + `packages/session/session-format/src/json.ts` | message 构造保留在会话域（L0 不依赖 llm，简化标注）；上游 `json.ts` 在独立的 session-format 包（JSON 规范化/roundtrip），mini 并入会话域 |
| `core/session/persistence.py` | `packages/session/session-persistence-jsonl`（+ `session-format*` 相邻迁移链） | 上游是独立包组，mini 并入会话域（简化标注）；目录布局与上游 `session-persistence-jsonl/src/format.ts` 一致：`root/--<projectKey(cwd)>--/<encodeSegment(id)>/session.v3.jsonl[.zstd]`（generation 版本化文件名，v0 旧名 `session.jsonl` 保留拒读；`~XXXX` 段转义、projectKey 分隔符折叠+251 截断、cwd 缺省 `_no-cwd`）；**默认载体 zstd 拼接帧容器**（`zstd_frames.py`，一帧一记录，torn 末帧前缀恢复；可选明文 `.jsonl`），编码互斥、遗留平铺、重复 id、未知版本都直接拒绝读取（released 旧版本由上游相邻迁移链处理）；**V3 一行一事件**（上游仅 v0→v1 迁移 codec 保留打包，`assistant/message` 内嵌流）；头行与目录同源（header.cwd 回写）；崩溃修复 closers 经 `commit_repair` 写入磁盘（与上游 commitRepair 一致），恢复构造 `mode="restore"`；**多代读侧**：`_find`/`list_headers` 经 `core/session/generation.py` 解析 canonical 名、取最高代、`migrate-on-open`（确保代当前才回写，`_ensure_generation_current`）——v0/v1 released 旧制品读入时被相邻迁移（v0→v1→v2→v3）转成 v3 后继发布，源文件不可变保留（`os.replace` 原子发布，`.zstd` 后缀＝mini 载体约定，上游 `.zst`） |
| `core/session/released/`（helpers/dispositions/codec/validate/validate_v2/validate_v3/payload_validation/relationships/migrate_v0_v1/migrate_v1_to_v2/migrate_v2_to_v3/catalog，共 13 文件） | `packages/session/session-format-catalog` + `session-format-v0-to-v1` + `session-format-v1-to-v2` + `session-format-v2-to-v3`；词表 `packages/core/session/src/known-event-types.ts`（54 类型闭集）；格式类型 `packages/session/session-format/src/types.ts` | 相邻迁移链整件纯函数移植（含 v2→v3）；`migrate_released_artifact` 按版本选边 v0→v1→v2→v3；**载体差异（登记）**：v1 物理头 `seedLength→isSeeded`（`seedLength:0` = seeded 零切割）、v1 `user/message` flat、chunk 流内嵌（`assistant/message` stream records / `assistant/attempt`）；fork 切点重导出为 `{inherited:true}` marker（空种子合成 createdAt 时间）；密集重映射拒绝指向已消费 chunk 的引用（绝不重定向，逐字文案）；**迁移不写任意目标**（组装完整 AFTER + staged 校验 + 原子替换）；**深度校验层**：54 类型逐字段 payload 语义（`payload_validation.py`，上游 payload-validation.ts ~1028 行）+ 跨事件关系状态机（`relationships.py`，上游 relationships.ts ~475 行，v2 `assistant/attempt` step 门经 `(set,frozenset)` 修复）+ artifact 编排（`validate.py`/`validate_v2.py`/`validate_v3.py`：内嵌流三事实 cross-check、marker/cut 双向、restore＝信封级装载、v2 field 无引号方言）；迁移链已切真实校验器（v0→v1 逐事件门 + 终态 artifact 双重门，v1→v2 目标门，v2→v3 目标 assertV3Event 全量校验）；仅令牌/自性能隔离不随行移植 |
| `core/session_store.py` | `packages/core/session/src/index.ts`（SessionStore 部分） | 内存会话服务：create/prepare/enter/announce 生命周期 + get/list/fork（五错误码）+ flush 检查点 + `session/created|disposed|event|flush` 四事件；create 与上游 generator effect 一致（enter 先 yield、announce 抛错自动回滚）；事件派发经 scope_target 载波（carrier=scope_target(session, scope_of(owner_ctx or self.ctx))，与上游 enter 的 scopeTarget(session, scopeOf(store.ctx)) 一致）；**结构一致：`SessionStore(Service)`，构造 `super(ctx, "sessions")` 即经 ctx.provide 自动登记、随拥有 fiber 自动注销（与上游 index.ts:790 `extends Service` + `super(ctx, 'sessions')` 一致），install_sessions/web/api 手工 provide 已简化移除**；无 typert lookup、flush 为同步近似（简化标注见模块 docstring） |
| `core/agents.py` | `packages/core/agent/src/index.ts`（AgentRegistry） | 进程内 live 代理实例注册表（ctx.agents，L1）：`AgentRegistry(Service)` 构造即 `super(ctx, "agents")` 登记、随拥有 fiber 注销；`register(agent, owner=None)`（id 与会话不符 / 同 id 已登记 fail-loud）、查询面 get/list/roots/is_owned_by、`agent/created`/`agent/disposed`（agent 自有载波，非会话日志事件；与上游 publish 时 enter+announce 公告一致，mini 单同步进程一键发布）；`install_agents(ctx)` 幂等装配（生产 6 处 root 组合均接在 install_sessions 旁）；模块级 `assert_live_agent(agent)`——装配即强制（无 agents 服务的裸装配 no-op）。注册点 = `AgentLoop.publish()`；jobs `_assert_access` / goal `_prepare_mutation` 共用 assertLive 边界（陈旧/重复实例拒绝）。载体差异：上游 initiator 用 AsyncLocalStorage 承载身份，走 enter+announce 两步；subagent 运行时 owner 链不承载（本节只登记 root/父子全部 live 实例） |
| `storage/`（hub/registry/spec/domain/facility/events/error + jsonbackend/） | `packages/storage/storage`（index.ts：Storage 插件 + storageBackendServiceKey） + `packages/storage/storage-domain`（domain.ts/index.ts） + `packages/storage/storage-json`（single-unit/per-record-unit/format/atomic） | 非会话 KV 存储中心（ctx.storage，L1，不承载 storage-sqlite）：hub 不碰 IO（backend 注册表 + 可挂载数据形态，`storage.backend.<name>` 生命周期服务键）；domain 数据形态带 schema 校验（open 逐记录 `validate_schema_value`，invalid-record 带 fail-closed detail（table/key）；`invalid_records='backup-and-skip'` 时坏记录 `backup_record` 移开 + 日志 + 跳过）、单写链（写先持久后改内存再发 `domain/changed`，put 带新值 / delete 无值；`update` 在链槽读改写 + missing-key 拒绝；close 排空已排队写照发事件、以 DomainError('closed') 拒绝新写）；每域单开（already-open / reserved 释放）；JSON backend：single 整文档 / per-record 一记录一文档（path-safe key、foreign 坏/过期文档读缺位、legacy 整文档 bootstrap 经版本接受集栅、`.bak.<YYYYMMDDHHmm>` 备份文件） |
| `core/scope.py` | `vendor/cordis` + `packages/core/scope` | Context（服务仓库 + 事件总线 + 四种派发 + asyncio 变体）+ fiber 生命周期（对应 fiber.ts：状态机 PENDING/LOADING/ACTIVE/FAILED/UNLOADING/DISPOSED + `internal/status`；`effect(execute, label)` 上游形态——execute 立即执行、返回值按 None/callable/awaitable/生成器收集为 disposer；单发 + 可 await；注册先于执行 + setup barrier 重入保护；dispose 幂等 join 在途；同步立即逆序、异步并发 unload、错误 contained；装载半边 + 注册表——`RegistryService`（`ctx.plugin` 缩写 + 插件形态归一 + 运行记录按 callback 键控）、fiber 携带 inject 依赖 + epoch 重载（依赖变化卸载→重装）、`restart()`/`update()`（`internal/update` waterfall）、`internal/config` waterfall + schema 校验（`resolve_config`，`core/schema.py`）、`internal/plugin` 每次装载/卸载派发）+ `create_scope` fiber-backed 作用域（父销毁收回子 fiber）+ 服务仓库（reflect.ts 对应：按隔离标签键控的全局 store，`ctx.get` strict 缺省返回 None，`ctx.isolate(name)` 换标签，per-agent 的 tools/systemPrompt 经隔离不冲撞 root realm，重复提供同一标签 fail loud）；另含 `Service` 基类（service.ts 对应：构造即经 `ctx.provide` 自动登记、随 fiber 注销、`_invoke` 可调用、`_check`/`_init`）、`ctx.extend(meta)`/`ctx.intercept(name, config)`（intercept 配置经 `Service._resolve_config` 沿祖先链近根优先合并）、内建 `LoggerService`（logger.ts 对应：`ctx.logger(name)` 铸具名 Logger 门面 + printf 格式 + exporter 注册/级别过滤/默认缓冲导出器，`ctx.logger` 属性为绑定访问方 ctx 的视图）；dsh-scope 对应：事件派发模型改上游形态——`on` 双写 root `_flat_hooks`（全局 Hook 表）+ 祖先链 `_listeners`，dispatch 系加 `this_arg` 载波参数（有载波走扁平表按载波键过滤，无载波保留祖先链）；`create_scope` 返回 Scope 包装 + 自动绑父 scope；`scope_key` = `scope_of(self)`（scopeParents 图）；残余简化标注见模块 docstring |
| `core/schema.py` | `vendor/schemastery/src/index.ts`（902 行单文件） | schemastery 引擎全量移植：可调用 Schema 节点 + `resolve` 分发 + 17 类 resolver（any/never/const/string/number/boolean/function/is/bitset/array/dict/tuple/object/union/intersect/transform/lazy + date/regExp/arrayBuffer 复合体）+ meta 克隆语义 + from/extend + ValidationError(`$path` 前缀) + Options(autofix/ignore/path/strict) + toString 全 formatter + toJSON(uid+refs 共享序列化) + i18n(mergeDesc) + simplify(deepEqual dict-aware)；`S.from_/is_/reg_exp/array_buffer` 等 pythonic 命名（上游名映射进 docstring）；cordis fiber 适配层（`resolve_config`/`ValidationError` 聚合消息）保留文件尾部；L0 叶零内部依赖，cosmokit 助手(deepEqual/isNullable/isPlainObject/clone/valueMap/pick/Binary)内联；callback 不做字符串求值、date/regExp/arrayBuffer 锚定 Python 对应物（载体差异标注） |
| `core/dsh_scope.py` | `packages/core/scope/src/index.ts` + `store.ts` | dsh-scope 协议本尊（纯库，L0）：ScopeKey 弱引用身份键、scopeParents 图（bind/link/rebind + 环检测）、`scope_parent_of`/`scope_chain_of`（nearest-first）、`scope_target`/`_ScopeCarrier`/`is_scope_carrier`/`carrier_key_of`、`scope_of`（parent 链）、NamedEntries/AnonymousEntries/ScopedLayers 对应 store.ts；`Context.create_scope` 返回 Scope 包装（delegation 包装，`__slots__` 无 `__dict__`） |
| `core/hmr.py` | `vendor/hmr/src/index.ts` | Cordis HMR 服务：`Hmr(Service)` provide="hmr"；`register_config(filename, refresh)`——findWatchRoot walk-up 根定位（realpath+depth+缺盘拒绝）、重复注册拒绝、初扫已存在目标即刷（chokidar ignoreInitial=false 语义：缺文件无初扫）、disposer 注销+关 watcher+join 在飞刷新；`refresh_config(key)` 单飞+dirty 合并循环、失败 logger.warn + `hmr/config-update-failed` 并行事件外泄不毒化循环；销毁期注册归一 `CordisError(INACTIVE_EFFECT)`。载体 watchdog（上游 chokidar）；Node ESM 模块图热重载（ModuleLoader/externals/accepted）不适用 Python 载体；Windows 短路径两侧 normcase+realpath 归一 |
| `core/tools.py` | `packages/core/tools` | 作用域化注册表（ScopedLayers/NamedEntries 存储：注册即 effect 归目标 fiber、fiber 销毁即自动注销；resolve/names 缺省取注册表 root 的 scope 键，显式 scope 沿键父链最近者胜 + 全局层兜底）+ 守卫执行管线（pre/execute/post waterfall + schema 校验 + 超时） |
| `core/tool_timeout.py` | `packages/util`（超时常量）→ 被 `packages/guard/timeout-policy` 消费 | 工具调用超时约定叶（L0，零内部依赖）：`TOOL_TIMEOUT = "TOOL_TIMEOUT"`、`timeout_error_message(timeout_ms)`；被 `core/tools.py`（管线超时替换 `error_info={name:'ToolTimeoutError', code:TOOL_TIMEOUT}`）与 `guard/timeout_policy.py` 两侧共享——guard 超时执行器是 `packages/core/tools` 超时语义的消费方 |
| `guard/` | `packages/guard`（`timeout-policy` + `repeat-tool-reminder`） | **循环卫生守卫**：`repeat_tool_reminder.py` —— 重复工具调用提醒：`Config`（`thresholds: [3,5,8]` fail-loud 校验、`include`/`exclude` 通配符、`argumentsPreviewChars` 默认 500）；per-agent WeakKeyDictionary 链，`agent/pre-step` 任一 user 消息清链；pre-execute 监听器对下行 `{kind:'deny'}` 决策计数并把提醒挂到 `exec_.additional_contexts`（mini deny 短路 post-execute）；post-execute `observe()` 在委派前执行，提醒 prepend 到下游决策 `additionalContexts`（accept 路径 `{kind:'accept', additionalContexts:[reminder]}`，block 路径保留 `kind:'block'`）；提醒消息 source `{kind:'plugin', plugin:'repeat-tool-reminder', form:'notice', summary:'{tool} × {count}'}`；`timeout_policy.py` —— 只 import L0 `core.tool_timeout` 再导出 `TOOL_TIMEOUT`（超时执行器本体在 `core/tools.py` 管线，timer-wins），装配面 opt-in `install_timeout_policy`/`install_repeat_tool_reminder`。载体差异登记：上游 post-execute 决策 `PostToolDecision {kind, feedback?, additionalContexts?}`（tools/src/index.ts:599-602）——mini 管线读取 `kind`（旧 `action` 别名已统一）；上游 pre 侧 deny-vote 计数在 post-execute 决策里反馈，mini 因 deny 短路改为挂 exec |
| `core/agent_loop/agent.py` | `packages/core/agent-loop/src/agent.ts` | 单一 async 泵（`_pump_async`/`_run_step_async`）+ `followup`/`steer` 同步门面（经常驻单事件循环驱动，见下行 resident_loop）+ 协作式取消（`_cancel_event` 每轮新建 + `call_soon_threadsafe` 跨线程置位——对应上游 agent.ts:325 每 phase 新建 AbortController）；agent/pre-step 决策经 `awaterfall`；publish/dispose 生命周期（enter+announce+agent/session-start / cancel(disposed)+scope.dispose+detach，会话店成员资格归 loop）+ agent/* 事件载波派发（scopeTarget(agent, loop scope 键)，兄弟作用域隔离）+ turn/step 编号 1 起经 `_replayed_next_turn` 从会话日志续号（对应 invariant.ts `nextTurn`：turn/end 闭合后 +1、尾部未闭合停在当前号；resume 冷重建 loop 不重置回合号）；**V2 流结算**（agent.ts:375-458）：正常完成 settle `assistant/message`（内嵌 stream、content=原始 assembler 块）、finish error/aborted 与异常先 settle `assistant/attempt` 再走 request-error waterfall、取消定稿 interruptedBlocks 前缀（空则 attempt） |
| `core/agent_loop/resident_loop.py` | （无独立文件：Node 进程固有单事件循环） | 教学扩展：进程级懒加载单例循环（守护线程 run_forever）；`run_on_resident` 阻塞提交协程、异常冒泡、主线程 Ctrl+C 协作取消在途泵；同步门面由此驱动后跨调用共享同一循环，与上游形态一致 |
| `core/agent_loop/tool_calls.py` | `packages/core/agent-loop/src/tool-calls.ts` | |
| `core/agent_loop/inbox.py` | `packages/core/agent-loop/src/inbox.ts` | 双队列（followup→next-turn / steer→next-step）+ `agent/inbox/spliced` 持久化 |
| `core/agent_loop/assistant_stream.py` | `packages/core/agent-loop/src/assistant-stream.ts` | `AssistantStreamAttempt`：一次模型 attempt 的压缩 + 组装 + 终态结算封装（attemptId/revision/start/push/settle/abandon；settle 在持久事件 append 成功后发 committed、失败 abandon；push 同时喂 Accumulator 与 BlockAssembler）；V2 `assistant/message.stream` 内嵌与 `assistant/attempt` 的生产端 |
| `llm/assistant_stream.py` | `packages/llm/llm/src/assistant-stream.ts` | `AssistantStreamRecord` codec：`AssistantStreamAccumulator` 游程压缩（text/reasoning/tool-call delta 连续段 + 不可压缩原样 chunk，dt 硬性规定）、`expand_assistant_stream` 逐 delta 边界还原、`validate_record` exactKeys fail-closed；V2 seed 边界展开验证（`_assert_current_assistant_stream`）消费同 codec |
| `core/agent_loop/runtime_context.py` | `packages/core/agent-loop/src/runtime-context.ts` | loop 侧运行时上下文投影：retained 三态（undefined/null/{seq,text}）restore（倒序找最近一条仍在 surface 的 owned 快照）+ 按追加序惰性消化新事件；`project(current, sections)` 文本相等去重、变化铸快照 user 消息（sections 非空带 `form:'snapshot'` 归因，空即 CLEARED 哨兵不带归因）；SOURCE/CLEARED 逐字一致；接线在 `_run_step_async` pre-step waterfall 前（默认进入把快照追加在 claimed 之后，显式 enter 决策整体接管） |
| `core/system_prompt.py` | `packages/core/system-prompt/src/` | assemble waterfall + contexts/tools/variables 提供器 + `{{variable}}` 严格插值 + `render_context_sections`/`join_context_sections` 节渲染面（上游 renderContextSections/joinContextSections）；scope 层叠、assembly.tools→请求工具集成未复现（简化标注见模块 docstring） |
| `llm/protocol.py` | `packages/llm/llm/src/` | `stream(messages, tools, signal)` async 约定 + `StreamAborted` + `_aiter_raced`（异步迭代与 abort 事件竞速，asyncio 原生载体） |
| `llm/deepseek.py` | `packages/llm/llm-deepseek/src/` | httpx 异步传输（原生 asyncio，abort 置位即关闭连接、真取消）+ per-read idle 300s watchdog（与上游 fetch 一致）+ SSE spec-strict 解析 + catalog 能力解析 + image-capable 请求路径（Files API file-id 优先 → inline base64 回退 → stale-id 有界重试） |
| `llm/deepseek_files/` | `packages/llm/llm-deepseek/src/common/` | Files API 执行簇：file-id/defaults/models/types/model-info（能力解析）+ image-tokens（vision-token 计算器）+ request-pricing（路由目标 ImageRequestTarget + 请求图定价）+ files-api（双协议 httpx 传输）+ upload-index（files-v3.json + filelock）+ file-store（单飞共享上传 + 配额恢复）+ request-files（解析 + stale-id 重试 + 规范化图片诊断） |
| `llm/fake.py` | 无 | 教学扩展 |
| `attachment/`（types + error + image + encoding + normalization + projection + request_image + admission + file_store + store） | `packages/attachment/attachment`（seam + types + error + admission + request-projection）+ `packages/attachment/attachment-local`（store + image + encoding + normalization + request-image + file-store） | sharp→Pillow（权威全量解码/EXIF 定向/重编码）；规范化管线（总像素预算 + 长边封顶 + 共享质量阶梯按 alpha 分流）与 variantId 请求图缓存（request-image-v6，路由目标 ImageRequestTarget）与 alpha.1 一致；**verbatim 文件族与 alpha.1 一致**（`file_store.py`：file_leaf_name 清洗 / `files/<sha2>/<sha>/<name>` 别名 + `file-objects` 规范对象 / save·save_stream·read_stream 摘要验证；AttachmentStore 七方法含 admit_prompt_content 实例方法）；CompressionLimiter 并发闸与 SharedRequest 单飞登记架构不适用（同步载体天然串行）；显式 root（上游 DSH_HOME/attachments/v1） |
| `llm/retry_policy.py` | `packages/llm/llm/src/retry-policy.ts` | |
| `llm/retry.py` | `packages/llm/llm-retry/src/` | async 恢复决策（派发前熔合信号检查 + always 派发后复查中止胜过决策）+ 事件驱动多信号竞速可取消等待（等价 `AbortSignal.any`；裸测试替身信号回退轮询）+ 插件 effect teardown（注销监听器 + lifetime.abort + 排干在途恢复） |
| `llm/token_meter.py` | `packages/llm/token-meter/src/` | |
| `compaction/`（config + region + summarizer + engine + tool_result_pruner + image_offload） | `packages/compaction/compaction-basic/src/` + `compaction-tool-result-pruner/src/` + `compaction-image-offload/src/`（config / region / summarizer / index.ts / projection.ts） | 前缀重放无 KV cache 语义；toolResultPruner 可选阶段已与上游一致（`compaction/tool_result_pruner.py`，上游注入 `ctx.toolResultPruner`，mini 经 `ctx.get('toolResultPruner')` 取用）；`image_offload.py` 镜像 compaction-image-offload——`offload_oldest_images` + `agent/request-error` 上的 `IMAGE_OFFLOAD_REQUIRED` surface 修复（`install_compaction` 一并安装），投影经 `core/session/projections.py` |
| `jobs/`（types + registry + tools） | `packages/jobs/`（seam + jobs-local + tool-jobs） | controller/监听器按 scope 分层（P1-4a）；canonical value + render 分离；finalizeContent 可见输出二次封顶（job_output/job_kill）；`_assert_access` 前置 `assert_live_agent`（R4 agent registry，`core/agents.py`）；`run_in_background` 触发入口经模型侧 `subagent` 工具复现（简化标注见模块 docstring） |
| `plan/`（config + mode + review + projection） | `packages/plan/plan-mode/src/` | 状态机 + plan:policy 节 + 审查 UI（exit_plan_mode / /plan / userQuestions）+ plan 投影；canonical value + Tool.render 已与上游一致（简化标注见模块 docstring） |
| `commands/` | `packages/interaction/commands/src/` | 命令注册/派发 + `command/run|done` 配对 + commands/change 通知 + normalizeResult fail-loud；handler 签名 `(agent, raw)` 为教学扩展（简化标注见模块 docstring） |
| `goal/`（domain + service + prompt + driver + tools + commands） | `packages/goal/`（goal + goal-round-driver + tool-goal + command-goal） | Typert remote（上游命令由 human UI 表面派发，mini 用 `/goal` 命令承载）；`_prepare_mutation` 前置 `assert_live_agent`（R4 agent registry）；driver 模式事件驱动续跑（同步门面保留 `continue_rounds`）；权威判定近似；三工具 canonical value + render 已与上游一致（简化标注见模块 docstring） |
| `skills/`（registry + filesystem + tool_skill） | `packages/skill/`（skill + skill-filesystem + tool-skill） | 无 chokidar watch、无 ctx.fs 适配；skill 工具 canonical value + render 已与上游一致（简化标注见模块 docstring） |
| `telemetry/`（folds + service） | `packages/session/session-stats/src/`（projection）+ `packages/llm/token-meter/src/`（usage-projection + turn-usage） | sessionStats/tokenUsage 投影 fold + derive_turn_token_usage（fail-closed）+ opt-in `UsageStatsService`（ctx.usageStats）；wire `projections.values` 现场折叠等价（不建 registry）；contextPressure 与 telemetry-capture 不承载 |
| `telemetry/session_telemetry.py` + `telemetry/session_telemetry_otel.py` | `packages/session/session-telemetry` + `session-telemetry-otel` | 会话遥测（L2）：`SessionTelemetryBackend`（`ctx.sessionTelemetry`）seam + `SessionTelemetryCoordinator`（live 订阅 `session/created|event|disposed|flush` + `agent/error` 并清扫在世会话 / on-demand 读 canonical log；每条事件经 `session-telemetry/record` waterfall 脱敏，本包无规则；模块级 handoff cursor 防重放；contain 单步异常）+ OTel 后端（`LoggerProvider`+`BatchLogRecordProcessor`+OTLP/HTTP；`FEEDBACK_ONLY` 按反馈 on-demand 采集、`DISABLED` 仅告警；`sharing` 模式；shutdown 期限）。载体差异：Node `@opentelemetry/sdk-logs` → Python `opentelemetry-sdk`；匿名 `user.id` 经 `identity`；`feedback/committed` 面板与 `Session.fromRestore` 采集路径不承载（mini 无 feedback 提交面板）|
| `session_query/`（config/extraction/documents/sqlite/service/tool） | `packages/session-query/{session-query,session-query-sqlite,tool-session-query}` | 会话检索（L2）：`extract_event_text`（一方事件语义文本）+ `build_search_documents`（surface 分类 current/shadowed/log-only）+ `SqliteSearchIndex`（FTS5 bm25 + snippet；查询当数据）+ `SessionQuery`（`ctx.sessionQuery`：`search` 跨会话最佳命中 / `search_events` 会话内 / `read_event` 原始窗口 / `trace_event` 替换来源 / `lineage` 世系；活会话经 `ctx.sessions`、持久化经注入 persistence）+ 五模型工具。**载体差异**：上游 tracing 提供方分层与 observation/lease 不承载（现场解析日志）；授权/workpace 作用域（workspace-access + sessionProjections）不承载（无 workspace 实体，M5）；`session-log-export` 由 `web/downloads.py` 承载 |
| `todo/__init__.py` | `packages/todo` | 待办清单（L2）：`to_todo_list`（工具入参校验，措辞逐字对齐）+ `fold_todos`（`todo/write` 最新胜出、`turn/start` 清空）+ `install_todo_tool`（模型侧 `todo_write`；事件 `todo/write` 为 log-only，已入 `core/session/types.py` KNOWN_TYPES）。**载体差异**：上游投影单元经 `ctx.sessionProjections` 注册，mini 无投影注册表（随 M7），当前以纯函数 fold + 工具结果承载 |
| `spill/__init__.py` | `packages/spill/{spill,spill-local,spill-policy}` | 大结果落盘（L2）：`SpillStore`（`ctx.spillStore`）seam + `LocalSpillStore`（每会话私有目录、0600 文件、注入式根）+ `install_spill_policy`（`tools/post-execute` prepend：全文本结果超 `maxInlineBytes` 落盘 + head/tail 预览 + 取回提示；best-effort，失败绝不改写成功结果）。**载体差异**：上游策略依赖 `dsh-output-retention`（TextRetainer/`describeOmitted`），mini 内联简化 head/tail 预览 |
| `workspace/__init__.py` | `packages/workspace/workspace` | 工作区实体（L2）：`fully_qualified_workspace_path`/`default_workspace_title`/`realpath_normalize`（realpath 为唯一性 canon）+ `Workspace`（稳定 uuid、目录路径、标题、有序会话账户：setTitle/attachSession/insertSessionBefore/detachSession/status）+ `WorkspaceService`（`ctx.workspaces`：create/list/get/remove）。**载体差异**：上游经 `ctx.storage.domain`（storage-domain 表 + 双写恢复标记 + 单写链）持久化并做 header-validated 账户过滤/归档集，mini 以 JSON 注册表 + `os.replace` 原子发布承载（无 pendingMutation 恢复标记、无归档集、无 typert RPC 视图） |
| `settings/__init__.py` | `packages/settings/{settings,settings-file}` | 用户设置（L2）：`SettingsProvider`（`ctx.settings`：命名空间注册 + 解析值 = schema 默认 → composition `base` → 用户文档 section）+ `SettingsScope`（get/watch/update/replace/mutate）+ 写路径 monotonic revision（陈旧写 `SettingsConflictError`/`SETTINGS_CONFLICT`）+ `settings/updated`（深度相等门控）与 `settings/document-updated` + `redact_secrets`（schema 声明 secret 位置只报 set 状态）+ `SettingsFileProvider`（JSON 文档原子写 + watchdog 外部改动重载，provider 源提交）。**载体差异**：上游 schema 用 schemastery（`role('secret')` + `toJSON` 线视图），mini 以纯 dict 默认值 + 显式 secret 路径集承载；typert RPC 描述视图未承载 |
| `boot/boot.py` | `packages/boot/app-boot` | `mount_root_include`（Loader 服务 + 根 Include 条目，并登记 `_BOOTSTRAP_INCLUDES` WeakMap）+ `boot()`（装载根配置→依序补丁→审计未激活条目 fail loud，ACTIVE/FAILED/PENDING 三态对齐 `auditStartupEntries`）+ `load_optional_patches`（缺文件→空层、坏文件 fail loud）+ `watch_user_patches`（与 app-boot watchUserPatches 一致：经 HMR 服务 watch 用户补丁层→重读 include 非补丁 config + 用户补丁 → 根 Include `entry.update({config})` 事务性重挂 → `loader.await_all()` → 未激活审计）。载体：旧 `{replace|insert}` 叠层补丁在 boot 侧转成 applyEntryPatches 形态（`_overlay_to_entry_patches`，不做表达式求值） |
| `boot/composition.py` | `packages/boot/app-boot` + `apps/cli/src/args.ts` | `load_dotenv_file` 与上游 readEnvLayer 一致：ENOENT 静默/其它 warn/已存在不覆盖/bootstrap-only 物化前整体拒绝；`home=` 为 harness-home 时 HOME_LAYER_PROXY_NAMES（HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY）豁免、代理名错误文案明说 home `.env` 第二条出路（index.ts:174-177） |
| `boot/dotenv.py` | `packages/boot/app-boot`（loadEnv） | bootstrap-only 名单/前缀与 BOOTSTRAP_NAMES/PREFIXES 一致；HOME_LAYER_PROXY_NAMES 同款；豁免判定在 load_dotenv_file（同上游 readEnvLayer） |
| `loader/`（model/utils/patch/entry/group/tree/loader/isolate/include） | `vendor/loader/src/{index,internal,config/{entry,group,isolate,tree,utils}}.ts` + `vendor/include/src/index.ts` | cordis 组件系统活树：EntryTree 扁平 store + `entries()/resolve()/import_()/write()`；Entry 生命周期（update/disabled/init、`loader/partial-dispose`、`internal/plugin` 归属）+ module 旧方言 apply 桥；EntryGroup 一级子列表宿主（create/remove/update/stop）+ Group 插件；Loader 服务（`internal/config` 树载体字面/普通条目 interpolate、`internal/update` 写回 + 重载日志、`internal/plugin` 自销毁回写 disabled、`[Service.check]` 的 `intercept.await` 门控、`envData`/`exit`）+ `apply_entry_patches` + `isolate`/`intercept` 条目选项（`isolate.py`：Local/Global realm + `loader/patch-context` waterfall 迁实现 + `loader/partial-dispose` realm GC）+ Include（文件读写/initial/dump `!!js` 原样、rename 重试）。**方言**：条目 YAML 用上游 `entryListSchema`（JSON_SCHEMA + `!!js`）。载体/适配（登记 §3）：同步门面 + `ctx.plugin(parent=)` 显式归属、baseUrl 沿 ctx 父链上溯、`settle_gathered` 瞬态 loop 结算、`getOuterStack` 数据面。**不适用**：`internal.ts`（Node ESM 内部加载器）|
| `fs/`（types/service/local/sandbox/observation_policy/diff/tools/str_replace_editor/search/present） | `packages/fs/fs` + `fs-local` + `fs-sandbox` + `fs-observation-policy` + `tool-fs` + `tool-str-replace-editor` + `tool-fs-search` + `tool-present` | 文件系统域（L1）：`FileSystem`（`ctx.fs`）seam + `LocalFileSystem`（realpath 身份、严格 UTF-8 + NUL 二进制拒绝、原子 staging、`createIfAbsent` 硬链接 no-replace、`replaceIfVersion` 陈旧守卫、字面编辑 LF/CRLF）+ `SandboxedFileSystem`（每次调用 read-only/workspace-write/danger-full-access 围栏）+ `ObservedStateGate`（`fs/write-intent`/`fs/edit-intent`/`fs/observed`）+ 模型侧 `read`/`write`/`edit`、`str_replace_editor`、`glob`/`grep`、`present`。载体差异：AbortSignal→取消句柄；win32 DACL 复制/替换不承载；glob/grep 以 stdlib `os.walk`+`re` 替代打包 ripgrep（tool-fs-search）；`present` 投影声明面随 M7（无投影注册表）；`read_image` 未承载（依赖图片/附件渲染）。详见 verified-diffs §2.45/§2.46 |
| `cli/main.py` | `apps/cli/src/args.ts` | |
| `cli/headless.py` | `packages/bundle/headless` + `apps/cli` | |
| `cli/default_tools.py` | 无 | 教学扩展（上游是工具插件注册） |
| `cli/session_cmds.py` | 无 | 教学扩展（上游会话管理在 web 表层） |
| `cli/preset_cmds.py` | 无 | 教学扩展（上游 preset 管理是 web 表层 Remote：list/read/deletePreset/selectPreset）——`miniharness presets` 子命令，投影/锁/删除语义一致，不写 `agent-preset/selected` |
| `preset/presets.py` | `packages/preset` + `apps/cli/config/agent-presets` | shipped root(system) + 多根 first-root-wins roster、project_preset/project_session_agent_preset 投影、PresetLockedError、PresetNotWritableError、mount 作用域审计；YAML 翻译（agent.cordis.yml → Preset）；数据目录 `preset/{minimal,standard}` |
| `extensions/dynamic.py` | `packages/extensions/*` | |
| `interaction/approval.py` | `packages/interaction/user-approval` | |
| `client/trajectory.py` | `packages/client/ui-trajectory` | |
| `mcp/types.py` | `packages/mcp/mcp-client/src/{connection,index}.ts` | ReconnectConfig / RECONNECT_DEFAULTS / Config schema 默认值（serverName 模式 `[A-Za-z0-9_-]{1,32}`、toolCallTimeoutMs 60000、maxInstructionBytes 32768、GENERATION_CLOSE_TIMEOUT_MS 5000）；resolve_reconnect_policy / resolve_mcp_config 以显式解析承载上游 Schemastery loader 的 fail-loud 归一校验 |
| `mcp/client.py` | `packages/mcp/mcp-client/src/index.ts` | apply(ctx, config)：scope-session 化 serverName 保留集 + resolve → 链接生成；McpServerConnection 同步门面 |
| `mcp/connection.py` | `packages/mcp/mcp-client/src/connection.ts` | 代连接 supervisor：stdio / streamable-http 统一，简历过期扫描 + 重连（初始/上限延迟、预算耗尽即停），工具归属注册（`_RESOURCE_KEY` / `_TOOLS_OFFSET` 预算集）与 `sync_tools` 换代、`tools/list_changed` 重同步、server instructions 字节上限 fail-loud、失败即拒绝（failOnStartupError 直抛）；**载体差异（SDK 2.2）**：SDK stdio 传输在子进程退出时**不投递 EOF/异常**（`_drain_stdout` 永久阻塞、read_stream 不关闭）——mini 以「握手/工具 RPC 有界竞速（`_run_rpc`，5s，对 `generation.lost` 中止）+ 常驻生命线 ping（watchdog，15s 心跳 / 3s 超时）」替代上游 within 取消令牌，杜绝子进程退出后握手流程阻塞挂起；代关闭屏障（GENERATION_CLOSE_TIMEOUT_MS）超时 fail-closed 防重叠子进程 |
| `mcp/transport.py` | `packages/mcp/mcp-client/src/transport.ts` | 传输工厂 + stdio 子进程 env 组装（复用 `seams.subprocess_env` 净身切片的 L3 例外，见 §3 规则 1） |
| `mcp/server_context.py` | `packages/mcp/mcp-client/src/{server-context,mcp-servers.ts}` | server 挂载点 / CLI 定位（`-m module`、script、绝对路径）/ MCP_SERVERS_ORDER |
| `mcp/tools.py` | `packages/mcp/mcp-client/src/tools.ts` | sync_tools：`${server}.${name}#${hash}` 公共工具名（stringHash32 → base36 → MCP 前缀截断）、darkfrozen 手写 JSON 序列化等价（frost_equal / create_output）、LLM 输入/输出/图片受理投影（admit 门 + variantId 请求图缓存） |
| `mcp/resources/render.py` | `packages/mcp/mcp-resources/src/render.ts` | render_resource_result：手动 JSON 序列化紧凑 JSON + blob 掩码（8 base64 字符不泄漏原文） |
| `mcp/resources/tools.py` | `packages/mcp/mcp-resources/src/tools.ts` | list_mcp_resources / list_mcp_resource_templates / read_mcp_resource 三工具定义（server/cursor/uri 参数约定 + Render 双参） |
| `mcp/resources/runtime.py` | `packages/mcp/mcp-resources/src/index.ts` | McpResourceRuntime（createResource 生命周期、资源请求经关联连接、disposer 注销）+ install_mcp_resources(ctx) 装配 |
| `mcp/fixture_server.py` | 无 | 教学扩展（tests 的 stdio/HTTP 夹具，`--die-after` 自毁模拟崩溃） |
| `web/envelope.py` | `packages/client/connection/src/{rpc-schema,rpc}.ts` | 两信封消息联合（client-request / server-response）+ 连接层错误闭集（含 R3 新增 `gateway/input-invalid`）；transport_error 折叠兜底码 ‘internal’ |
| `web/api.py` | `packages/api/session-controller/src/index.ts`（session 域辅助入口）| WebApi unary 方法（list/search/create/selectModel/modelCatalog/canOpenWorkspacePath/openWorkspacePath/rename/fork/prompt/attachment/updateQueue/cancel/page）+ 路由表；`session/queue` placement 三态经 `session/control` 投影 |
| `web/args.py` | `packages/api/gateway/src/index.ts`（assertExactArguments:1112 / decode:1140）+ `remote-error-codes.ts` | 路由层 `{args}` 边界校验：每方法字段集合精确匹配（missing/unexpected → `gateway/arguments-invalid`）+ 顶层 JSON 类型（错型 → `gateway/input-invalid`）；`TypertGatewayFaultDetails{endpoint, field?}`；枚举/范围/非空/跨字段语义留 handler（业务码） |
| `web/stream_protocol.py` | `packages/api/gateway/src/stream-protocol.ts` | Remote 流 wire 语法：`open`/`cancel`/`item`/`end`/`error` 帧、`$events` 打开与 `$events/result` payload 解析、无损 JSON 判定（dict 键须 str、float 有限非 -0） |
| `web/mux.py` | `packages/api/gateway/src/create-mux-websocket.ts`（RemoteStreamMuxConnection）| 单条 `/api/remote.mux` WebSocket 承载全部 Remote 流；open/cancel/item/end/error 帧往返，二进制 1003/非法 1008 关闭码，隔离单流失败 |
| `web/events.py` | `packages/api/gateway/src/index.ts`（remote-event）+ `packages/api/session-controller`（api-session/*）+ `packages/api/remotes` | `$events` 注册表：open 首帧 `ready`{clientId, host.home} → 转发 emit/waterfall/cancel；api-session/* 转发源（created/disposed/status/error/activity）；waterfall 经 `$events/result` 结算（result/next/rejected/cancelled），未知 clientId fail-closed |
| `web/streams.py` | `packages/api/session-controller/src/{index,remote-events}.ts` | GatewayStreams Remote 方法面：session/follow（快照 snapshot{header,cursor,records,hasMore,projections} + 逐条 event）+ session/control（baseline{queues,jobs} + 实时 queue/jobs）+ `$events` 装配；跨堆非阻塞唤醒线程安全。活体 event 载体 = ≤50ms 短轮询批量提取（`_poll_new_events`，`seq >= cursor`，0 基 seq 不吞首帧）；**wire 无 since**（重连=重开全量） |
| `web/approvals.py` | `packages/interaction/user-approval` + `packages/api/remotes`（last-resort approval 转发）| 审批桥：async `tools/ask` 闸门 → `approval/request` waterfall（`$events`）+ `$events/result` 结算； outcome 映射 result∈APPROVAL_OUTCOMES（否则 unavailable fail-closed）/rejected→unavailable/next→nxt()/cancelled；审计对 approval/asked+decided；接线点在工具闸门（上游在 approval/request，教学简化） |
| `web/server.py` | `packages/api/gateway/src/{stream-server,index}.ts`（WS mux + 升级拒绝）+ `packages/client/connection/src/rpc.ts`（unary 载体语义） | FastAPI 载体：unary POST `{args}` 严格解包（`/api/<endpoint>`）+ `$events/result` 特判；载体状态码 404/415/400（token 门配置时 /api/* 另有 401，`web/auth.py`——上游 requestRejection 等价物），业务错误恒 200 + result.ok=false + server-response 信封；WS `/api/remote.mux`；`GET /api/session.export` 载体（query 校验→400、调 `build_session_export`）；SPA 静态 fallback；无 CORS（上游同款：靠 415 状态码挡跨站写入） |
| `web/downloads.py` | `packages/session-query/session-log-export/src/{archive,index}.ts`（导出域）+ `api/session-controller`（下载端点约定面） | 会话日志导出：parse_export_query（sessionId/includeDescendants）、SessionLogExportDeps、safe_session_id_segment、session_log_zip_filename、build_session_export（zip 条目序：根制品逐字原始文件名→后代 BFS+seen-set 去重→媒体、压缩等级缺省 6、私有错误安全壳）；测试 `tests/test_web_export.py` |
| `web/frontend.py` | `packages/host/frontend-static` | 静态服务约定：遍历 403 / SPA 回退 200 / MIME 按扩展 / 未知扩展 octet-stream；index taps 恒 identity（无 boot-manifest）；`DIST_ROOT` 默认 `web/static/`，经 `MINIHARNESS_WEBUI_DIST` 可指向产品化前端构建产物（`webui/dist/`），约定不变 |
| `web/inventory.py` | `packages/host/plugin-inventory/src/{index,types}.ts` + `packages/preset/agent-presets/src/composition-inventory.ts` + `discovery.ts`（entryListProblem） | `pluginInventory/list` 投影：`entries_snapshot`（Loader 非 group 条目四字段 `{entryId,moduleName,enabled,fiberPhase}`，无 loader 服务→空）、`file_composition`/`composition_inventory`/`build_inventory`（preset 组合行 flatten：组行跳过、组 disabled 继承、`!!js` 求值被拒→`'conditional'` + condition 原文、无 roster→省略 `agentPresets` 键）、`PluginInventoryService`（`ctx.pluginInventory`）；JSON-schema 方言与 `!!js` 求值子集同 loader。载体差异：preset.json 载体无插件行→`rows: []` |
| `web/static/`（index.html + app.js + style.css） | `packages/bundle/web-app` + `packages/client` | 教学参照 vanilla SPA（无构建步）：消费**旧 SSE wire**（`events.mux`/`respond`/`host.describe`），alpha.1 后端已删这些端点，故不对新后端工作，仅作历史/教学说明；产品化前端 = 仓库顶层 `webui/` 独立 React 工程（只依赖新 wire 约定，见 §3 三层边界） |
| `web/launcher.py` | `packages/host/webserver`（Config：host 两值 + port 0）+ `api/gateway` heartbeat | host/port 优先级「CLI `--host`/`--port`（经 cli/main 传入）> env MINIHARNESS_WEB_HOST/PORT > 缺省 127.0.0.1/0」；`0.0.0.0` 无 token fail-loud；**心跳与上游 gateway 约定一致**：`uvicorn_options()` 设 `ws_ping_interval=2 / ws_ping_timeout=4`（transport 级 Ping 2s + 连续 2 周期无 Pong 判定断开 ≈ 上游 `websocketHeartbeatIntervalMs` @default 2000 + `MAX_MISSED_HEARTBEATS=2` terminate） |
| `protocol/acp.py` | `packages/acp/acp` | 自动化专用 JSON-RPC 服务：initialize（`sessionCapabilities:{close,list,resume}`）、会话生命周期 new/resume/list/close（校验序逐字、keyset 分页 `page.at(-1)` 游标、selectionFor 恢复已提交路由）、模型选择标准配置 `set()`（model/reasoning_effort 逐字文案、切 model 复位 reasoning；目录经可选 `adapter.models_catalog`/`resolve_model_info()['reasoning']` 教学扩展承载）、prompt 同步完整回合（snapshot+pin、turnless/max-tokens/error 结算逐字）+ 更新流投影（agent_message_chunk 带 messageId、agent_thought_chunk、tool_call/_update completed/failed）+ 富媒体受理 + 一次性审批桥 + usage_update 发射（`request/context` 带 contextWindow + `Session.request_context()` + `_emit_usage_update`，assistant/message 带 usage 时发射）+ `session/update` 并发逐块流式通知（`_install_update_stream` 订阅 session/event 逐事件实时投影、`update_sink` 即时外发；in-process 载体收敛 `server.updates` 批量）**+ 可写入磁盘归档**：`AcpServer(persistence=...)` 可选 `JsonlPersistence` 后端——`new_session` declare + `_install_persistence_hook`（`session/event` append / `session/flush` flush）、`close_session` flush、`list_sessions` 合并磁盘 headers、`resume_session` 非 live 经 `repair_and_replay` 物化；磁盘写默认关闭（宿主装配持久化）；简化标注见模块 docstring（磁盘会话仅事件日志重建、不给 live turn 后台执行） |
| `protocol/sdk.py` | `packages/sdk/protocol` + `sdk/server` | messageId 为真实消息 id（与 inbox 回执一致，官方 SDK 依赖）；互操作测试 `tests/test_upstream_sdk_interop.py`（需 pydantic + 上游 SDK 源码，缺则 skip） |
| `protocol/hooks.py` | `packages/hooks/hook-protocol` + `hooks-claude-code` | 默认 runner 与 runner.ts 一致：stdin JSON payload + trailing newline、cwd、CLAUDE_PROJECT_DIR env、缺省 600000ms 超时；保留"异步 + signal"同步近似（subprocess） |
| `seams/sandbox_local.py` | `packages/sandbox/sandbox-local` + `sandbox-windows-acl` | landlock 后端经 `seams/landlock_run.py` ctypes 自限制执行器真执行（CLI 约定与 `native/landlock-run/docs/cli-contract.md` 一致：--ro/--rw/--/--probe、exit 125、报告行逐字）；Windows ACL 写限制由 `seams/sandbox_windows_acl/` 物化（见下行） |
| `seams/sandbox_windows_acl/`（12 模块） | `sandbox/windows-acl-restrict-poc`（单文件 index.ts 分区） | ctypes FFI 物化 WRITE_RESTRICTED 写入限制：win32_abi / ffi（替上游 koffi）/ errors / acl / token / workspace_sid / path_boundary / grant / spawn / index / runner 一一对应上游分区；runner 为 `python -m` CLI（exit 127 失败签名）；非 win32 平台 import 即抛 OSError；门控 e2e 见 tests/test_windows_acl_e2e.py |
| `seams/landlock_run.py` | `native/landlock-run`（C11 launcher） | ctypes 复刻同一 CLI 约定与 Landlock UAPI 语义（ABI 协商 / PATH_BENEATH 规则 / PR_SET_NO_NEW_PRIVS → restrict_self → execvp；full ⟺ 内核 ABI ≥ 5，否则 partial 但仍受限；非 Linux 宿主干净退出 125） |
| `seams/sandbox_policy.py` | `packages/sandbox/sandbox-policy` | ctx.sandboxPolicy：Config {mode 缺省 read-only, workspaceRoot} fail-loud 校验；resolve() = 显式 mode > 会话日志最后一条 `sandbox/mode`（session-mode.ts 的 effectiveSandboxMode fold）> 部署缺省，workspace 根先 canonical 后词法规范化、会话 cwd 即边界；三档策略上下文经 systemPrompt `.context('sandbox:policy', order=110)` 注册，loop 侧投影在变化时把快照注入对话消息流（`core/agent_loop/runtime_context.py`） |
| `shell/bash_local.py` + `bash_sandbox.py` + `helpers.py` | `packages/shell/{shell, bash-local, bash-sandbox}` | ctx.shell 前台 `bash -c` 执行器族：本地直跑 / 经 ctx.sandbox confine 包裹并报告 {mode, denied, enforcement}；三路归因与 helpers.ts 一致——runner 启动失败（ENOENT/EACCES 且 argv[0] 证据 + cwd 可用性独立校验）与 runner 失败规则命中抛 SandboxUnavailableError 且优先于 denial，denial = 非零退出 + stderr 大小写不敏感签名；danger-full-access 直通不包裹。后台进程未复现（mini 后台面是 jobs registry） |
| `seams/credentials_local.py` | `packages/credentials/credentials-local` | 文档为 version-1 JSON 布局 `{version:1, refs, records}`（上游 YAML）：fail-closed 解析 + 可识别 flat 文档启动自动迁移；记录服务侧五件套（read/describe/list/modify/delete_record + `.records` 只读视图，键语法 `[a-z][a-z0-9-]*`、写锁 `DOCUMENT_LOCK_WAIT_SECONDS=30`、modifyRecord 唯一写路径——锁内 reconcile + mutate + 写前准入 = 读路径选择）；**CredentialsService 桥接**：`CredentialsService(Service)` provide="credentials" 包装 `LocalCredentialProvider`，`modify_record`/`delete_record` 成功后发 `credentials/record-updated`(key)（无 carrier ancestor 路由）；`install_credentials(ctx, provider)` 装配；读侧热重载：读入口 `_refresh_if_changed()` 先 `os.stat` 比对 mtime/size、变了才整表重解析（外部编辑/删除即时生效），写路径 `_reconcile_from_disk` 折叠不变，原子写无撕裂读侧不需锁 |
| `seams/authorization.py` | `packages/credentials/authorization`（src/{index,types,invariant}.ts，全套 437 行） | `AuthorizationService(Service)` provide="authorization" + `install_authorization(ctx)`（显式 opt-in，依赖 `ctx.credentials` 缺失 fail-loud）。错误码闭集 DUPLICATE_FLOW/NO_FLOW/UNKNOWN_METHOD/ALREADY_IN_FLIGHT/NOT_COMMITTED/DECLINED；`registerFlow` effect 登记（disposer 注销，二次 DUPLICATE_FLOW）+ begin 复核 ALREADY_IN_FLIGHT；list/describe；begin 校验序 NO_FLOW→UNKNOWN_METHOD→ALREADY_IN_FLIGHT→pre-aborted（返回 cancelled 不占槽不 settle）→ 占槽（同步 `AbortSignal`）→ `_attempt`（`credentials/record-updated` 记账 + `describe_record` 二次确认、declined/pre-aborted → cancelled 不核 commit、未提交 → NOT_COMMITTED）→ `_settle` 发 `authorization/settled`（payload `(key, settlement)` 元组、listener 异常 contained）；简化标注：async→sync（begin 同步返回）、AbortSignal 无事件机、interaction 回调参数化 |
| `seams/subprocess_env.py` | `packages/subprocess/subprocess/src/index.ts` + `types.ts` | 环境清洗切片：SENSITIVE_ENV_PATTERN 凭据形启发式 + DSH_ENV_PREFIX 大小写不敏感剔除；显式 env 在 scrub 之后合并（providers spawn 层叠） |
| `seams/subagent/`（`__init__.py` + `descriptor.py` + `providers.py` + `worker.py` + `continuation.py` + `tool.py`） | `packages/subagent/subagent` + `subagent-fork-in-process` + `-acp` + `-dsh-sdk` + `subagent-spawn-in-process` + `subagent-in-process-driver` + `tool-subagent-control` + `tool-subagent-report` | 续跑 A8 为异步事件驱动（双路径：父有 driver → 投递即返回 + watchSettlement 结算 + steer 批内合并；无 driver → 回退同步 pump）。生命周期 scoped dispatch（委托父 scope 载体过滤，无标号退化祖先链）+ provider 注册表（register_provider → 注销发布 subagent/provider-removed）+ DRAINING 拒绝面（drain/drain_descendants/drain_children + assert_admitting 准入边界）+ report 工具逐字约定（output 参数、部署级 reportDelivery 'quiet'\|'next-step'、{messageId} 返回与 render）+ childId 预留 DUPLICATE_CHILD 断言已与上游一致；invariant 运行时校验架构不适用；同步模式结算投递走非唤醒 next-step |
| `seams/agent_team/`（13 模块） | `packages/experimental/agent-team` + `tool-agent-team`（src/index.ts） | **Agent Teams 实验族**：implicit-root roster + durable peer mailbox + shared task DAG。`types/validation/error/journal/projection`（`team/member`(v2)/`team/task`/`team/message/queued`/`team/message/delivered` 四事件全 log-only、payload 逐字段 zod-strict fail-closed、task revision 从 1 连续、图成环拒、写域 advisory 重叠校验）；`roster`（spawn/stop/interrupt/reconcile/live_children_by_root；成员以 `start_continuable(agent_options={provider})` 子会话承载，descriptor `agentProvider` 承载请求方 provider）；`mailbox`（Team authority queue → `steer_host_subagent` 冷/热三态路由）；`task_board`（8 动作 CAS 转移矩阵）；`activity`（with观测窗口 wait_for）；`lifecycle`（admit/JOINED 窗）；`service`（同步门面）；`tools`（9 工具 + `team:policy` 提示节，canonical value 全 fixed record）。**sync/async 双投递载体**：同步 `_spawn_admitted`（harness/CLI 无循环态）+ 事件循环内 `start_continuable_async`/`spawn_async`/`_checkpoint_initial_prompt_async`（await 让出控制，驱动载体不被 time.sleep 阻塞）。简化：wire/Remote 端点不承载（错误语义走 TeamError.code 闭集）、上游 `todo` 事件与 `client-ui-agent-team`/web-profile 前端 wire 面由 `webui/` 自行选择 |
| `seams/session_checkpoint.py` | `packages/session/session-checkpoint-policy/src/index.ts` | 语义持久化检查点（三屏障）：模型请求（`agent/checkpoint` boundary='request'，请求信封落日志后、adapter 派发前）+ 顶层工具（`tools/pre-execute`，`exec_.agent` 有且 `exec_.parent` 为空；取消折叠为 canonical `ABORTED_BEFORE_DISPATCH`）+ 步边界（`agent/pre-step`）；经 `SessionStore.checkpoint`（无持久化参与者 fail-closed）。载体差异：上游经 `llm/stream` 服务 waterfall 延迟适配器构造，mini 单一 adapter 直接调用故改用 `agent/checkpoint` waterfall。`install_checkpoint_policy(ctx)` opt-in |
| `ptc_runtime/`（types + service + runtime） | `packages/ptc-runtime/ptc-runtime/src/{index,types}.ts` + `ptc-runtime-node` + `packages/experimental/ptc-runtime-python` | PTC（programmatic tool calls，程序化工具调用）执行 seam：`PtcRuntime` Service Definition（`ctx.ptcRuntime`）+ 保留名常量（`RESERVED_BINDING_GLOBALS`/`RESERVED_ERROR_MEMBERS`/`PORTABLE_RESERVED_WORDS`/`DUNDER_MEMBER`）+ 绑定校验；`PythonPtcRuntime` 每请求在全新 CPython 子进程跑模型 Python（顶层 await/return），绑定经 stdin/stdout 行 JSON 协议桥接，墙钟预算/中止/输出上限 + 正交失败分类（exception/timeout/abort/worker-exit/invalid-output/output-limit/protocol）。载体差异：上游 Node 后端 worker/subprocess + fd-3 wire，mini CPython 子进程 + 行 JSON；子进程非安全边界（上游同款声明）。`install_ptc_runtime(ctx)` |
| `ptc/run_code.py` | `packages/core/tools/src/ptc.ts` | PTC 模式 `run_code` 工具（上游 `createRunCodeTool`）：程序经 `tools.<name>` 调用 agent 可见工具（嵌套子派发）；每次子派发落 `tool/ptc-dispatch-start` / `tool/ptc-dispatch`（`rootCallId`/`parentCallId`/`subCallId`=`<parent>:ptc:<n>`/`name`/`arguments`/`isError`/`content`）；只有外层精心挑选的结果进模型历史。`describe`/`parameters` 按 runtime 语言取 flavor（typescript/python）。载体差异：上游用 registry 分阶段调度接口（prepare/dispatch/finalize/finish）+ 并发池，mini 经 `run_pipeline` 顺序执行（事件序与 payload 形状对齐，并发上限简化登记） |
| `demo.py` | `packages/examples/agent-spine-demo` | 教学入口，保留顶层（`python -m miniharness.demo`） |
| `example_plugins.py` | `examples/` | 教学示例，保留顶层 |

## 3. 依赖方向规则

分层如下（Python 没有编译期模块边界，规则由 `tests/test_dependencies.py` 的 import 方向断言固定，违反即测试失败）：

| 层 | 内容 | 允许依赖 |
|---|---|---|
| L0 地基 | `core/session`、`core/scope`、`core/dsh_scope`、`core/schema`、`core/hmr`、`core/home_paths`、`core/tool_timeout`、`loader` | 无（互不依赖；core.scope ↔ core.dsh_scope / core.schema / core.hmr→core.scope / loader→core.scope 经 §3 例外豁免；core.tool_timeout 是超时约定常量叶，被 core.tools 与 guard 两侧共享） |
| L1 领域 | `llm/*`、`core/tools`、`core/system_prompt`、`core/session_store`、`core/agents`、`attachment`、`ptc_runtime`、`identity`、`storage`、`fs/*`、`boot/*`、`guard` | 仅 L0（fs 单元还注册模型侧工具进 `core.tools`——§3 规则 1 显式例外） |
| L2 编排 | `core/agent_loop`、`compaction`、`jobs`、`plan`、`commands`、`goal`、`skills`、`telemetry`、`session_query`、`todo`、`spill`、`workspace`、`settings` | L0 + L1 |
| L3 应用与入口 | `cli/*`、`protocol/*`、`seams/*`、`preset`、`extensions`、`interaction`、`client`、`mcp`、`web`、`shell` | L0 ~ L2 |
| 教学层 | `demo.py`、`example_plugins.py` | 任意层，但不得被业务模块依赖 |

**三层组织边界**（代码与结构上清晰分离、解耦）：

| 边界 | 内容 | 耦合面 |
|---|---|---|
| core 核心能力 | `miniharness/core/` 等（L0~L2 纯领域逻辑） | 不感知 web/CLI 传输载体 |
| 后端 web 服务 | `miniharness/web/` + `protocol/` 等（L3 传输层） | 消费 core 能力；对外发布 HTTP/WS wire 约定（信封/帧/错误语义） |
| 前端工程 | 独立工程形态：**`webui/`**（React+TS+Vite，产品化）走新 wire；`web/static/` vanilla SPA 降为教学参照（旧 wire，不实跑） | 只依赖后端发布的 wire 约定，禁止 import/hack Python 内部 |

前端唯一的耦合面是 `web/` 层发布的 wire 约定，不是 Python 内部 API——这保证前端可独立选用现代化的技术组合（如 React）而无需改造内核。

**`webui/`**（仓库顶层独立工程，React+TS+Vite）：产品化浏览器前端，只消费 `web/` 发布的 alpha.1 wire 约定（两信封 RPC `/api/<endpoint>` + `/api/remote.mux` WS 帧 + `$events`/`$events/result` + `session/follow`/`control`），零 Python import。三层结构：`src/wire/`（约定客户端层，纯 TS 可单测）、`src/app/`（React 编排 hooks）、`src/ui/`（无状态展示组件）；测试用 vitest（mock fetch/WS）。构建/运行手册见 `webui/README.md`：开发期 Vite dev server 把 `/api` 与 `/api/remote.mux` 代理到本地 Python 后端（`vite.config.ts`，目标经 `MINIHARNESS_WEBUI_PROXY` 覆盖）；生产期 `vite build` 产出 `webui/dist/`，后端 `web/frontend.py` 经 `MINIHARNESS_WEBUI_DIST` 指向该产物即可承载（`serve_static` 约定不变）。覆盖范围与教学 SPA 功能面一致（会话列表/新建、Trajectory（虚拟化窗口 + Overview 折叠跳转 + 全文搜索）、审批瀑布、队列/作业），不整体移植上游 `packages/client` 40 模块。

规则：

1. L_n 只依赖 L_{&lt;n}，禁止依赖同层或上层。九条显式例外：
   - `seams/subagent/worker.py` 依赖 `protocol/*`（同层）：worker 是 ACP / SDK 线协议的服务端载体，复用协议层的帧与信封实现；
   - `core/hmr.py` 依赖 `core/scope`（同层）：HMR 是 cordis 家族的 vendored 部件（上游 vendor/hmr 直接建在 cordis 之上），复用 Service/fiber 基座，与 core.dsh_scope 同理归属 L0；
   - `loader/*` 依赖 `core/scope`（同层）：loader 是 cordis 家族的 vendored 部件（上游 vendor/loader 直接建在 cordis 之上），复用 Service/fiber/Inject 基座，与 core.hmr 同理归属 L0；
   - `fs/*` 依赖 `core.tools`（同层，单方向）：fs 单元承载模型侧文件工具（上游 tool-fs 等是 fs 域的消费面，register 进 core.tools）；core.tools 不得反向 import fs；
   - `cli/main.py` 依赖 `web`（同层，单方向）：launcher 组装 web profile——cli 把 ctx/adapter/tools 交给 web 层运行时，web 层不得反向 import cli；
   - `cli/headless.py` 依赖 `seams` 与 `shell`（同层，单方向）：run_headless 组装沙箱后端链路与 bash 执行器装进 ctx——同上游 bundle/headless 依赖 dsh-sandbox / sandbox-policy / bash-sandbox 的包拓扑；seams/shell 层不得反向 import cli；
   - `shell/bash_sandbox.py` 依赖 `seams/sandbox_local`（同层，单方向）：bash-sandbox 是 ctx.sandbox 的消费者——上游 bash-sandbox 同样依赖 dsh-sandbox，拓扑一致而非分层倒挂；seams 层不得反向 import shell；
   - `mcp/connection.py` 依赖 `seams/subprocess_env`（同层，单方向）：stdio 子进程 env 组装复用 seam 的净身切片（上游 mcp-client spawn 透传 env），同 cli→seams 先例；seams 层不得反向 import mcp；
   - `cli/main.py` 依赖 `demo`（教学层）：无 profile 时以 `demo` 兜底（教学扩展入口）。
2. `protocol/` 内三个模块互不依赖（acp、sdk、hooks 各自独立）。
3. `seams/` 内 sandbox（sandbox_local + sandbox_policy）、credentials、subagent 互不依赖；policy 与 local 同属沙箱子域——上游 dsh-sandbox-policy 同样依赖 dsh-sandbox。
4. `seams/credentials_local.py` 从 `boot/dotenv.py` 导入 `parse_dotenv`（L3 → L1）：凭据文档解析复用 boot 层的 `.env` 解析器，方向合法。

## 4. 公共 API 面

**白名单（约定层，改它需要对照上游 + 更新差异清单）**：`Session`、`Context`、`RegistryService`、`Tool`、`ToolRegistry`、`AgentLoop`、`StreamChunk`、`LlmAdapter`、`DeepSeekAdapter`、`LlmFailure`、`SessionPersistence`/`JsonlPersistence`/`SqlitePersistence`、`apply_patch`、`boot`、`run_headless`、`create_message` 与四个 block 构造、`derive_messages`、`turn_balance`、`repair_interrupted_turn`、`SESSION_FORMAT_VERSION`、`TOOL_NOT_STARTED`、`TOOL_OUTCOME_UNKNOWN`。
**黑名单（内部工具，不在顶层 `__all__`，只允许深路径 import）**：`deep_freeze`、`thaw`、`is_json_safe`、`now_ms`、`_http_error_code`、`_map_finish_reason`、`load_events_checked`、`repair_and_replay`、`balanced_after_replay`。

**教学扩展（上游无对应，标注于此）**：`cli/default_tools.py`、`cli/session_cmds.py`（会话管理子命令；`--config` 属 `cli/main.py` 启动器标志，同为教学扩展）、`llm/fake.py`、`demo.py`、`example_plugins.py`。

顶层 `__all__` 收敛至 28 项（白名单 + `FakeLlmAdapter`），由 `tests/test_dependencies.py` 断言固定；白名单每一项都能在 §2 映射表里找到上游对应。

**深路径约定（不在顶层 `__all__`，仅经子包深路径暴露，由 `tests/test_token_meter.py`、`tests/test_compaction.py`、`tests/test_jobs.py`、`tests/test_plan.py`、`tests/test_skills.py`、`tests/test_session_store.py` 固定行为）**：`TokenMeter`、`install_compaction`、`CompactionEngine`、`compact_surface_region`、`select_compactable_range`、`inspect_compaction_entry_state`、`frame_summary`、`install_jobs`、`register_job_tools`、`LocalJobRegistry`、`JobDoneBox`、`fit_with_suffix`、`fit_completion_notice`、`install_system_prompt`、`SystemPromptService`、`install_plan_mode`、`PlanModeController`、`fold_plan_mode`、`resolve_config`、`install_skills`、`register_skill_tools`、`SkillRegistry`、`FileSystemSkillProvider`、`SkillTool`、`SKILL_GESTURE`、`render_skill_content`、`parse_skill_file`、`digest_catalog_entries`、`install_sessions`、`SessionStore`、`SessionForkError`、`SESSION_NOT_FOUND`、`SESSION_NOT_LIVE`、`SESSION_ALREADY_EXISTS`、`INVALID_BOUNDARY`、`OPEN_TURN`、`LocalAttachmentStore`、`AttachmentStore`、`ImageAttachmentRef`、`SaveImageAttachment`、`ImageAttachmentLimits`、`AttachmentError`、`is_image_admission_error`、`detect_image`、`probe_image`、`supports_acp_image_prompts`、`admit_acp_prompt`、`assistant_block_to_acp`。装配约定：`apply_retry_planner(ctx)` → `install_compaction(ctx)` → `install_jobs(ctx)` → `install_system_prompt(ctx)` →（可选）`install_plan_mode(ctx, config)` →（可选）`install_skills(ctx)` →（可选）`install_sessions(ctx)`（均幂等；`CONTEXT_WINDOW_EXCEEDED` 不在重试白名单，由压缩接管；作业工具注册经 `register_job_tools(reg, ctx.get("jobs"))`，`default_tools` 在 `ctx.jobs` 存在时自动收编；skill 工具注册经 `register_skill_tools(reg, ctx.get("skills"))`，`default_tools` 在 `ctx.skills` 存在时自动收编；plan 依赖 systemPrompt 服务，缺失 fail loud；会话经 `install_sessions(ctx)` 提供 `ctx.sessions`，headless / demo / resume 入口已接入）。