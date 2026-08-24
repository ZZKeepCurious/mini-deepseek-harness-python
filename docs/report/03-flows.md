# 03 · 关键处理流程

<p class="lead">五条核心时序：Turn/Step 循环、工具执行管线、会话持久化、LLM 流式适配、启动与组合。每条给出事件序列与源码路径。</p>

<div class="pill-row">
  <span class="tag t-blue">everything is a plugin</span>
  <span class="tag t-blue">event-sourcing</span>
  <span class="tag t-blue">capability seams</span>
  <span class="tag t-green">TypeScript strict</span>
  <span class="tag t-green">Cordis vendored</span>
  <span class="tag t-amber">Python SDK 存在</span>
</div>

## 5. 关键处理流程

### 5.1 Turn / Step 循环

**step** = 一次模型请求 + 它调用的工具；**turn** = 零或多个 step，打开于首次输入被认领前，关闭于再无未偿之责。输入经唯一 inbox 送达，部分消息立即唤醒 driver，注入上下文在 inbox 等待。

<p class="fig-cap">图 11：Turn / Step 完整时序（含 reject 与 turn-stopping 分支）</p>

```mermaid
sequenceDiagram
  participant U as 用户 / UI
  participant A as Agent
  participant D as agent-loop Driver
  participant S as Session 日志
  participant LLM as ctx.llm 扩展口
  participant T as ctx.tools 注册表
  U->>A: followup(content)
  A->>D: 排队输入唤醒 driver
  A->>S: turn/start [durable]
  D->>D: claim 一个排队消息 + next-step 输入
  D->>D: agent/pre-step (waterfall)
  alt 被 reject 或空 enter
    D->>S: turn/end（零 step 关闭）[durable]
  else 进入 step
    D->>S: step/start [durable]
    D->>S: user/message [durable]
    D->>D: deriveMessages() 派生模型历史
    D->>LLM: agent/request (waterfall) → llm/stream
    LLM-->>D: StreamChunk 流（assistant/chunk*）
    D->>S: assistant/message [durable]
    D->>T: tool/call → 工具执行管线 [durable]
    T-->>D: tool/result [durable]
    D->>S: step/end [durable]
    alt 还有工具请求或 next-step 输入
      D->>D: 再次 claim → 下一步
    else 无未偿之责
      D->>D: agent/turn-stopping (serial)
    end
  end
  D->>S: turn/end [durable]
  A->>A: status: idle
```

<p class="mermaid-note">完整时序见 docs/agent-lifecycle.md（同款 Mermaid sequenceDiagram）；`turn/start` 在认领输入前就打开，因此"被拒绝的尝试"也会留下持久化记录。</p>

!!! example "示例走查（一次被拒绝的 turn）"
    用户 `followup("删除 /etc")` → driver 认领输入 → `turn/start` 先落盘 → `agent/pre-step` 上的守卫插件不调用 `next()`，直接返回 `reject` → driver 不产生任何 step，直接 `turn/end`（零 step 关闭）。日志里留下了 `turn/start + turn/end` 两条记录——"被拒绝的尝试"也是事实，必须可审计。

### 5.2 工具执行管线（可扩展 waterfall + 单调守卫）

<p class="fig-cap">图 12：工具执行管线全流程（策略 → 守卫 → 执行 → 后处理 → 权威结果）</p>

```mermaid
flowchart TD
  M["assistant 消息含 tool-call block"]
  TC["Session: tool/call&lt;br/&gt;执行前先记录 [durable]"]
  PC["UI 挂起卡片 presentCall(args)"]
  PRE["tools/pre-execute waterfall&lt;br/&gt;hooks / 权限 / 沙箱"]
  ASK["ask → ctx.approval 一次性询问&lt;br/&gt;absent / unanswerable → deny"]
  DEN["denied&lt;br/&gt;工具体被跳过"]
  G["注册的单调守卫&lt;br/&gt;只能减权 · 乱序无法撤销"]
  EX["tools/execute waterfall&lt;br/&gt;超时 / 重试 / 度量（around-dispatch）"]
  BODY["工具 execute() 体&lt;br/&gt;自有事件：todo/write · fs/observed · tool/code-dispatch"]
  POST["tools/post-execute waterfall&lt;br/&gt;accept / replace / block(+feedback)"]
  NORM["注册表外层规范化&lt;br/&gt;snapshot 异常 → isError"]
  FIN["finalizeContent&lt;br/&gt;最后一个内容只读硬性规定"]
  RES["tools/result 同步通知&lt;br/&gt;冻结的权威结果"]
  TR["Session: tool/result&lt;br/&gt;唯一模型面向结果 [durable]"]
  PR["UI 完成卡片 presentResult"]
  M --> TC
  TC --> PC
  TC --> PRE
  PRE -->|allow| G
  PRE -->|deny| DEN
  PRE -->|ask| ASK
  ASK -->|allowed-once| G
  ASK -->|拒绝 / 取消| DEN
  G -->|allow| EX
  G -->|deny| DEN
  EX --> BODY
  BODY --> POST
  POST --> NORM
  NORM --> FIN
  FIN --> RES
  RES --> TR
  TR --> PR
```

!!! example "示例走查（一次 bash 调用）"
    模型流式返回 `tool-call(bash)` → 先落 `tool/call` 事件，UI 挂起卡片 → `pre-execute` 权限插件返回 allow → 单调守卫（30s 超时 wrapper 挂在 `execute` 上）→ 工具体执行 → `post-execute` 接受结果 → 注册表规范化（异常统一 `isError`）→ `tools/result` 通知冻结结果 → 落 `tool/result`，UI 渲染完成卡片。全程参数只物化一次并深度冻结，任何一步抛错都不会让回合中断。

- 参数在策略前一次性**无损 JSON 物化并冻结**；结果 `value` 是执行局部的，持久层只存 `content`/`error`/`meta`。
- 工具可声明 `isConcurrencySafe` 加入并行组；否则 `exclusive` 形成串行屏障；`timeoutMs` 由 `tools/execute` wrapper 强制，绝不发给模型。
- 内置 JSON Schema DSL（16 层容器精确推断后回退 `JsonValue`）与受强制子集的 raw schema 校验器（`assertSupportedJsonSchema`）。

### 5.3 会话持久化（durability seam）

常规做法是"每次变化立刻写库"；dsh 把持久化做成订阅者，异步成批写入。四个要点：

- **扩展口**：`ctx.sessionPersistence` 抽象（locate / create / append / 逻辑 load/inspect / 物理后缀读）+ 两个可互换后端：**JSONL**（每会话一个文件，支持 packed chunk 行）与 **SQLite**（多会话一库，单调 `SCHEMA_VERSION`）。
- **flush 检查点**：`session/event` 是同步通知，持久化插件先复制事件再异步成批写入；`session/flush` 是等待的并行栅栏，用于认领下一个普通 turn 前的排序与错误观察点。
- **格式拒绝，不迁移**：版本落后 = "升级 harness"；版本超前 = "使用更新的 harness 打开"。未知事件类型若未带 `ignorable: true` 则整体拒绝加载（防止静默丢事件改变后续解读）。
- **崩溃恢复**：关闭孤儿 turn（合成 `interrupted`），只作用于冷会话；活会话 `load` 等待权威内存快照持久化。

<p class="fig-cap">图 13：会话持久化——双后端、flush 栅栏与崩溃恢复</p>

```mermaid
flowchart LR
  S["Session 内存日志"]
  EVT["session/event 同步广播"]
  P["持久化插件：先复制事件"]
  Q["异步成批写入队列"]
  J["JSONL 后端&lt;br/&gt;每会话一个文件 · packed chunk 行"]
  QL["SQLite 后端&lt;br/&gt;多会话一库 · 单调 SCHEMA_VERSION"]
  F["session/flush 并行栅栏&lt;br/&gt;下一 turn 前等待 + 错误观察点"]
  NEXT["认领下一个普通 turn"]
  LOAD["load()：未知事件类型 fail-closed&lt;br/&gt;版本落后 / 超前 = 拒绝，不迁移"]
  INT["崩溃恢复&lt;br/&gt;合成 turn/end interrupted&lt;br/&gt;保持括号平衡"]
  S --> EVT --> P --> Q
  Q --> J
  Q --> QL
  J --> F
  QL --> F
  F --> NEXT
  LOAD -.冷会话重载.-> INT
```

!!! example "示例走查（进程崩溃）"
    `turn/start` 已落库但 `turn/end` 未及写入时进程被杀 → 重启后 JSONL/SQLite 后端的 `load()` 发现括号不平衡 → 不截断日志，而是追加合成 `turn/end { reason: "interrupted" }` → 会话回到可继续状态。若日志里混入未知事件类型（未带 `ignorable: true`），则整体拒绝加载——宁可不打开，也不能静默丢事件改变后续解读。

### 5.4 LLM 流式适配扩展口

常规做法是"官方 SDK 直接调用，错误各自处理"；dsh 把模型厂商差异收敛到统一流协议里。四个要点：

- **统一流协议 `StreamChunk`**：`block-start / text-delta / reasoning-delta / tool-call-delta / block-end / usage / finish`。块索引关联交错增量；`block-end` 携带完整块；`usage` 必须在 `finish` 前、之后不再有值。
- **DeepSeek 官方适配器**（`dsh-llm-deepseek`）：直接 `fetch` + SSE（eventsource-parser）翻译官方 wire 格式。支持 thinking / reasoningEffort / contextWindow / maxTokens 输出上限 / 重试策略（normal|always + 退避）。
- **动态配置**：baseURL、目录、请求默认值经 thunk **每次操作**重读；`ctx.settings` 支持无重启覆盖；`ctx.credentials` 让 API key **每次调用**解析（配置只存 `apiKeyEnv` 引用，绝无明文）。
- **两种授权错误路径统一为 `LlmFailure`**；上下文溢出统一编码 `CONTEXT_WINDOW_EXCEEDED`；空响应视为可重试错误 `EMPTY_RESPONSE`；每次请求携带 app attribution 头。

<p class="fig-cap">图 14：LLM 流式适配——统一 StreamChunk 协议与官方适配器</p>

```mermaid
sequenceDiagram
  participant D as Driver
  participant AD as llm-deepseek 适配器
  participant API as DeepSeek API（SSE）
  participant S as Session 日志
  D->>AD: 构造请求（baseURL / 目录 / 默认值每次操作重读）
  AD->>API: fetch + SSE（eventsource-parser）
  loop 流式
    API-->>AD: data: text / reasoning / tool-call delta
    AD-->>D: StreamChunk：block-start / text-delta / reasoning-delta / tool-call-delta / block-end
    D->>S: assistant/chunk [durable]
  end
  API-->>AD: usage（必须在 finish 之前）
  API-->>AD: finish（之后不再有值）
  AD-->>D: 收尾并落 assistant/message
  D->>S: assistant/message [durable]
```

!!! example "示例走查（一次带思考的流式回合）"
    driver 每次请求都经 thunk 重读 `ctx.settings` 与 `ctx.credentials`（配置只存 `apiKeyEnv` 引用，绝无明文）→ 适配器 `fetch` 官方 SSE 端点 → 逐块翻译成统一 `StreamChunk`：`block-start` → `reasoning-delta`* → `text-delta`* → `tool-call-delta` → `block-end`（携带完整块）→ `usage` → `finish`。每次 delta 同步落 `assistant/chunk`，最后合并成一条 `assistant/message`。键过期与余额不足两条错误路径统一收口为 `LlmFailure`，上下文溢出编码 `CONTEXT_WINDOW_EXCEEDED`。

### 5.5 启动与组合（profile / bundle / patch 层）

<p class="fig-cap">图 15：boot() 启动流程与组合层叠顺序</p>

```mermaid
flowchart TD
  BOOT["boot()"]
  ROOT["创建 root context&lt;br/&gt;暴露 dshHomePath 给 !!js 表达式"]
  LDR["安装 Loader&lt;br/&gt;mountRootInclude：cordis:include + cordis:group 内建"]
  PREP["prepare hook（可选，宿主准备）"]
  MNT["挂载 include 树（并发挂载条目）"]
  CHK{"断言条目已加载 + 已激活"}
  OK["返回 root context"]
  FAIL["dispose 部分 context&lt;br/&gt;标签化错误 + exit(1)"]
  BOOT --> ROOT --> LDR --> PREP --> MNT --> CHK
  CHK -->|成功| OK
  CHK -->|失败| FAIL
  subgraph LAYERS["组合顺序（层叠到空条目列表）"]
    L1["各 bundle 层（按 profile 列表顺序）"]
    L2["profile 级 cordis.patch.yml"]
    L3["home 级 cordis.patch.yml（压过 profile 级）"]
    L4["任何 --patch overlay"]
  end
  L1 --> L2 --> L3 --> L4
  L4 --> SEM["补丁语义&lt;br/&gt;按 id 定位整段替换 / insert 插入 / !!js 挂载时插值"]
```

**关键设计**：组合、配置导出（`--dump-config`）、标志派发共用同一个补丁算法（include 的 `applyEntryPatches` 导出为纯函数），因此三者永不漂移。