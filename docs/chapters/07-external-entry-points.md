# 07 外部入口：两个产品表面与三个协议入口

> 本章回答一个问题：使用者怎么把 dsh 跑起来？前六章讲的是内核（会话、总线、工具、loop、持久化、扩展口），这一章讲的是外壳——所有能"启动一个 dsh 进程"的路径，以及它们各自把什么约定暴露给外部。
>
> 对应 dsh 真实源码：`apps/cli` + `packages/boot/app-boot` + `packages/bundle/{headless,web-app}` + `packages/{acp,sdk,hooks}`。mini 复现了 headless 一条（`miniharness/headless.py`），其余入口在本章做系统解读并标注复现规划。

## 7.1 总览：一切入口都是 profile

先纠正一个常见误解：dsh 没有"web 模式"和"headless 模式"两个程序。它只有一个启动器 `dsh`（`apps/cli/src/args.ts`），启动器只解析自己的标志，然后把剩余参数原样交给"被启动的 profile"。profile 是 `$DSH_HOME/profiles/<name>` 下的一个目录，里面有一个 `package.json`（声明有序的 bundle 层列表）和一个用户自己的 `cordis.patch.yml`。换句话说，**每种入口 = 一份不同的插件组合树**，入口之间的差异全在组合层，内核一行不改。

启动器自己拥有的东西只有四类（`apps/cli/src/args.ts:48`）：

1. `--profile <name>`：启动哪个 profile（必填）。
2. `web`：`--profile web` 的硬编码别名。
3. `plugin --profile <name> <pnpm args>`：把 pnpm 转发进 profile 目录装插件。
4. `--dump-config` / `--dump-default-config`：打印组合树并退出，不启动。

被启动的应用自己解析自己的标志（`--host`、`--port`、任务文本、`--help`），所以 `dsh --profile web --help` 打印的是 web 应用的帮助，不是启动器的。这个"启动器只做薄壳"的分层是理解一切入口的前提。

出厂自带的 profile 模板只有两个（`packages/boot/app-boot/src/profile.ts:114`）：

| profile | bundle 层 | 形态 |
|---|---|---|
| `web` | `dsh-base` + `dsh-web-app` | 浏览器表面，有 Host/HTTP/浏览器插件 |
| `headless` | `dsh-base` + `dsh-headless` | 一次性任务，无任何 Host 层 |

其它名字的 profile 首次使用不会自动初始化，必须先经 `dsh plugin` 路径显式创建（`initProfile`），否则 fail loud。`dsh-base` 是共享的内核底座（persona、工具模式、Code Mode worker 等），两个表面都叠在它上面。

### mini 复现现状（launcher 层，阶段 8）

`miniharness/cli.py` 复现了启动器的选项语义（对齐 `apps/cli/src/args.ts`，已核实）：

- `--profile headless "task"`：一次性任务（§7.2 全部语义）；未知 profile fail loud。
- `--patch <path>`（可重复）：YAML/JSON overlay 补丁，参与组合层叠与 dump。
- `--dump-config`：只读打印最终组合（boot-free，不启动任何应用）；`--dump-default-config`：只打印内置默认组合。两者互斥（`program.error` 同语义）；dump 不接受任务参数；`--dump-default-config` 不接受 `--patch`/`--config`。输出带行级 `# == <label>` 来源注释、`!!js` 表达式原样未求值、skipped patch warn 不失败、单文档可再加载（对齐 `renderConfigDump`）。
- mini 教学扩展（上游没有，须标注）：`--config <path>` 指定组合文件（上游用 profile 目录机制）；`miniharness sessions` 子命令（会话列表 / 恢复 / 删除 —— 上游会话管理在 web 表层，见 `miniharness/sessions.py`）。
- mini 内置默认组合为空（headless 不走插件树，见 headless.py 简化标注）；组合层与 headless 运行时解耦：带 `--config/--patch` 跑任务时先 boot 验证（fail loud），headless 运行时仍为内置 adapter。

## 7.2 headless：任务文本即命令行

headless 是最容易理解的一个入口，它把"跑一个 agent 回合"压缩成一条 shell 命令：

```sh
dsh --profile headless "run the tests"
```

进程语义（`packages/bundle/headless/README.md` 全文就是这几句话）：

1. 任务文本就是这个应用的命令行：位置参数按空格 join，缺失或纯空白 → usage error，进程退出 1。
2. 启动后创建**一个全新的持久化 Agent**（session id 随机），把任务作为普通用户消息提交。
3. 等它完全停稳（quiescence），先 flush 会话，再汇总本次运行的事件区间。
4. 把**最后一条非空 assistant 文本**写到 stdout（带换行）。
5. 按最终 `turn/end` 的 reason 决定退出码：`completed` → 0，其它（`error`、`blocked`、`max-tokens`……）→ 1；`error` 时 stderr 再写一行 `dsh: <code>: <message>`。
6. 进程不开任何监听端口。

这个入口把前四章的所有约定变成了可观测的进程级契约：你能在 stdout 上直接检验"模型说了什么"，用退出码检验"回合是否正常结束"。`headless` 的"h"不是 headless browser 的意思，是"没有交互、跑完即退"。

### 源码解剖：runner 与 startup

`dsh-headless` bundle 里只有两个插件（`packages/bundle/headless/cordis.patch.yml`）：

- `headless-startup`（`src/startup.ts`）：解析命令行，把任务作为普通 Cordis 服务 `headlessStartup` 发布。空任务在这里被拒绝：`program.error('error: a task is required, ...')`，拒绝时不发布服务，下游 runner 也就不会激活。
- `headless-runner`（`src/index.ts`）：注入 `headlessStartup`，从 lazy config 读任务，然后执行上面那 6 步。

runner 的 `run()` 函数（`src/index.ts:96`）值得逐行看一遍，因为它把前几章的约定串了起来：

```ts
await ctx.get('loader')?.await()          // 等整棵插件树装完，避免半组合状态
const selection = defaultModel.currentSelection()
const { agent } = await agents.create({   // 全新持久化 Agent，随机 session id
  sessionId: SessionId(`session-${randomUUID()}`),
  agentOptions: { provider: selection.provider, model: selection.model },
})
await agent.whenIdle()                    // 首次停稳（没有任何输入）
const firstSeq = agent.session.seq        // 记住区间起点
agent.followup(createUserMessage({ content: [{ type: 'text', text: task }] }))
await agent.whenIdle()                    // 任务回合完成
await sessions.flush(agent.session)       // 先落盘，再汇总
const outcome = summarize(agent.session.events, firstSeq)
io.stdout.write(outcome.text + '\n')
if (outcome.reason?.kind === 'error') {
  io.stderr.write(`dsh: ${outcome.reason.error.code}: ${outcome.reason.error.message}\n`)
}
io.exit(outcome.reason?.kind === 'completed' ? 0 : 1)
```

几个值得注意的细节：

- **`firstSeq` 之后才算数**：summarize 只汇总本次运行产生的事件，会话打开前的历史（恢复场景）不影响输出。这对应第 5 章的恢复语义。
- **summarize 只拼 text 块**（`src/index.ts:61`）：reasoning 块、tool-call 块都被过滤，空文本不覆盖已有结果，所以"最后一条**非空** assistant 文本"是精确语义。
- **错误有两条不同的 stderr 路径**：回合正常结束但 reason 是 error（如模型层带内失败）→ `dsh: code: message`；runner 自身抛异常 → `run().catch(fail)` 只写 `dsh: <message>`（不写 code）。两条都退出 1。
- **`ctx.appExit` 由启动器持有**（`src/index.ts:144`）：runner 不在启动器外运行，没有 appExit 就在激活时报错。进程退出请求是宿主注入的能力，runner 自己不开 exit 后门。

### mini 复现对照

mini 的 `miniharness/headless.py` 复现了上面全部语义，载体差异有两处（诚实标注）：

1. 上游经 Cordis 服务（`agents` / `sessions` / `agentDefaultModel`）创建 Agent；mini 直接构造 `Session + AgentLoop`（stdlib 同步简化，契约不变）。
2. 上游错误经 `finish {kind:'error'}` 带内失败或异常两条路；mini 的 `LlmFailure` 一律以异常抛出（`llm.py` 已声明该简化），所以 `dsh: code: message` 分支目前不可达，保留是为了对齐上游格式。

运行方式：

```sh
python -m miniharness.headless "run the tests"          # 直接运行
python -m miniharness.cli --profile headless "task"     # 走启动器（对齐 dsh CLI）
miniharness --profile headless "run the tests"          # 安装后（pyproject scripts）
```

CLI 解析与上游一致：`--profile headless` 之后的位置参数 join 空格、空任务 usage error 退出 1、未知 profile fail loud（mini 未复现 web，`--profile web` 明确报错而不是静默）。

## 7.3 web：同一个 base 上的浏览器表面

`dsh-web-app` bundle（`packages/bundle/web-app/README.md`）在 `dsh-base` 之上叠了 Web Host 层：webserver、API 网关、workspace、投影缓存、存储，以及浏览器插件名录。它自带的 `web-startup` 提供方解析 `--host` / `--port` / `--trusted-host` 和应用自己的 `--help`，**参数解析完成前不会绑定任何端口**——所以 `dsh --profile web --help` 只是打印帮助。

web 与 headless 是同一 base 的"同级表面"（README 原文 sibling surface）：内核、工具、会话全部共享，只有 Host 层不同。这个"表面 = 组合层差异"的视角正是第 5 章 boot/patch 机制的用武之地。

mini 未复现 web（前端工程量与教学目标不匹配），观察清单中列为后续选项。

## 7.4 三个协议入口

除了两个产品表面，dsh 还有三个"对机器说话"的入口。它们不经过 profile 模板，是独立的协议服务器或桥接层。

### ACP：自动化专用协议

`packages/acp/acp/` 是 Agent Client Protocol 服务器（agentclientprotocol.com），跑在 **stdio JSON-RPC** 上，语义是"自动化专用"：程序化客户端创建全新 agent、发送文本提示、收集已提交的 assistant 文本、解析一次性权限请求、取消工作。它的读取模型是 ACP 的（`session/request_permission` 提供 allow/reject 二选一，客户端可以自动回答），所以它服务的是**另一个程序**，不是人。

仓库内最主要的客户端是 `subagent-acp`（`packages/subagent/subagent-acp`）：当主 agent 派生子 agent 时，如果 provider 选的是 ACP，子 agent 就通过这个协议跑在独立的 harness 进程里。这解释了第 6 章那六个子 agent provider 里 ACP 的位置——协议入口同时是子 agent 出口。

### JSON-RPC SDK：官方 SDK 的线

`packages/sdk/` 定义 JSON-RPC 协议与服务端；`python/sdk`（`deepseek-harness-sdk`）是 stdio JSON-RPC **客户端**，`python/sdk-runtime`（`deepseek-harness-runtime-bin`）是打包了默认组合的运行时二进制。`packages/examples/jsonrpc-demo` 演示同一个协议如何跑在部署方自己的插件树上（`cordis=` 指向自己的 cordis.yml 即可换组合，但要保留 jsonrpc-server 条目）。

它和 ACP 的区别：ACP 是单向自动化契约，SDK 是通用的消息信封协议（rpcId 签发、信封解包、SSE 帧解码等），上层可以再搭任何语义。上一节说的"headless 不开端口"，SDK 恰恰相反——它把 harness 暴露成一条可以编程驱动的线。

### hooks：把你已有的 Claude Code / Codex 钩子带进来

`packages/hooks/` 是两条桥：`hooks-claude-code` 和 `hooks-codex`。它们读取用户**既有**的 Claude Code 式 hook 配置（`hooks.json` 或 settings 的 `hooks` 键），把 `PreToolUse`、`UserPromptSubmit` 这类事件翻译成 harness 的类型化 Decision。公共的匹配器、退出码编解码、`ctx.shell` 执行、最严格合并都放在 `hook-protocol`，两条桥各只实现自己方言的 stdin 载荷与事件映射。

hooks 的价值在于迁移成本：已经写好 Claude Code 钩子（安全策略、工作流检查）的用户，把这些钩子原样带进 dsh，而不是在 harness 里重写一遍。注意它只能挂在 harness 的**既有拦截点**上，不是新的独立入口。

## 7.6 复现：JSON-RPC 信封最小子集（`miniharness/sdk_protocol.py`）

> 对应 dsh 真实源码：`packages/sdk/protocol`（`transport.ts` + `types.ts`）。信封层全对齐，三个方法（initialize / session/prompt / shutdown）接在内存假模型上，"可编程驱动 harness"成立。

### 7.6.1 线协议（`JsonRpcLineTransport`）

newline-delimited JSON-RPC 2.0，每行一个紧凑 JSON 帧。帧分类与上游 `transport.ts` 逐条一致：

1. `id` + `method` → **请求**：无 handler 答 `-32601`；handler 抛错答 `-32603`（带 message）；
2. 仅 `id` → **响应**：`error` 对象 → 以 `JsonRpcResponseError` 拒绝 pending（保留 wire `code` 与 `data`）；否则 resolve `result`；
3. 仅 `method` → **通知**：无 handler 直接丢弃，params 可省略（省略时不带 `params` 成员）。

细节：畸形 JSON 行忽略；`request` 的 id 为 `req_` + uuid（无连字符）；params 归一化（数组/标量折叠为 `{}`）；`close()` 拒绝全部 pending 而不销毁流；`flush()` 以空行 barrier 表达语义。

mini 的同步近似：上游是字节流 + async，mini 是"行馈送 + 内存输出"——`request()` 返回 `PendingRequest`，feed 到对应响应帧时 settle。帧分类、错误码、id 配对语义完整保留。

### 7.6.2 最小运行服务（`SdkRuntime`）

对齐 `types.ts` 的三个请求方法：

| 方法 | 语义（上游） | mini |
|---|---|---|
| `initialize` | cwd/provider/model + 可选 maxTokens → `serverInfo` | 记录参数，返回 `{name: 'deepseek-harness-sdk-runtime', version: '0.0.1'}`（name 是 wire 稳定标识） |
| `session/prompt` | 未知 sessionId **懒创建** agent+session；返回 durable enqueue 回执 `messageId` | 懒创建 `AgentLoop`，跑一个回合，返回 `msg-N` |
| `shutdown` | → `{}` | `{}` |

`messageId` 只标识入队的 user 消息，不标识任何后续 assistant 消息或回合结束（上游 README 明确）。通知（`session.event` / `session.status` / `subagent.*`）在 mini 中由 loop 事件钩子承载，属简化标注。

### 7.6.3 硬性规定（被 21 个测试钉住）

1. 帧分类三态判定与上游一致；畸形行忽略不产生输出。
2. 无 handler → `-32601`；handler 抛错 → `-32603` 且 message 原样；错误响应带 `JsonRpcResponseError(code, data)`。
3. 响应帧只 settle 匹配 id 的 pending；未知 id 忽略；close 后拒绝写入。
4. `serverInfo.name` 恒为 `deepseek-harness-sdk-runtime`。
5. `session/prompt` 未知 sessionId 懒创建会话，`messageId` 递增签发。

验证：`python -m unittest tests.test_sdk_protocol -v`（21 个用例，含端到端 stdio 行仿真）。
## 7.7 复现：ACP 最小子集（`miniharness/acp.py`）

> 对应 dsh 真实源码：`packages/acp/acp`（`apply()` + `codec.ts`）。自动化专用契约全对齐，跑在假模型上。

### 7.7.1 握手与会话

- `initialize` → `agentInfo.name == 'deepseek-harness-acp'`、`promptCapabilities` 全 `false`（不宣称富媒体）、`authMethods: []`——本桥只承诺 text / resource_link；
- `new_session`：cwd 必须绝对路径；`additionalDirectories` 非空拒绝；`mcpServers` 非空拒绝；mint sessionId；
- `cancel`：未知 session **no-op**，已知 session 取消 agent。

### 7.7.2 prompt 的结算语义

同步模型下 `prompt()` 直接跑完整回合再返回 stopReason，等价于上游"等到 whole-agent idle"：

1. session 必须存在（unknown → invalid params）；已有 inflight → 拒绝（"a prompt is already in flight for this session"）；
2. 只支持 `text` 与 `resource_link`（resource_link 渲染为显式文本引用，不静默丢弃）；空 prompt 拒绝；
3. turn/end reason → stopReason 映射（`codec.ts` 同构）：`completed→end_turn`、`max-tokens→max_tokens`、`aborted→end_turn`（`cancelled` 保留给显式 client 取消）、`interrupted→cancelled`、`blocked/error→end_turn`；
4. turn/end kind='error' → 以 "turn failed: …" 立即拒绝（模型失败直接表现为 prompt 错误）。

### 7.7.3 审批桥

`approval/request` 监听器：仅当 `callId` 存在时提供**二选一**（`allow-once` → `allowed-once`、`reject-once` → `rejected`、`cancelled` → `cancelled`）；callId 缺失 → 委派 next()（不处理）。一次决策、绝不从未知客户端响应推断持久授权。

### 7.7.4 硬性规定（被 26 个测试钉住）

1. 握手不宣称富媒体能力（image/audio/embeddedContext 全 false）。
2. cwd 非绝对路径、additionalDirectories 非空、mcpServers 非空 → invalid params（-32602）。
3. prompt：未知 session / inflight / 空 prompt / 非 text·resource_link 内容 → 拒绝；回合真实跑完（turn/start + turn/end 落日志）。
4. stopReason 映射表逐项与上游一致。
5. 审批桥：callId 缺失委派；allow-once/reject-once/cancelled 三态映射；默认 answerer 允许（测试注入可换）。
6. close 后一切请求 → internal error（-32603，"disposed"）。

验证：`python -m unittest tests.test_acp -v`（26 个用例）。

### 7.7.5 简化标注

- 上游 async（whenIdle 等待、stream 通知、`agent_message_chunk` 增量）；mini 同步跑完整个回合，会话更新流以 `updates` 记录承载；
- 上游经 cordis 插件挂载（`inject: ['agents']`）+ ACP SDK 的 stdio 连接；mini 直接操作服务对象；
- inflight 拒绝在同步模型下只能手动置标志触发（真并发不存在）。

## 7.8 复现：hooks 桥（`miniharness/hooks.py`）

> 对应 dsh 真实源码：`packages/hooks/hook-protocol`（`codec.ts` + `matcher.ts` + `merge.ts` + `types.ts`）与 `hooks-claude-code/src/config.ts`。mini 复现 claude-code 一条桥，把既有 CC 钩子配置翻译成 harness 的四类拦截决策；审计以 `hook/invoked` + `hook/result` 配对入日志（log-only 非 surface）。

### 7.8.1 配置解析（`parse_claude_code_config`）

CC 钩子配置有两种形态：settings 对象里的 `hooks` 键（`{"settings": {"hooks": {...}}}`），或裸事件映射。解析结果分两路：`config`（可用的 command 钩子，事件 → 钩子组）与 `skipped`（非 command 类型的钩子，如 `http`，如实记录但跳过）。

```python
parsed = parse_claude_code_config(raw)          # raw: 字典
parsed["config"]["PreToolUse"][0]["hooks"]      # command 钩子列表
parsed["skipped"]                               # [{"event": ..., "type": ...}, ...]
```

逐条对齐上游 `config.ts` 的约定：

- 事件名限定在 CLAUDE_EVENTS 七事件（UserPromptSubmit / PreToolUse / PostToolUse / PreCompact / PostCompact / Notification / Stop），事件名不存在视为整段无效；
- 钩子条目必须带 `type: "command"`，否则进 `skipped`；
- `UserPromptSubmit` 与 `Stop` 的 matcher 无意义（前者必然匹配、后者是声明式配置），解析时直接丢弃；
- matcher 在**解析期**就用与运行时同一套校验：无效正则直接抛 `SyntaxError` 拒掉整份配置（fail-closed，和上游 `parseMatcher` 抛错一致）；
- command 里的 `${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_PROJECT_DIR}` 变量在解析期替换（`substitute_command`），未提供的变量原样保留。

### 7.8.2 匹配器（`matches_matcher` / `matcher_diagnostic`）

```python
matches_matcher("Bash|Read", "Read", "claude-code")   # True：字面量管道交替
matches_matcher(r"Bash.*", "BashExec", "claude-code") # True：regex（非锚定）
matches_matcher(None, "Bash", "codex")                # True：match-all 哨兵
```

- match-all 哨兵：`None` / `""` / `"*"` 恒匹配（上游 `isMatchAll`）；
- claude-code 方言：`^[A-Za-z0-9_|]+$` 命中的是**字面量管道交替**（逐段精确比较，非正则）；否则当未锚定正则；
- codex 方言：一律当正则（`RegExp(test)`），无效正则不匹配而不是抛错（运行时容错，`matcher_diagnostic` 负责报出）。

### 7.8.3 退出码编解码（`parse_hook_output`）

上游 `codec.ts` 的约定，mini 逐条复刻：

- `exitCode == 0` 且 stdout 以 `{` 开头：尝试解析 JSON，失败则把 stdout 原样当文本（干净退出的解析错误是宽容的，不阻断）；
- `exitCode == 2`（`BLOCKING_EXIT_CODE`）：**block**，reason 取 stderr（空则 `"blocked"`）；
- 其他非零码：**error**，reason 取 stderr（空则 `"error"`）；
- JSON 顶层只有 `approve` / `block` 两种决策有效，越界值忽略；
- 事件域（`hookSpecificOutput`）里的 `permissionDecision: allow|deny|ask` **覆盖**顶层决策（这是 PreToolUse 钩子表达 `ask` 的唯一途径）；`hookEventName` 与期望事件不符时，整个事件域丢弃（保留判别符，丢决策字段）；
- `updatedInput` 解析但不执行——拦截决策由调用方决定是否采纳。

### 7.8.4 合并（`merge_hook_outputs`）

最严格合并（上游 `merge.ts`），决策等级 `deny > ask > allow`：

- 任一 `block`/`deny` → `deny`（reason 用 `\n\n` 连接全部获胜等级条目）；无 deny 时任一 `ask` → `ask`；否则全部 `allow` → `allow`；
- `continue: false` 只影响 `stop`/`stopReason` 字段，**不**参与决策等级；第一个 `continue: false` 粘住（sticky），其 `stopReason` 胜出；
- `additionalContext` 按序累积成列表，`systemMessage` 进 `systemMessages`；空列表合并结果 `decision: "none"`。

### 7.8.5 桥（`ClaudeCodeBridge`）

四个拦截点把合并结果翻译成 harness 决策（与 7.4 的 hooks 解读一致）：

| 拦截点 | 输入 | 阻断 | 委派（放行） |
|---|---|---|---|
| `pre_step` | 用户提示文本 | `{"kind": "reject"}`（deny 时） | `None` |
| `pre_tool` | 工具名 | `{"kind": "deny"|"ask", "reason"}`（deny/ask 时） | `None`（allow） |
| `post_tool` | 工具结果 | `{"kind": "block", "feedback"}`（deny 时，feedback 为 reason） | `None` |
| `stop` | — | `{"continue": True, "reason"}`（deny 时强制继续，reason 缺省 `"blocked by Stop hook"`） | `None` |

执行经 `run_fn`（可注入，默认 `run_hook`：`subprocess` shell 执行 + 超时；超时 `exitCode` 为 `None` 并给出 stderr 说明）。每次钩子执行都落一对审计事件 `hook/invoked` → `hook/result`（同一 `handlerId` 配对，含 point/dialect/turn 上下文；事件在 turn 内包围，log-only 不带 surfaceOp），对应上游 `hooksRuntime` 的 `audit` 集成。

验证：`python -m unittest tests.test_hooks -v`（40 个用例，含真实子进程集成）。

### 7.8.6 简化标注

- 只复现 claude-code 方言（codex 桥的 stdin 载荷与 `MessageContext` 解码未做，matcher 方言已支持）；
- 上游经 cordis 插件注入 + `ctx.shell` 执行；mini 用 `subprocess` + 注入点；
- 上游 PreCompact/PostCompact/Notification 在 harness 循环里挂接；mini 只落桥未挂循环（循环侧扩展口见第 6 章）。

## 7.9 一张表看懂全部入口（复现状态更新）

| 入口 | 对谁说话 | 载体 | mini 状态 |
|---|---|---|---|
| `dsh --profile web` | 人（浏览器） | HTTP + 浏览器客户端 | 未复现（观察清单） |
| `dsh --profile headless "task"` | 人（shell 一次性任务） | 进程（stdout/退出码） | ✅ `miniharness/headless.py` |
| `dsh --profile <自定义>` | 组合层自定义 | 任意 | 未复现（可经 boot/patch 扩展） |
| ACP 服务器 | 自动化程序 | stdio JSON-RPC | ✅ `miniharness/acp.py`（握手/会话/prompt/取消/审批桥，26 测试） |
| JSON-RPC SDK | 编程客户端（官方 Python SDK 的线） | stdio JSON-RPC | ✅ `miniharness/sdk_protocol.py`（信封子集 + 三方法，21 测试） |
| hooks 桥 | 用户既有 CC/Codex 钩子 | 子进程 | ✅ `miniharness/hooks.py`（CC 配置 → 四类拦截决策 + 审计配对，40 测试） |

复现顺序建议：headless（已完成）→ JSON-RPC 信封最小子集（复用 `miniharness.cli` 的进程壳，价值最高）→ ACP 最小子集 → hooks 桥（已完成）。真正的取舍在第 4 行：自定义 profile 是"组合层的事"，它不需要新的协议，只需要 `boot()` 已经支持的 patch 层叠——第 5 章的 `apply_patch` 就是干这个的。
