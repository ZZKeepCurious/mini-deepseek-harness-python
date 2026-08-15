# 06 · 附录：资源、HOWTO 与结语

<p class="lead">Python SDK 驱动 harness、添加插件与贡献、常见 HOWTO、参考速查与结语。</p>

<div class="pill-row">
  <span class="tag t-blue">everything is a plugin</span>
  <span class="tag t-blue">event-sourcing</span>
  <span class="tag t-blue">capability seams</span>
  <span class="tag t-green">TypeScript strict</span>
  <span class="tag t-green">Cordis vendored</span>
  <span class="tag t-amber">Python SDK 存在</span>
</div>

## 9. 用 Python SDK 驱动 harness

### 9.1 SDK 与 dsh 的关系和区别

**一句话关系**：Python SDK 不是 dsh 的替代品，也不是"另一个实现"，而是 **dsh 的进程级客户端**——它把内置的 dsh 运行时（单文件可执行 `dsh-jsonrpc-agent`）作为子进程启动，通过 stdio 上的换行分隔 JSON-RPC 协议驱动它。SDK 能做什么，取决于其组合里挂了哪些插件；它并不重新实现 harness。

| 维度 | dsh（产品本体） | deepseek-harness-sdk（Python 客户端） |
|---|---|---|
| 形态 | TypeScript monorepo：CLI + Web + 插件生态 + 组合系统，从源码构建运行 | PyPI 分发包 `deepseek-harness-sdk`（import `deepseek_harness`），内置捆绑运行时，安装后**不需要 Node.js** |
| 扮演角色 | 运行时产品本身（消费者直接面向用户） | 面向 Python 程序员的驱动层（`DeepSeekHarness` 上下文管理器，`run()` 一次任务） |
| 与运行时的连接 | 进程内直接调用插件 | 启动 `dsh-jsonrpc-agent` 子进程，走 stdio JSON-RPC；**同一协议在 TS 侧的对应实现是 `packages/sdk`（protocol/client/server）** |
| 配置方式 | profile / bundle / patch 组合树（cordis.yml） | 内置默认组合（stdio JSON-RPC 服务器 + agent core + DeepSeek 适配器 + JSONL 持久化 + 本地 bash）；传 `cordis=` 指向自己的 cordis.yml 即可换组合（需保留 `@deepseek-ai/dsh-sdk-jsonrpc-server` 条目） |
| 环境变量 | 继承 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DSH_HOME` 等 | 运行时继承同一套 `DEEPSEEK_*`；`DSH_SESSION_ROOT` 由 `session_root=` 参数设置；自定义组合经 `DSH_CORDIS_CONFIG` 注入 |
| 平台 | macOS / Linux 为主（Windows 部分支持） | Linux x64 / arm64、macOS 14+ arm64；持久 PTY bash 需 POSIX，**不支持 Windows agent**；内置组合是 `danger-full-access`，只能在可丢弃的 checkout/容器里跑 |

### 9.2 上手步骤（step-by-step）

1. **安装**（Python 3.10+）：

    ```bash
    python -m venv .venv
    . .venv/bin/activate
    python -m pip install deepseek-harness-sdk
    ```

    会自动装上同版本的 `deepseek-harness-runtime-bin` 平台 wheel，无需手动指定可执行文件。

2. **设置凭据**：`DEEPSEEK_API_KEY` 必须；若走 OpenAI 兼容代理再设 `DEEPSEEK_BASE_URL`。

    ```bash
    export DEEPSEEK_API_KEY=sk-your-key-here
    # export DEEPSEEK_BASE_URL=http://127.0.0.1:8000/v1
    ```

3. **最小调用**：

    ```python
    from deepseek_harness import DeepSeekHarness

    with DeepSeekHarness() as harness:
        result = harness.run("Say hi.")
    print(result.final_response)
    ```

    上下文管理器会延迟启动运行时子进程并在多次调用间复用。

4. **带 workspace / 会话持久化 / 自定义组合**：

    ```python
    from pathlib import Path
    from deepseek_harness import DeepSeekHarness

    with DeepSeekHarness(
        provider="deepseek-official",
        model="deepseek-v4-flash",
        max_tokens=49_152,
        cwd=str(Path("/absolute/path/to/workspace").resolve()),
        session_root=str(Path("/absolute/path/to/sessions").resolve()),
        cordis=str(Path("examples/jsonrpc-agent/minimal.cordis.yml").resolve()),
    ) as harness:
        result = harness.run("Inspect the repo and fix failing tests.", session_id="example-001")
    print(result.final_response)
    ```

5. **理解返回值**：`RunResult` 含 `session_id`、`final_response`（该区间根会话最后提交的助手文本）、`finish_reason`（最后一个 `turn/end` 的 `kind`，如 `completed`/`max-tokens`/`error`）、`events`（根会话事件）、`notifications`（根会话 + 全部已知后代）、`session_root`。

6. **session id 复用语义**：复用同一 harness + 同一 `session_id` 会**保留该会话的 Bash 进程**（工作目录、已导出的变量、shell 函数）。独立任务用新 id；只有需要延续同一段持久化对话才复用。

!!! warning "安全边界"
    内置组合是 `danger-full-access`——bash 与编辑器可改任何路径，且上下文压缩关闭。只应在可丢弃的 checkout 或容器内运行。仓库内置示例：`python examples/jsonrpc-agent/minimal.py --workspace <dir> --session-root <dir> --session-id example-001 "task"`。

## 10. 添加插件与贡献

所有扩展都走"在旁边挂插件"，不改核心。两条路径：**入门路径**是往 preset 里挂一个最小插件（5 分钟验证）；**正式贡献**是按仓库清单新增一个 workspace 包（含门禁、测试、文档约定）。

### 10.1 最小工具插件（入门路径）

最小工具就是一个有 `name/inject/Config/apply` 的 Cordis 插件 + `ctx.tools.register(defineTool(...))`。schema 自动进入 system-prompt 组装，卸载插件即注销工具：

```ts
import { readFile } from 'node:fs/promises'
import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'my-tool'
export const inject = ['tools']

export function apply(ctx: Context) {
  ctx.tools.register(defineTool({
    name: 'read_file',
    description: 'Read a file from disk.',   // 模型看到的描述
    parameters: {
      path: { type: 'string', required: true, description: 'Absolute path' },
      limit: { type: 'number' },             // 默认可选
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args, exec) {
      // args 由 schema 推导出类型 { path: string; limit?: number }
      return readFile(args.path, { encoding: 'utf8', signal: exec.signal })
    },
  }))
}
```

**execute 约定要点**：参数在进入 `execute` 前已按 `ParameterSchemaSpec` 校验；`exec` 携带不可变身份与 token，`exec.signal` 是唯一可替换的运行期字段（用于超时）；返回单一 canonical JSON 值；抛错或返回非法值即视为 `isError`。完整约定见 `docs/cookbook/adding-a-tool.md`。

**UI 渲染是设计的一部分**：用 `output.presentCall/presentResult` 返回 `card`-tagged render intent（`generic` / `terminal` / `diff` / `search` / `web`），且必须是 `args`（+ result）的纯函数——回放也要跑。见 11.3。

### 10.2 挂载到 preset 验证

在 `$DSH_HOME/profiles/<name>/cordis.patch.yml`（或 `--patch <path>` overlay）里写一个 patch 列表。文件是**顶层 YAML 数组**：按 `id` 定位的 patch 会**整段替换**目标行的 `config`（未改字段也要重述），`insert` 则插入新条目；`!!js` 表达式在挂载时插值。裸插件 `name` 从 profile 目录向上按 Node 模块规则解析，回退到 `$DSH_HOME/profiles/node_modules`。

```yaml
# cordis.patch.yml —— 顶层数组，insert 一个插件行
- insert:
    - id: my-tool
      name: my-tool            # 你的插件（安装进 profile 的 node_modules）
```

把插件安装进 profile 用 `dsh plugin --profile <name> add <spec>`（转发给 pnpm，profile 缺失时先初始化；相对路径 spec 锚定到调用目录，因此从插件 checkout 里执行 `add .` 装的就是该 checkout）。若是带 `"dsh": { "bundle": { "patch": "./cordis.patch.yml" } }` 声明的组合包，安装后会自动加入 `dsh.profile.bundles` 层栈。

!!! success "验收"
    `pnpm dsh --profile <name> --dump-config` 能看到该行（注释标注来源文件与 overlay）；跑一次任务观察工具被调用；对行为变化按 `docs/testing.md` 补测试。

### 10.3 正式贡献：新增 workspace 包（按清单）

完整 checklist 见 `docs/cookbook/adding-a-package.md`（以 bash 与 adapter 包为模板校验过）。流程：

1. **建包**：`packages/<group>/<pkg>/` 下四个文件——`package.json`（拷贝 `packages/core/tools`，改 name/description/deps）、`tsconfig.json`（extends `../../../tsconfig.base.json`，rootDir src，references 含 `vendor/cosmokit` + `vendor/cordis`（用 Config 时加 `schemastery`）+ 每个 dsh 依赖）、`src/index.ts`（service 默认导出或 `name/inject/apply/Config` 插件）、`README.md`（含 gated Model Experience 段 + Known Limitations 段）。
2. **注册进根配置**：新包组加 `tsconfig.base.json` 的 `@deepseek-ai/dsh-*` wildcard；Host/Client 包分别加 `tsconfig.host.json` / `tsconfig.client.json` 的 `references`（一个包只属于一个 aggregate）；有额外 entrypoint 才动 `knip.json`。workspaces、publint、tsdown、oxlint、constraints 全部由 glob 自动覆盖，不用改。
3. **决定拓扑**：可替换能力按 Service Definition / Provider / Consumer 拆包（shell 三人组是模板）；单一用途插件一个包。命名用稳定职责，role 后缀有词典（Controller/Store/Registry/Policy/Provider/Backend/…），`local` 仅在"约定包含同宿主执行"时用。
4. **写 README**：包自身 API/事件/扩展点在前，最后以固定四段收尾：`## Model Experience` → `### Request context and condition`（What the model sees / Token effect / KV Cache effect）→ `## Known Limitations and Deferred Work`。
5. **验证**：

    ```sh
    pnpm install
    pnpm run doc-sync
    pnpm run constraints && pnpm run typecheck && pnpm run lint
    pnpm run build && pnpm run hygiene
    ```

    再按 `docs/testing.md` 补行为测试与快照；非平凡变更**必须同 PR 附 Agent Note**。

!!! success "其它插件形态参考"
    `docs/cookbook/extension-cookbook.md` 有完整 feature → mechanism 映射：hook 插件（`ctx.on('tools/pre-execute', ...)` 返回 typed decision 的瀑布）、UI 插件（监听 `session/event` + `agent.followup()`）、外部协议驱动（把 wire peer 映射到 `ctx.agents`）、LLM 适配器（`LlmAdapter` 子类 + `ctx.llm.registerAdapter(['my-provider'], ...)`）。

## 11. 其他常用 HOWTO

### 11.1 读懂组合树与排查启动问题

- `dsh --profile <name> --dump-default-config`：只打印 bundle 层；`--dump-config` 额外含 profile `cordis.patch.yml`、home 级 `cordis.patch.yml`、`--patch` overlays。两者都注释每行来源文件与修改过它的 overlay，`!!js` 表达式保持未求值。
- 裸插件名解析失败、patch 目标找不到、schema 校验失败、启动失败都会报错并以非零退出——不会静默跳过。解析顺序：dsh 安装目录 → profile 目录 → `$DSH_HOME/profiles/node_modules` 维护的后备符号链接。
- 环境变量：`DSH_HOME`（默认 `~/.dsh`）、`DEEPSEEK_API_KEY`、可选 `DEEPSEEK_BASE_URL`。

### 11.2 无 key 联调与本地回放

- `pnpm mock:llm`：本地 mock LLM 服务器，无需 API key 跑通工具调用回合。
- `pnpm run test:snapshot`：keyless ACP/headless 回放，比对期望输出；`-t <name>` 过滤；`test:snapshot:record` 重录（需 key）。
- 会话即 JSONL 日志：模型可见历史由 `deriveMessages()` 投影，回放 = `sessions.create(id, { seed })`。任何模型可见输入都能从日志重建。

### 11.3 给工具配 UI 卡片（render intent）

`presentCall(args)` 返回待处理卡片，`presentResult(args, { content, isError, meta })` 返回完成卡片；二者**必须是纯函数**（直播与回放都跑，禁止 I/O/读会话状态/时钟）。卡片类型：

| card | 用途 | 参考工具 |
|---|---|---|
| `generic` | 默认；可设 `locations: [{path, line?}]` 让编辑器跟随 | 大多数工具 |
| `terminal` | 调用本身就是一条 shell 命令 | tool-bash |
| `diff` | 创建/修改文件，内联 diff | tool-fs write/edit |
| `search` | 按文件分组的发现结果（仅 result 视图） | tool-fs-search grep/glob |
| `web` | 完成的网页检索，按 `kind: search\|fetch` 判别 | tool-web |

UI 专用格式（fenced console、diff、相对路径）留在卡片投影里，绝不进 canonical 值或 Native 内容；持久化的 replayable 卡片数据走 `output.presentationMeta(args, value)`，经 `tool/result` 的 `meta` 回放复原。

### 11.4 常用参考速查

- **事件**：`docs/subsystems/event-producer-consumer.md` + 每包组 subsystems 页；事件 JSDoc 需 `@mode` 与 payload `@param`。
- **测试**：`docs/testing.md`（门禁 `test:coverage` 每文件 100%；模型/用户可见行为要 keyless 快照）。
- **Cordis 入门**：`docs/cordis-primer.md` + `docs/cordis-tutorial/` 动手教程。
- **文档规范**：`docs/AGENTS.md`；贡献任何非平凡变更需 Agent Note（`.agents/notes/`），已归档的冻结。
- **协议栈**：SDK wire 协议见 `packages/sdk/protocol`；ACP 自动化协议见 `packages/acp/acp/README.md`；Claude Code/Codex hooks 桥接见 `packages/hooks`。

## 12. 结语

DeepSeek Harness 的本质可以用一句话概括：

<div class="card" style="font-size:16px; text-align:center;" markdown>

**一个"无特权内核 + 可逆副作用注册"的 Cordis 插件树，以事件溯源会话日志为唯一数据源，通过"Service Definition / Provider / Consumer"三段式能力扩展口把模型适配、工具执行、沙箱、子 agent 等一切能力做成可配置替换的插件；外层由 profile / bundle 补丁层组合出 Web / headless / SDK 等运行形态，并辅以内容寻址的生成器门禁与每文件 100% 覆盖率纪律。**

</div>

对想用 Python 深刻掌握并复现的你，最有价值的三件事依次是：**(1) 事件溯源日志作为唯一数据源；** **(2) "注册 = 可逆副作用 + waterfall 短路"的插件语义；** **(3) 能力扩展口三角色带来的整体可替换性。** 把这三件事用 Python 亲手实现一遍，对 dsh 的理解会超过大多数读过一遍文档的人。