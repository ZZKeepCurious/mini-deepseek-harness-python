# MiniHarness 架构说明

> 本页是 `miniharness/` 代码自身的"建筑图纸"：目录怎么组织、每个文件对应上游什么、依赖方向规则、公共 API 边界。
> 读者是改代码的人，以及想理解仓库布局的学习者。
> 与其它文档的分工：`docs/report/` 解读上游系统"是什么、为什么"；`docs/chapters/` 教你怎么从 0 到 1 实现；本页回答"仓库里的代码本身怎么摆、凭什么这么摆"。

## 1. 目录组织

### 1.1 原则：目录按上游包家族镜像

代码目录镜像上游的包族结构（`packages/` 下的 `core/`、`llm/`、`boot/`、`sandbox/`……），镜像到**家族粒度**（两级子包），不镜像到每个包，也不按主题平铺：

- 家族级镜像：约 20 个子包，维护成本低，"去哪个目录找什么"与上游一致；
- 文件级镜像：只做契约密集处（session、llm、agent-loop），这几处上游"一个文件一个职责"本身就是知识点；
- 为什么不是 1:1 镜像全部近 50 个包？Python 一个仓库拆 50 个目录，光 `__init__.py` 就有 50 个，对教学项目是过度工程；
- 为什么不是主题平铺？平铺表达不了模块边界：无法声明"哪些是契约、哪些是实现细节"，依赖方向无法用测试约束，环压力只能靠延迟导入绕。

### 1.2 目录树

```
miniharness/
├── __init__.py            # 教学面再导出，只含契约层（__all__ == 28，见 §4）
├── core/                  # packages/core
│   ├── session/           # Session 本体 + types/invariant/json/message/repair/surface，__init__.py 聚合
│   │   │                  #   message.py 上游在 llm/llm/src/message.ts，mini 保留会话域（L0 不依赖 llm，简化标注）
│   │   ├── chunk_rows.py # StorageRecord 打包行（assistant/chunk 连续段，MIN_RUN=3，对齐 core/session/src/chunk-rows.ts）
│   │   ├── zstd_frames.py # zstd 拼接帧容器扫描/解码/截断前缀恢复（python-zstandard）
│   │   └── persistence.py # JSONL(zstd 帧容器/明文) / SQLite 持久化（上游独立包组 packages/session）
│   ├── session_store.py   # SessionStore（ctx.sessions 服务：create/prepare/enter/announce + fork + flush）
│   ├── scope.py           # Context + RegistryService（vendor/cordis 语义）
│   ├── dsh_scope.py       # dsh-scope 原语（scopeParents 图 + scopeTarget 载波 + createScope，对齐 packages/core/scope）
│   ├── hmr.py             # Cordis HMR 服务（vendor/hmr：register_config watch + 单飞刷新 + config-update-failed 外泄）
│   ├── schema.py           # schemastery 配置引擎全量移植（vendor/schemastery）
│   ├── tools.py            # 工具注册表 + 执行管线
│   ├── system_prompt.py   # SystemPromptService（分节渲染，systemPrompt 服务）
│   └── agent_loop/        # agent.py（turn/step 状态机）+ resident_loop.py（常驻单循环）+ tool_calls.py（并行调度）+ inbox.py（双队列收件箱）
├── llm/                   # packages/llm
│   ├── protocol.py        # StreamChunk / LlmAdapter / LlmFailure / BlockAssembler（协议层）
│   ├── deepseek.py        # DeepSeek wire 序列化 + SSE 适配器
│   ├── fake.py            # FakeLlmAdapter（教学扩展）
│   ├── retry_policy.py    # retry policy 解析（normal/always）
│   ├── retry.py           # agent/request-error 恢复 + 退避
│   └── token_meter.py     # TokenMeter 增量 fold + usage 折入锚
├── attachment/             # packages/attachment（attachment + attachment-local）
│   ├── types.py            # ImageAttachmentRef（含 originalDimensions）/ SaveImageAttachment / ImageAttachmentLimits / ImageRequestPolicy / RequestImageAttachment
│   ├── error.py            # AttachmentError + 15 错误码 + is_image_admission_error
│   ├── encoding.py         # 共享质量阶梯 [85,75,60] + encodeFirstWithinLimit 惰性候选执行
│   ├── normalization.py    # provider 无关规范化管线（直通/总像素预算+长边封顶/按 alpha 分流编码）
│   ├── projection.py       # requestImageDimensions 纯请求投影几何（alpha.1 抽到 seam 包）
│   ├── request_image.py    # variantId 确定身份的请求图缓存版本
│   ├── admission.py        # canonical base64 wire 受理入口
│   └── store.py            # LocalAttachmentStore（规范化字节 sha256 内容寻址 + 完整性复验）
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
├── commands/              # packages/interaction/commands（命令契约）
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
├── boot/                  # packages/boot
│   ├── boot.py            # 启动 + patch overlay
│   ├── composition.py     # YAML 配置 / !!js 插值 / dump 渲染
│   └── dotenv.py          # .env 解析（parse_dotenv）
├── cli/                   # apps/cli
│   ├── main.py            # launcher 选项（profile / patch / dump）
│   ├── headless.py        # 一次性任务入口
│   ├── default_tools.py   # headless 默认工具集（教学扩展）
│   ├── session_cmds.py    # 会话 list / resume / delete（教学扩展）
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
├── seams/                 # packages/{sandbox, credentials, subprocess, subagent}
│   ├── sandbox_local.py   # 真沙箱后端（平台链探测 / 失败即拒绝）
│   ├── sandbox_policy.py  # 沙箱策略服务（部署缺省 + 会话日志覆盖决议）
│   ├── landlock_run.py    # Landlock 自限制执行器（native/landlock-run 的 ctypes 载体）
│   ├── sandbox_windows_acl/ # Windows ACL 写限制沙箱（ctypes FFI 物化上游 windows-acl-restrict-poc；非 win32 import 即抛 OSError）
│   ├── credentials_local.py # 凭据四层
│   ├── subprocess_env.py  # 子进程环境清洗切片（凭据形 + DSH_* 名剔除，packages/subprocess）
│   └── subagent/          # __init__ + descriptor + providers（三通道）+ worker（子进程）+ continuation（续跑管理）+ tool（模型侧委托工具）
├── shell/                 # packages/shell/{shell, bash-local, bash-sandbox}
│   ├── bash_local.py      # 本地 bash 执行器（ctx.shell 缺省 provider）
│   ├── bash_sandbox.py    # 沙箱消费执行器（confine 包裹 + 三路归因）
│   └── helpers.py         # spawn 归因 / denial / runner 失败分类（helpers.ts）
├── client/                # packages/client
│   └── trajectory.py      # Trajectory 折叠引擎
├── web/                   # packages/api/gateway + packages/client/connection + session-controller + remotes + host/frontend-static + host/webserver（mini 子集）
│   ├── envelope.py        # 两信封 RPC（client-request / server-response，rpc-schema.ts：connection 层错误闭集 + transport_error 折叠）
│   ├── api.py             # WebApi 会话服务（unary 方法 + 路由表）
│   ├── stream_protocol.py # Remote 流 wire 语法（open/cancel/item/end/error 帧 + $events/result payload）
│   ├── mux.py             # WS /api/remote.mux 单路径承载全部 Remote 流（RemoteStreamMuxConnection）
│   ├── events.py          # $events 注册表（api-session/* 转发源 + waterfall + $events/result 结算）
│   ├── streams.py         # GatewayStreams（$events 装配 + session.follow/control 流分发表）
│   ├── approvals.py       # 审批桥（async tools/ask 闸门 ↔ approval/request waterfall + $events/result）
│   ├── downloads.py       # GET /api/session.export 会话日志导出（zip 打包 root + 后代 + 媒体）
│   ├── frontend.py        # 静态服务契约（遍历 403 / SPA 回退 200 / MIME）
│   ├── static/            # vanilla SPA 教学参照前端（index.html / app.js / style.css，无构建步；消费旧 SSE wire，对新后端不工作——真实对接见仓库顶层 `webui/`）
│   ├── server.py          # FastAPI 载体（unary {args} 解包 + $events/result 特判 + WS + 静态，stream-server.ts / handler.ts）
│   └── launcher.py        # web profile 启动器（build_app / run_web）
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
| `core/session/persistence.py` | `packages/session/session-persistence-{jsonl,sqlite}` | 上游是独立包组，mini 并入会话域（简化标注）；目录布局对齐上游 `session-persistence-jsonl/src/format.ts`：`root/--<projectKey(cwd)>--/<encodeSegment(id)>/session.jsonl[.zstd]`（`~XXXX` 段转义、projectKey 分隔符折叠+251 截断、cwd 缺省 `_no-cwd`）；**默认载体 zstd 拼接帧容器**（`zstd_frames.py`，一帧一记录/打包行，torn 末帧前缀恢复；可选明文 `.jsonl`），编码互斥/遗留平铺/重复 id/版本双向拒读均响亮拒绝（逐字文案）；头行与目录同源（header.cwd 回写）；崩溃修复 closers 经 `commit_repair` 落盘（对齐上游 commitRepair） |
| `core/session/chunk_rows.py` | `packages/core/session/src/chunk-rows.ts` | StorageRecord 打包行全语义移植：MIN_RUN=3、assistant/chunk 白名单分类与连续性、行信封 `{type,seq0,time0,data:{turn,step,index,dt[,id][,name],texts|args}}`、validate_row fail-closed 先于 expand_row |
| `core/session_store.py` | `packages/core/session/src/index.ts`（SessionStore 部分） | 内存会话服务：create/prepare/enter/announce 生命周期 + get/list/fork（五错误码）+ flush 检查点 + `session/created|disposed|event|flush` 四事件；create 对齐上游 generator effect（enter 先 yield、announce 抛错自动回滚）；事件派发经 scope_target 载波（carrier=scope_target(session, scope_of(owner_ctx or self.ctx))，对齐上游 enter 的 scopeTarget(session, scopeOf(store.ctx))）；**结构对齐（§3.2 已闭合）：`SessionStore(Service)`，构造 `super(ctx, "sessions")` 即经 ctx.provide 自动登记、随拥有 fiber 自动注销（对齐上游 index.ts:790 `extends Service` + `super(ctx, 'sessions')`），install_sessions/web/api 手工 provide 已简化移除**；无 typert lookup、flush 为同步近似（简化标注见模块 docstring） |
| `core/agents.py` | `packages/core/agent/src/index.ts`（AgentRegistry） | 进程内 live 代理实例注册表（ctx.agents，L1）：`AgentRegistry(Service)` 构造即 `super(ctx, "agents")` 登记、随拥有 fiber 注销；`register(agent, owner=None)`（id 与会话不符 / 同 id 已登记 fail-loud）、查询面 get/list/roots/is_owned_by、`agent/created`/`agent/disposed`（agent 自有载波，非会话日志事件；对齐上游 publish 时 enter+announce 公告，mini 单同步进程一键发布）；`install_agents(ctx)` 幂等装配（生产 6 处 root 组合均接在 install_sessions 旁）；模块级 `assert_live_agent(agent)`——装配即强制（无 agents 服务的裸装配 no-op）。注册点 = `AgentLoop.publish()`；jobs `_assert_access` / goal `_prepare_mutation` 共用 assertLive 边界（陈旧/重复实例拒绝）。载体差异：上游 initiator AsyncLocalStorage 机制与 enter+announce 两步、subagent 运行时 owner 链不承载（本节只登记 root/父子全部 live 实例），见 verified-diffs §2.15 |
| `core/scope.py` | `vendor/cordis` + `packages/core/scope` | Context（服务仓库 + 事件总线 + 四种派发 + asyncio 变体）+ fiber 生命周期（对齐 fiber.ts：状态机 PENDING/LOADING/ACTIVE/FAILED/UNLOADING/DISPOSED + `internal/status`；`effect(execute, label)` 上游形态——execute 立即执行、返回值按 None/callable/awaitable/生成器收集为 disposer；单发 + 可 await；注册先于执行 + setup barrier 重入保护；dispose 幂等 join 在途；同步立即逆序、异步并发 unload、错误 contained；装载半边 + 注册表——`RegistryService`（`ctx.plugin` 缩写 + 插件形态归一 + 运行记录按 callback 键控）、fiber 携带 inject 依赖 + epoch 重载（依赖变化卸载→重装）、`restart()`/`update()`（`internal/update` waterfall）、`internal/config` waterfall + schema 校验（`resolve_config`，`core/schema.py`）、`internal/plugin` 每次装载/卸载派发）+ `create_scope` fiber-backed 作用域（父拆解收回子 fiber）+ 服务仓库（reflect.ts 对齐：按隔离标签键控的全局 store，`ctx.get` strict 缺省返回 None，`ctx.isolate(name)` 换标签，per-agent 的 tools/systemPrompt 经隔离不冲撞 root realm，重复提供同一标签 fail loud）；另含 `Service` 基类（service.ts 对齐：构造即经 `ctx.provide` 自动登记、随 fiber 注销、`_invoke` 可调用、`_check`/`_init`）、`ctx.extend(meta)`/`ctx.intercept(name, config)`（intercept 配置经 `Service._resolve_config` 沿祖先链近根优先合并）、内建 `LoggerService`（logger.ts 对齐：`ctx.logger(name)` 铸具名 Logger 门面 + printf 格式 + exporter 注册/级别过滤/默认缓冲导出器，`ctx.logger` 属性为绑定访问方 ctx 的视图）；dsh-scope 对齐：事件派发模型改上游形态——`on` 双写 root `_flat_hooks`（全局 Hook 表）+ 祖先链 `_listeners`，dispatch 系加 `this_arg` 载波参数（有载波走扁平表按载波键过滤，无载波保留祖先链）；`create_scope` 返回 Scope 包装 + 自动绑父 scope；`scope_key` = `scope_of(self)`（scopeParents 图）；残余简化标注见模块 docstring |
| `core/schema.py` | `vendor/schemastery/src/index.ts`（902 行单文件） | schemastery 引擎全量移植：可调用 Schema 节点 + `resolve` 分发 + 17 类 resolver（any/never/const/string/number/boolean/function/is/bitset/array/dict/tuple/object/union/intersect/transform/lazy + date/regExp/arrayBuffer 复合体）+ meta 克隆语义 + from/extend + ValidationError(`$path` 前缀) + Options(autofix/ignore/path/strict) + toString 全 formatter + toJSON(uid+refs 共享序列化) + i18n(mergeDesc) + simplify(deepEqual dict-aware)；`S.from_/is_/reg_exp/array_buffer` 等 pythonic 命名（上游名映射进 docstring）；cordis fiber 适配层（`resolve_config`/`ValidationError` 聚合消息）保留文件尾部；L0 叶零内部依赖，cosmokit 助手(deepEqual/isNullable/isPlainObject/clone/valueMap/pick/Binary)内联；callback 不做字符串求值、date/regExp/arrayBuffer 锚定 Python 对应物（载体差异标注） |
| `core/dsh_scope.py` | `packages/core/scope/src/index.ts` + `store.ts` | dsh-scope 协议本尊（纯库，L0）：ScopeKey 弱引用身份键、scopeParents 图（bind/link/rebind + 环检测）、`scope_parent_of`/`scope_chain_of`（nearest-first）、`scope_target`/`_ScopeCarrier`/`is_scope_carrier`/`carrier_key_of`、`scope_of`（parent 链）、NamedEntries/AnonymousEntries/ScopedLayers 对齐 store.ts；`Context.create_scope` 返回 Scope 包装（delegation 包装，`__slots__` 无 `__dict__`） |
| `core/hmr.py` | `vendor/hmr/src/index.ts` | Cordis HMR 服务：`Hmr(Service)` provide="hmr"；`register_config(filename, refresh)`——findWatchRoot walk-up 根定位（realpath+depth+缺盘拒绝）、重复注册拒绝、初扫已存在目标即刷（chokidar ignoreInitial=false 语义：缺文件无初扫）、disposer 注销+关 watcher+join 在飞刷新；`refresh_config(key)` 单飞+dirty 合并循环、失败 logger.warn + `hmr/config-update-failed` 并行事件外泄不毒化循环；销毁期注册归一 `CordisError(INACTIVE_EFFECT)`。载体 watchdog（上游 chokidar）；Node ESM 模块图热重载（ModuleLoader/externals/accepted）不适用 Python 载体；Windows 短路径两侧 normcase+realpath 归一 |
| `core/tools.py` | `packages/core/tools` | 作用域化注册表（ScopedLayers/NamedEntries 存储：注册即 effect 归目标 fiber、拆解自动注销；resolve/names 缺省视角 = 注册表 root 的 scope 键，显式 scope 沿键父链最近者胜 + 全局层兜底）+ 守卫执行管线（pre/execute/post waterfall + schema 校验 + 超时） |
| `core/agent_loop/agent.py` | `packages/core/agent-loop/src/agent.ts` | 单一 async 泵（`_pump_async`/`_run_step_async`）+ `followup`/`steer` 同步门面（经常驻单事件循环驱动，见下行 resident_loop）+ 协作式取消（`_cancel_event` 每轮新建 + `call_soon_threadsafe` 跨线程置位——对应上游 agent.ts:325 每 phase 新建 AbortController）；agent/pre-step 决策经 `awaterfall`；publish/dispose 生命周期（enter+announce+agent/session-start / cancel(disposed)+scope.dispose+detach，会话店成员资格归 loop）+ agent/* 事件载波派发（scopeTarget(agent, loop scope 键)，兄弟作用域隔离）+ turn/step 编号 1 起经 `_replayed_next_turn` 从会话日志续号（对齐 invariant.ts `nextTurn`：turn/end 闭合后 +1、尾部未闭合停在当前号；resume 冷重建 loop 不重置回合号） |
| `core/agent_loop/resident_loop.py` | （无独立文件：Node 进程固有单事件循环） | 教学扩展：进程级懒加载单例循环（守护线程 run_forever）；`run_on_resident` 阻塞提交协程、异常冒泡、主线程 Ctrl+C 协作取消在途泵；同步门面由此驱动后跨调用共享同一循环，与上游形态一致 |
| `core/agent_loop/tool_calls.py` | `packages/core/agent-loop/src/tool-calls.ts` | |
| `core/agent_loop/inbox.py` | `packages/core/agent-loop/src/inbox.ts` | 双队列（followup→next-turn / steer→next-step）+ `agent/inbox/spliced` 持久化 |
| `core/agent_loop/runtime_context.py` | `packages/core/agent-loop/src/runtime-context.ts` | loop 侧运行时上下文投影：retained 三态（undefined/null/{seq,text}）restore（倒序找最近一条仍在 surface 的 owned 快照）+ 按追加序惰性消化新事件；`project(current, sections)` 文本相等去重、变化铸快照 user 消息（sections 非空带 `form:'snapshot'` 归因，空即 CLEARED 哨兵不带归因）；SOURCE/CLEARED 逐字对齐；接线在 `_run_step_async` pre-step waterfall 前（默认进入把快照追加在 claimed 之后，显式 enter 决策整体接管） |
| `core/system_prompt.py` | `packages/core/system-prompt/src/` | assemble waterfall + contexts/tools/variables 提供器 + `{{variable}}` 严格插值 + `render_context_sections`/`join_context_sections` 节渲染面（上游 renderContextSections/joinContextSections）；scope 层叠、assembly.tools→请求工具集成未复现（简化标注见模块 docstring） |
| `llm/protocol.py` | `packages/llm/llm/src/` | `stream(messages, tools, signal)` async 契约 + `StreamAborted` + `_aiter_raced`（异步迭代与 abort 事件竞速，asyncio 原生载体） |
| `llm/deepseek.py` | `packages/llm/llm-deepseek/src/` | httpx 异步传输（原生 asyncio，abort 置位即关闭连接、真取消）+ per-read idle 300s watchdog（对齐上游 fetch）+ SSE spec-strict 解析 |
| `llm/fake.py` | 无 | 教学扩展 |
| `attachment/`（types + error + image + encoding + normalization + projection + request_image + admission + store） | `packages/attachment/attachment`（seam + types + error + admission + request-projection）+ `packages/attachment/attachment-local`（store + image + encoding + normalization + request-image） | sharp→Pillow（权威全量解码/EXIF 定向/重编码）；规范化管线（总像素预算 + 长边封顶 + 共享质量阶梯按 alpha 分流）与 variantId 请求图缓存（request-image-v5）对齐 alpha.1；CompressionLimiter 并发闸与 SharedRequest 单飞登记架构不适用（同步载体天然串行）；显式 root（上游 DSH_HOME/attachments/v1）；见 verified-diffs §3.9 |
| `llm/retry_policy.py` | `packages/llm/llm/src/retry-policy.ts` | |
| `llm/retry.py` | `packages/llm/llm-retry/src/` | async 恢复决策（派发前熔合信号检查 + always 派发后复查中止胜过决策）+ 事件驱动多信号竞速可取消等待（等价 `AbortSignal.any`；裸测试替身信号回退轮询）+ 插件 effect teardown（注销监听器 + lifetime.abort + 排干在途恢复） |
| `llm/token_meter.py` | `packages/llm/token-meter/src/` | |
| `compaction/`（config + region + summarizer + engine + tool_result_pruner） | `packages/compaction/compaction-basic/src/` + `compaction-tool-result-pruner/src/`（config / region / summarizer / index.ts） | 前缀重放无 KV cache 语义；toolResultPruner 可选阶段已对齐（`compaction/tool_result_pruner.py`，上游注入 `ctx.toolResultPruner`，mini 经 `ctx.get('toolResultPruner')` 取用） |
| `jobs/`（types + registry + tools） | `packages/jobs/`（seam + jobs-local + tool-jobs） | controller/监听器按 scope 分层（P1-4a）；canonical value + render 分离；finalizeContent 可见输出二次封顶（job_output/job_kill）；`_assert_access` 前置 `assert_live_agent`（R4 agent registry，`core/agents.py`）；`run_in_background` 触发入口经模型侧 `subagent` 工具复现（简化标注见模块 docstring） |
| `plan/`（config + mode + review + projection） | `packages/plan/plan-mode/src/` | 状态机 + plan:policy 节 + 审查 UI（exit_plan_mode / /plan / userQuestions）+ plan 投影；canonical value + Tool.render 已对齐（简化标注见模块 docstring） |
| `commands/` | `packages/interaction/commands/src/` | 命令注册/派发 + `command/run|done` 配对 + commands/change 通知 + normalizeResult fail-loud；handler 签名 `(agent, raw)` 为教学扩展（简化标注见模块 docstring） |
| `goal/`（domain + service + prompt + driver + tools + commands） | `packages/goal/`（goal + goal-round-driver + tool-goal + command-goal） | Typert remote（上游命令由 human UI 表面派发，mini 用 `/goal` 命令承载）；`_prepare_mutation` 前置 `assert_live_agent`（R4 agent registry）；driver 模式事件驱动续跑（同步门面保留 `continue_rounds`）；权威判定近似；三工具 canonical value + render 已对齐（简化标注见模块 docstring） |
| `skills/`（registry + filesystem + tool_skill） | `packages/skill/`（skill + skill-filesystem + tool-skill） | 无 chokidar watch、无 ctx.fs 适配；skill 工具 canonical value + render 已对齐（简化标注见模块 docstring） |
| `boot/boot.py` | `packages/boot/app-boot` | `load_optional_patches`（缺文件→空层、坏文件 fail loud）+ `watch_user_patches`（对齐 app-boot watchUserPatches：经 HMR 服务 watch 用户补丁层→刷新重挂；上游经 Include entry.update() 事务性重挂，mini 无 Include 插件由宿主供 remount 回调） |
| `boot/composition.py` | `packages/boot/app-boot` + `apps/cli/src/args.ts` | |
| `boot/dotenv.py` | `packages/boot/app-boot`（loadEnv） | |
| `cli/main.py` | `apps/cli/src/args.ts` | |
| `cli/headless.py` | `packages/bundle/headless` + `apps/cli` | |
| `cli/default_tools.py` | 无 | 教学扩展（上游是工具插件注册） |
| `cli/session_cmds.py` | 无 | 教学扩展（上游会话管理在 web 表层） |
| `cli/preset_cmds.py` | 无 | 教学扩展（上游 preset 管理是 web 表层 Remote：list/read/deletePreset/selectPreset）——`miniharness presets` 子命令，投影/锁/删除语义对齐，不落 `agent-preset/selected` |
| `preset/presets.py` | `packages/preset` + `apps/cli/config/agent-presets` | shipped root(system) + 多根 first-root-wins roster、project_preset/project_session_agent_preset 投影、PresetLockedError、PresetNotWritableError、mount 作用域审计；YAML 翻译（agent.cordis.yml → Preset）；数据目录 `preset/{minimal,standard}` |
| `extensions/dynamic.py` | `packages/extensions/*` | |
| `interaction/approval.py` | `packages/interaction/user-approval` | |
| `client/trajectory.py` | `packages/client/ui-trajectory` | |
| `web/envelope.py` | `packages/client/connection/src/{rpc-schema,rpc}.ts` | 两信封消息联合（client-request / server-response）+ 连接层错误闭集（含 R3 新增 `gateway/input-invalid`）；transport_error 折叠兜底码 ‘internal’ |
| `web/api.py` | `packages/api/session-controller/src/index.ts`（session 域辅助入口）| WebApi unary 方法（list/search/create/selectModel/modelCatalog/canOpenWorkspacePath/openWorkspacePath/rename/fork/prompt/attachment/updateQueue/cancel/page）+ 路由表；`session/queue` placement 三态经 `session.control` 投影 |
| `web/args.py` | `packages/api/gateway/src/index.ts`（assertExactArguments:1112 / decode:1140）+ `remote-error-codes.ts` | 路由层 `{args}` 边界校验：每方法字段集合精确匹配（missing/unexpected → `gateway/arguments-invalid`）+ 顶层 JSON 类型（错型 → `gateway/input-invalid`）；`TypertGatewayFaultDetails{endpoint, field?}`；枚举/范围/非空/跨字段语义留 handler（业务码） |
| `web/stream_protocol.py` | `packages/api/gateway/src/stream-protocol.ts` | Remote 流 wire 语法：`open`/`cancel`/`item`/`end`/`error` 帧、`$events` 打开与 `$events/result` payload 解析、无损 JSON 判定（dict 键须 str、float 有限非 -0） |
| `web/mux.py` | `packages/api/gateway/src/create-mux-websocket.ts`（RemoteStreamMuxConnection）| 单条 `/api/remote.mux` WebSocket 承载全部 Remote 流；open/cancel/item/end/error 帧往返，二进制 1003/非法 1008 关闭码，隔离单流失败 |
| `web/events.py` | `packages/api/gateway/src/index.ts`（remote-event）+ `packages/api/session-controller`（api-session/*）+ `packages/api/remotes` | `$events` 注册表：open 首帧 `ready`{clientId, host.home} → 转发 emit/waterfall/cancel；api-session/* 转发源（created/disposed/status/error/activity）；waterfall 经 `$events/result` 结算（result/next/rejected/cancelled），未知 clientId fail-closed |
| `web/streams.py` | `packages/api/session-controller/src/{index,remote-events}.ts` | GatewayStreams Remote 方法面：session.follow（快照 snapshot{header,cursor,records,hasMore,projections} + 逐条 event）+ session.control（baseline{queues,jobs} + 实时 queue/jobs）+ `$events` 装配；跨堆非阻塞唤醒线程安全。活体 event 载体 = ≤50ms 短轮询批量提取（`_poll_new_events`，`seq >= cursor`，0 基 seq 不吞首帧）；**wire 无 since 已是核实结论**（重连=重开全量，见 §3.4） |
| `web/approvals.py` | `packages/interaction/user-approval` + `packages/api/remotes`（last-resort approval 转发）| 审批桥：async `tools/ask` 闸门 → `approval/request` waterfall（`$events`）+ `$events/result` 结算； outcome 映射 result∈APPROVAL_OUTCOMES（否则 unavailable fail-closed）/rejected→unavailable/next→nxt()/cancelled；审计对 approval/asked+decided；接线点在工具闸门（上游在 approval/request，教学简化） |
| `web/server.py` | `packages/api/gateway/src/{stream-server,index}.ts`（fetch）+ `packages/host/apiproxy/src/fetch/handler.ts` | FastAPI 载体：unary POST `{args}` 严格解包（`/api/<endpoint>`）+ `$events/result` 特判；载体状态码 404/415/400，业务错误恒 200 + result.ok=false + server-response 信封；WS `/api/remote.mux`；`GET /api/session.export` 载体（query 校验→400、调 `build_session_export`）；SPA 静态 fallback；无 CORS（上游同款：安全机制 = 415 跨站写围栏） |
| `web/downloads.py` | `packages/host/apiproxy/src/session-export.ts` + `api/downloads.ts` + `api/downloads.schema.ts` | 会话日志导出：parse_export_query（sessionId/includeDescendants）、SessionLogExportDeps、safe_session_id_segment、session_log_zip_filename、build_session_export（zip 条目序：根→后代 BFS+seen-set 去重→媒体、压缩等级缺省 6、私有错误安全壳）；测试 `tests/test_web_export.py` |
| `web/frontend.py` | `packages/host/frontend-static` | 静态服务契约：遍历 403 / SPA 回退 200 / MIME 按扩展 / 未知扩展 octet-stream；index taps 恒 identity（无 boot-manifest）；`DIST_ROOT` 默认 `web/static/`，经 `MINIHARNESS_WEBUI_DIST` 可指向产品化前端构建产物（`webui/dist/`），契约不变 |
| `web/static/`（index.html + app.js + style.css） | `packages/bundle/web-app` + `packages/client` | 教学参照 vanilla SPA（无构建步）：消费**旧 SSE wire**（`events.mux`/`respond`/`host.describe`），alpha.1 后端已删这些端点，故不对新后端工作，仅作历史/教学说明；产品化前端 = 仓库顶层 `webui/` 独立 React 工程（只依赖新 wire 契约，见 §3 三层边界） |
| `web/launcher.py` | `packages/host/webserver`（Config：host 两值 + port 0）| host/port 读 MINIHARNESS_WEB_HOST/PORT（上游组合配置节，简化标注）；**心跳已闭合**：`uvicorn_options()` 设 `ws_ping_interval=30 / ws_ping_timeout=None`（transport 级 Ping，不强制 Pong，对齐 `stream-server.ts` heartbeat） |
| `protocol/acp.py` | `packages/acp/acp` | 自动化专用 JSON-RPC 服务：initialize（`sessionCapabilities:{close,list,resume}`）、会话生命周期 new/resume/list/close（校验序逐字、keyset 分页 `page.at(-1)` 游标、selectionFor 恢复已提交路由）、模型选择标准配置 `set()`（model/reasoning_effort 逐字文案、切 model 复位 reasoning；目录经可选 `adapter.models_catalog`/`resolve_model_info()['reasoning']` 教学扩展承载）、prompt 同步完整回合（snapshot+pin、turnless/max-tokens/error 结算逐字）+ 更新流投影（agent_message_chunk 带 messageId、agent_thought_chunk、tool_call/_update completed/failed、projected_seq 增量）+ 富媒体受理 + 一次性审批桥 + usage_update 发射（`request/context` 带 contextWindow + `Session.request_context()` + `_emit_usage_update`，assistant/message 带 usage 时发射）+ `session/update` 逐条通知（worker 按 updates 逐条排发，provider 按 agent_message_chunk 折叠）；简化标注见模块 docstring（close 内存归档、选择施加为信封级——实际流经单一 adapter 实例；`session/update` 仍为同步回合结束后批量排发非并发流式） |
| `protocol/sdk.py` | `packages/sdk/protocol` + `sdk/server` | messageId 为真实消息 id（与 inbox 回执一致，官方 SDK 依赖）；互操作测试 `tests/test_upstream_sdk_interop.py`（需 pydantic + 上游 SDK 源码，缺则 skip） |
| `protocol/hooks.py` | `packages/hooks/hook-protocol` + `hooks-claude-code` | 默认 runner 对齐 runner.ts：stdin JSON payload + trailing newline、cwd、CLAUDE_PROJECT_DIR env、缺省 600000ms 超时；保留"异步 + signal"同步近似（subprocess） |
| `seams/sandbox_local.py` | `packages/sandbox/sandbox-local` + `sandbox-windows-acl` | landlock 后端经 `seams/landlock_run.py` ctypes 自限制执行器真执行（CLI 契约对齐 `native/landlock-run/docs/cli-contract.md`：--ro/--rw/--/--probe、exit 125、报告行逐字）；Windows ACL 写限制由 `seams/sandbox_windows_acl/` 物化（见下行） |
| `seams/sandbox_windows_acl/`（12 模块） | `sandbox/windows-acl-restrict-poc`（单文件 index.ts 分区） | ctypes FFI 物化 WRITE_RESTRICTED 机制：win32_abi / ffi（替上游 koffi）/ errors / acl / token / workspace_sid / path_boundary / grant / spawn / index / runner 一一对应上游分区；runner 为 `python -m` CLI（exit 127 失败签名）；非 win32 平台 import 即抛 OSError；门控 e2e 见 tests/test_windows_acl_e2e.py |
| `seams/landlock_run.py` | `native/landlock-run`（C11 launcher） | ctypes 复刻同一 CLI 契约与 Landlock UAPI 语义（ABI 协商 / PATH_BENEATH 规则 / PR_SET_NO_NEW_PRIVS → restrict_self → execvp；full ⟺ 内核 ABI ≥ 5，否则 partial 但仍受限；非 Linux 宿主干净退出 125） |
| `seams/sandbox_policy.py` | `packages/sandbox/sandbox-policy` | ctx.sandboxPolicy：Config {mode 缺省 read-only, workspaceRoot} fail-loud 校验；resolve() = 显式 mode > 会话日志最后一条 `sandbox/mode`（session-mode.ts 的 effectiveSandboxMode fold）> 部署缺省，workspace 根先 canonical 后词法规范化、会话 cwd 即边界；三档策略上下文经 systemPrompt `.context('sandbox:policy', order=110)` 注册，loop 侧投影在变化时把快照注入对话消息流（`core/agent_loop/runtime_context.py`） |
| `shell/bash_local.py` + `bash_sandbox.py` + `helpers.py` | `packages/shell/{shell, bash-local, bash-sandbox}` | ctx.shell 前台 `bash -c` 执行器族：本地直跑 / 经 ctx.sandbox confine 包裹并报告 {mode, denied, enforcement}；三路归因对齐 helpers.ts——runner 启动失败（ENOENT/EACCES 且 argv[0] 证据 + cwd 可用性独立校验）与 runner 失败规则命中抛 SandboxUnavailableError 且优先于 denial，denial = 非零退出 + stderr 大小写不敏感签名；danger-full-access 直通不包裹。后台进程机制未复现（mini 后台面是 jobs registry，§3.5） |
| `seams/credentials_local.py` | `packages/credentials/credentials-local` | 文档为 version-1 JSON 布局 `{version:1, refs, records}`（上游 YAML）：fail-closed 解析 + 可识别 flat 文档启动自动迁移；记录服务侧五件套（read/describe/list/modify/delete_record + `.records` 只读视图，P2-20 §2.10 已闭合：键语法 `[a-z][a-z0-9-]*`、写锁 `DOCUMENT_LOCK_WAIT_SECONDS=30`、modifyRecord 唯一写路径——锁内 reconcile + mutate + 写前准入 = 读路径选择）；authorization 包未跟进；B 档已补读侧热重载——读入口 `_refresh_if_changed()` 先 `os.stat` 比对 mtime/size、变了才整表重解析（外部编辑/删除即时生效），写路径 `_reconcile_from_disk` 折叠不变，原子写无撕裂读侧不需锁 |
| `seams/subprocess_env.py` | `packages/subprocess/subprocess/src/index.ts` + `types.ts` | 环境清洗切片：SENSITIVE_ENV_PATTERN 凭据形启发式 + DSH_ENV_PREFIX 大小写不敏感剔除；显式 env 在 scrub 之后合并（providers spawn 层叠） |
| `seams/subagent/`（`__init__.py` + `descriptor.py` + `providers.py` + `worker.py` + `continuation.py` + `tool.py`） | `packages/subagent/subagent` + `subagent-fork-in-process` + `-acp` + `-dsh-sdk` + `subagent-spawn-in-process` + `subagent-in-process-driver` + `tool-subagent-control` + `tool-subagent-report` | 续跑 A8 为异步事件驱动（双路径：父有 driver → 投递即返回 + watchSettlement 结算 + steer 批内合并；无 driver → 回退同步 pump）。生命周期 scoped dispatch（委托父 scope 载体过滤，无标号退化祖先链）+ provider 注册表（register_provider → 注销发布 subagent/provider-removed）+ DRAINING 拒绝面（drain/drain_descendants/drain_children + assert_admitting 准入边界）+ report 工具逐字契约（output 参数、部署级 reportDelivery 'quiet'\|'next-step'、{messageId} 返回与 render）+ childId 预留 DUPLICATE_CHILD 断言已对齐；invariant 运行时校验架构不适用；同步模式结算投递走非唤醒 next-step |
| `demo.py` | `packages/examples/agent-spine-demo` | 教学入口，保留顶层（`python -m miniharness.demo`） |
| `example_plugins.py` | `examples/` | 教学示例，保留顶层 |

## 3. 依赖方向规则

分层如下（Python 没有编译期模块边界，规则由 `tests/test_dependencies.py` 的 import 方向断言钉死，违反即测试失败）：

| 层 | 内容 | 允许依赖 |
|---|---|---|
| L0 地基 | `core/session`、`core/scope`、`core/dsh_scope`、`core/schema`、`core/hmr` | 无（互不依赖；core.scope ↔ core.dsh_scope / core.schema / core.hmr→core.scope 经 §3 例外豁免） |
| L1 领域 | `llm/*`、`core/tools`、`core/system_prompt`、`core/session_store`、`core/agents`、`attachment`、`boot/*` | 仅 L0 |
| L2 编排 | `core/agent_loop`、`compaction`、`jobs`、`plan`、`commands`、`goal`、`skills` | L0 + L1 |
| L3 应用与入口 | `cli/*`、`protocol/*`、`seams/*`、`preset`、`extensions`、`interaction`、`client`、`web`、`shell` | L0 ~ L2 |
| 教学层 | `demo.py`、`example_plugins.py` | 任意层，但不得被业务模块依赖 |

**三层组织边界**（2026-08-29 产品化定位；代码与结构上清晰分离、解耦）：

| 边界 | 内容 | 耦合面 |
|---|---|---|
| core 核心能力 | `miniharness/core/` 等（L0~L2 纯领域逻辑） | 不感知 web/CLI 传输载体 |
| 后端 web 服务 | `miniharness/web/` + `protocol/` 等（L3 传输层） | 消费 core 能力；对外发布 HTTP/SSE wire 契约（信封/帧/错误语义） |
| 前端工程 | 独立工程形态：**`webui/`**（React+TS+Vite，2026-08-30 落地，产品化）走新 wire；`web/static/` vanilla SPA 降为教学参照（旧 wire，不实跑） | 只依赖后端发布的 wire 契约，禁止 import/hack Python 内部 |

前端唯一的耦合面是 `web/` 层发布的 wire 契约，不是 Python 内部 API——这保证前端可独立选用现代化技术栈（如 React）而无需改造内核。

**`webui/`**（仓库顶层独立工程，React+TS+Vite，2026-08-30 落地）：产品化浏览器前端，只消费 `web/` 发布的 alpha.1 wire 契约（两信封 RPC `/api/<endpoint>` + `/api/remote.mux` WS 帧 + `$events`/`$events/result` + `session.follow`/`control`），零 Python import。三层结构：`src/wire/`（契约客户端层，纯 TS 可单测）、`src/app/`（React 编排 hooks）、`src/ui/`（无状态展示组件）；测试用 vitest（mock fetch/WS）。开发期 Vite dev server 把 `/api` 与 `/api/remote.mux` 代理到本地 Python 后端；生产期 `vite build` 产出 `webui/dist/`，后端 `web/frontend.py` 经 `MINIHARNESS_WEBUI_DIST` 指向该产物即可承载（`serve_static` 契约不变）。覆盖范围对齐教学 SPA 功能面（会话列表/新建、Trajectory（虚拟化窗口 + Overview 折叠跳转 + 全文搜索，2026-09-02 R5）、审批瀑布、队列/作业），不整体移植上游 `packages/client` 40 模块。

规则：

1. L_n 只依赖 L_{&lt;n}，禁止依赖同层或上层。六条显式例外：
   - `seams/subagent/worker.py` 依赖 `protocol/*`（同层）：worker 是 ACP / SDK 线协议的服务端载体，复用协议层的帧与信封实现；
   - `core/hmr.py` 依赖 `core/scope`（同层）：HMR 是 cordis 家族的 vendored 部件（上游 vendor/hmr 直接建在 cordis 之上），复用 Service/fiber 基座，与 core.dsh_scope 同理落 L0；
   - `cli/main.py` 依赖 `web`（同层，单方向）：launcher 组装 web profile——cli 把 ctx/adapter/tools 交给 web 层运行时，web 层不得反向 import cli；
   - `cli/headless.py` 依赖 `seams` 与 `shell`（同层，单方向）：run_headless 组装沙箱栈与 bash 执行器装进 ctx——同上游 bundle/headless 依赖 dsh-sandbox / sandbox-policy / bash-sandbox 的包拓扑；seams/shell 层不得反向 import cli；
   - `shell/bash_sandbox.py` 依赖 `seams/sandbox_local`（同层，单方向）：bash-sandbox 是 ctx.sandbox 的消费者——上游 bash-sandbox 同样依赖 dsh-sandbox，拓扑一致而非分层倒挂；seams 层不得反向 import shell；
   - `cli/main.py` 依赖 `demo`（教学层）：无 profile 时以 `demo` 兜底（教学扩展入口）。
2. `protocol/` 内三个模块互不依赖（acp、sdk、hooks 各自独立）。
3. `seams/` 内 sandbox（sandbox_local + sandbox_policy）、credentials、subagent 互不依赖；policy 与 local 同属沙箱子域——上游 dsh-sandbox-policy 同样依赖 dsh-sandbox。
4. `seams/credentials_local.py` 从 `boot/dotenv.py` 导入 `parse_dotenv`（L3 → L1）：凭据文档解析复用 boot 层的 `.env` 解析器，方向合法。

## 4. 公共 API 面

**白名单（契约层，改它需要对照上游 + 更新差异清单）**：`Session`、`Context`、`RegistryService`、`Tool`、`ToolRegistry`、`AgentLoop`、`StreamChunk`、`LlmAdapter`、`DeepSeekAdapter`、`LlmFailure`、`SessionPersistence`/`JsonlPersistence`/`SqlitePersistence`、`apply_patch`、`boot`、`run_headless`、`create_message` 与四个 block 构造、`derive_messages`、`turn_balance`、`repair_interrupted_turn`、`SESSION_FORMAT_VERSION`、`TOOL_NOT_STARTED`、`TOOL_OUTCOME_UNKNOWN`。
**黑名单（内部工具，不在顶层 `__all__`，只允许深路径 import）**：`deep_freeze`、`thaw`、`is_json_safe`、`now_ms`、`_http_error_code`、`_map_finish_reason`、`load_events_checked`、`repair_and_replay`、`balanced_after_replay`。

**教学扩展（上游无对应，标注于此）**：`cli/default_tools.py`、`cli/session_cmds.py`（会话管理子命令；`--config` 属 `cli/main.py` 启动器标志，同为教学扩展）、`llm/fake.py`、`demo.py`、`example_plugins.py`。

顶层 `__all__` 收敛至 28 项（白名单 + `FakeLlmAdapter`），由 `tests/test_dependencies.py` 断言钉死；白名单每一项都能在 §2 映射表里找到上游对应。

**深路径契约（不在顶层 `__all__`，仅经子包深路径暴露，由 `tests/test_token_meter.py`、`tests/test_compaction.py`、`tests/test_jobs.py`、`tests/test_plan.py`、`tests/test_skills.py`、`tests/test_session_store.py` 钉死行为）**：`TokenMeter`、`install_compaction`、`CompactionEngine`、`compact_surface_region`、`select_compactable_range`、`inspect_compaction_entry_state`、`frame_summary`、`install_jobs`、`register_job_tools`、`LocalJobRegistry`、`JobDoneBox`、`fit_with_suffix`、`fit_completion_notice`、`install_system_prompt`、`SystemPromptService`、`install_plan_mode`、`PlanModeController`、`fold_plan_mode`、`resolve_config`、`install_skills`、`register_skill_tools`、`SkillRegistry`、`FileSystemSkillProvider`、`SkillTool`、`SKILL_GESTURE`、`render_skill_content`、`parse_skill_file`、`digest_catalog_entries`、`install_sessions`、`SessionStore`、`SessionForkError`、`SESSION_NOT_FOUND`、`SESSION_NOT_LIVE`、`SESSION_ALREADY_EXISTS`、`INVALID_BOUNDARY`、`OPEN_TURN`、`LocalAttachmentStore`、`AttachmentStore`、`ImageAttachmentRef`、`SaveImageAttachment`、`ImageAttachmentLimits`、`AttachmentError`、`is_image_admission_error`、`detect_image`、`probe_image`、`supports_acp_image_prompts`、`admit_acp_prompt`、`assistant_block_to_acp`。装配约定：`apply_retry_planner(ctx)` → `install_compaction(ctx)` → `install_jobs(ctx)` → `install_system_prompt(ctx)` →（可选）`install_plan_mode(ctx, config)` →（可选）`install_skills(ctx)` →（可选）`install_sessions(ctx)`（均幂等；`CONTEXT_WINDOW_EXCEEDED` 不在重试白名单，由压缩接管；作业工具注册经 `register_job_tools(reg, ctx.get("jobs"))`，`default_tools` 在 `ctx.jobs` 存在时自动收编；skill 工具注册经 `register_skill_tools(reg, ctx.get("skills"))`，`default_tools` 在 `ctx.skills` 存在时自动收编；plan 依赖 systemPrompt 服务，缺失 fail loud；会话经 `install_sessions(ctx)` 提供 `ctx.sessions`，headless / demo / resume 入口已接入）。