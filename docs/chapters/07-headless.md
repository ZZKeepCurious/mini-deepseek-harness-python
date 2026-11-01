[← 第 7 章导读](07-external-entry-points.md)

入口组合：[Fig S5](../system-map.md#fig-s5)。

# 第 7 章 · §7.2 headless

## 7.2 headless：任务文本即命令行 {#sec-7-2}

headless 是最容易理解的一个入口，它把"跑一个 agent 回合"压缩成一条 shell 命令：

```sh
dsh --profile headless "run the tests"
```

进程语义（`packages/bundle/headless/README.md` 全文就是这几句话）：

1. 任务文本就是这个应用的命令行：位置参数按空格 join；缺失或纯空白 → usage error，进程退出 1；位置参数为 `-` 时改读 stdin。
2. 启动后创建一个全新的持久化 Agent（session id 随机），把任务作为普通用户消息提交；给出 `--session <id>` 时改为恢复该会话（先校验身份与 cwd 可采纳）。
3. 等它完全停稳（quiescence），先 flush 会话，再汇总本次运行的事件区间。
4. 把最后一条非空 assistant 文本写到 stdout（带换行）；`--json` 时改为输出逐行 JSON 运行投影。
5. 按最终 `turn/end` 的 reason 决定退出码：`completed` → 0，其它（`error`、`blocked`、`max-tokens`……）→ 1；`error` 时 stderr 再写一行 `dsh: <code>: <message>`。
6. 进程不开任何监听端口。

这个入口把前四章的所有约定变成了可观测的进程级约定：你能在 stdout 上直接检验"模型说了什么"，用退出码检验"回合是否正常结束"。`headless` 的"h"是"没有交互、跑完即退"，不是 headless browser。

### 源码解剖：runner 与 startup

`dsh-headless` bundle 里只有两个插件（`packages/bundle/headless/cordis.patch.yml`）：

- `headless-startup`（`src/startup.ts`）：解析命令行，把任务作为普通 Cordis 服务 `headlessStartup` 发布。空任务在这里被拒绝：`program.error('error: a task is required, ...')`，拒绝时不发布服务，下游 runner 也就不会激活。
- `headless-runner`（`src/index.ts`）：注入 `headlessStartup`，从 lazy config 读任务，然后执行上面那 6 步。

runner 的 `run` 函数（`src/index.ts`）值得逐行看一遍，因为它把前几章的约定串了起来：

```ts
await ctx.get('loader')?.await // 等整棵插件树装完，避免半组合状态
const selection = defaultModel.currentSelection
const agentOptions = { provider: selection.provider, model: selection.model }
const agent = config.sessionId === undefined
 ? (await agents.create({ // 全新持久化 Agent，随机 session id
 sessionId: SessionId(`session-${randomUUID}`),
 meta: { cwd }, agentOptions, setup,
 })).agent
 : await resolveAgent(ctx, agents, sessionId, agentOptions, setup, cwd)
await agent.whenIdle // 首次停稳（没有任何输入）
const firstSeq = agent.session.seq // 记住区间起点
agent.followup(createUserMessage({ content: [{ type: 'text', text: task }] }))
await agent.whenIdle // 任务回合完成
await sessions.flush(agent.session) // 先写入磁盘，再汇总
const outcome = summarize(agent.session, firstSeq)
io.stdout.write(outcome.text + '\n')
if (outcome.reason?.kind === 'error') {
 io.stderr.write(`dsh: ${outcome.reason.error.code}: ${outcome.reason.error.message}\n`)
}
io.exit(outcome.reason?.kind === 'completed' ? 0 : 1)
```

几个实现细节：

- **`firstSeq` 之后才算数**：summarize 只汇总本次运行产生的事件，会话打开前的历史（恢复场景）不影响输出。这对应第 5 章的恢复语义。
- **summarize 只拼 text 块**（`src/index.ts`）：reasoning 块、tool-call 块都被过滤，空文本不覆盖已有结果，所以"最后一条非空 assistant 文本"是精确语义。
- **错误有两条不同的 stderr 路径**：回合正常结束但 reason 是 error（如模型层带内失败）→ `dsh: code: message`；runner 自身抛异常 → `run.catch(fail)` 只写 `dsh: <message>`（不写 code）。两条都退出 1。
- **`ctx.appExit` 由启动器持有**（`src/index.ts`）：runner 不在启动器外运行，没有 appExit 就在激活时报错。进程退出请求是宿主注入的能力，runner 自己不开 exit 后门。

### mini 复现对照

mini 的 `miniharness/cli/headless.py` 复现了任务文本、停稳后汇总、stdout 与退出码这几条进程语义，载体差异有三处：

1. 上游经 Cordis 服务（`agents` / `sessions` / `agentDefaultModel`）创建 Agent；mini 直接构造 `Session + AgentLoop`，再经 `publish` 进入会话店（约定不变）。
2. 上游错误经 `finish {kind:'error'}` 带内失败或异常两条路；mini 的 `LlmFailure` 一律以异常抛出（`llm.py` 已声明该简化），所以 `dsh: code: message` 分支通常走不到，保留是为了与上游格式一致。
3. 上游的 `--json` 逐行投影与 `--session <id>` 恢复选项，mini 的 CLI 未暴露；`run_headless(..., session_id=...)` 在 API 层支持恢复。

运行方式：

```sh
python -m miniharness.cli.headless "run the tests" # 直接运行
python -m miniharness.cli --profile headless "task" # 走启动器（同 dsh CLI）
miniharness --profile headless "run the tests" # 安装后（pyproject scripts）
```

CLI 解析与上游一致：`--profile headless` 之后的位置参数 join 空格、空任务 usage error 退出 1、未知 profile fail loud。`--profile web` 启动服务表层，不接受任务参数。

