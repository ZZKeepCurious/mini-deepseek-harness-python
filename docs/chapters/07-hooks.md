[← 第 7 章导读](07-external-entry-points.md)

入口组合：[Fig S5](../system-map.md#fig-s5)。

# 第 7 章 · §7.8 hooks 桥

## 7.8 hooks 桥（`miniharness/protocol/hooks.py`） {#sec-7-8}

> 对应 dsh 真实源码：`packages/hooks/hook-protocol`（`codec.ts` + `matcher.ts` + `merge.ts` + `types.ts`）与 `hooks-claude-code/src/config.ts`。mini 复现 claude-code 一条桥，把既有 CC 钩子配置翻译成 harness 的四类拦截决策；审计以 `hook/invoked` + `hook/result` 配对入日志（log-only 非 surface）。

### 7.8.1 配置解析（`parse_claude_code_config`）

CC 钩子配置有两种形态：settings 对象里的 `hooks` 键（`{"settings": {"hooks": {...}}}`），或裸事件映射。解析结果分两路：`config`（可用的 command 钩子，事件 → 钩子组）与 `skipped`（非 command 类型的钩子，如 `http`，如实记录但跳过）。

```python
parsed = parse_claude_code_config(raw) # raw: 字典
parsed["config"]["PreToolUse"][0]["hooks"] # command 钩子列表
parsed["skipped"] # [{"event": ..., "type": ...}, ...]
```

逐条同上游 `config.ts` 的约定：

- 事件名限定在 CLAUDE_EVENTS 七事件（SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / Stop / SubagentStart / SubagentStop，上游 `hooks-claude-code/src/config.ts`），事件名不存在视为整段无效；
- 钩子条目的 `type` 非字符串时**缺省按 `"command"` 处理**（不进 skipped，hooks.py）；字符串且非 `"command"` 才进 `skipped`；
- `UserPromptSubmit` 与 `Stop` 的 matcher 无意义（前者必然匹配、后者是声明式配置），解析时直接丢弃；
- matcher 在**解析期**就用与运行时同一套校验：无效正则直接抛 `SyntaxError` 拒掉整份配置（fail-closed，和上游 `parseMatcher` 抛错一致）；
- command 里的 `${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_PROJECT_DIR}` 变量在解析期替换（`substitute_command`），未提供的变量原样保留。

### 7.8.2 匹配器（`matches_matcher` / `matcher_diagnostic`）

```python
matches_matcher("Bash|Read", "Read", "claude-code") # True：字面量管道交替
matches_matcher(r"Bash.*", "BashExec", "claude-code") # True：regex（非锚定）
matches_matcher(None, "Bash", "codex") # True：match-all 哨兵
```

- match-all 哨兵：`None` / `""` / `"*"` 恒匹配（上游 `isMatchAll`）；
- claude-code 方言：`^[A-Za-z0-9_|]+$` 命中的是**字面量管道交替**（逐段精确比较，非正则）；否则当未锚定正则；
- codex 方言：一律当正则（`RegExp(test)`），无效正则不匹配而不是抛错（运行时容错，`matcher_diagnostic` 负责报出）。

### 7.8.3 退出码编解码（`parse_hook_output`）

上游 `codec.ts` 的约定，mini 逐条复刻：

- `exitCode == 0` 且 stdout 以 `{` 开头：尝试解析 JSON，失败则把 stdout 原样当文本（干净退出的解析错误是宽容的，不阻断）；
- `exitCode == 2`（`BLOCKING_EXIT_CODE`）：**block**；reason 取 stderr（**stderr 为空则不设 reason 字段**，上游 `codec.ts` 只在非空时设置）；
- 其他非零码：**不产生 decision**（落 pass），output 仅携带 `exitCode/stderr/stdout`（上游 `codec.ts`）；"spawn 失败"的 `exitCode == None` 同样无 decision；
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
| `pre_tool` | 工具名 | `{"kind": "deny", "reason"}` 或 `{"kind": "ask"}`（ask 仅当合并结果带 reason 时才带 `reason` 字段，hooks.py） | `None`（allow） |
| `post_tool` | 工具结果 | `{"kind": "block", "feedback"}`（deny 时，feedback 为 reason） | `None` |
| `stop` | — | `{"continue": True, "reason"}`（deny 时强制继续，reason 缺省 `"continue: blocked by Stop hook"`，hooks.py；reason 只用合并结果，不走 stopReason） | `None` |

执行经 `run_fn`（可注入，默认 `run_hook`：`subprocess` shell 执行 + 超时；超时 `exitCode` 为 `None` 并给出 stderr 说明）。每次钩子执行都落一对审计事件 `hook/invoked` → `hook/result`（同一 `handlerId` 配对，含 point/dialect/turn 上下文；事件在 turn 内包围，log-only 不带 surfaceOp），对应上游 `hooksRuntime` 的 `audit` 集成。

验证：`python -m unittest tests.test_hooks -v`（含真实子进程集成）。

### 7.8.6 简化标注

- 只复现 claude-code 方言（codex 桥的 stdin 载荷与 `MessageContext` 解码未做，matcher 方言已支持）；
- 上游经 cordis 插件注入 + `ctx.shell` 执行；mini 用 `subprocess` + 注入点；
- 上游 CLAUDE_EVENTS 七事件中 SessionStart / SubagentStart / SubagentStop 在 harness 循环里另有挂接；mini 只落桥未挂循环（循环侧扩展口见第 6 章）。

