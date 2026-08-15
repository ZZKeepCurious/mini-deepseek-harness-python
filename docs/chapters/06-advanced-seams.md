# 第 6 章：进阶扩展口（选做，任选其一深入）

> 对应 dsh 真实源码：`docs/capability-seams.md` + 各子系统页（sandbox / credentials / subagent）
> 前置：第 1~5 章。产出文件：`miniharness/miniharness/seams.py` + `tests/test_seams.py`

## 6.1 这一章要做什么

前面几章反复出现一个模式：接口 + 可替换实现 + 只依赖接口的消费方。这一章把这个模式正式化——"能力扩展口三角色"，并且亲手实现三遍，每一遍都在验证同一句话：

> **换一个 Provider，不改 Consumer，即换行为。**

三个扩展口，任选其一做深（各自 2~3 天工作量）：

1. **沙箱**：`Sandbox` 接口 + `wrap(argv)`——把 argv 包裹进受限执行环境
2. **凭据**：`CredentialProvider` 接口 + `resolve(key)`——配置只存引用，每次操作解析
3. **子 agent**：`SubAgentProvider` 工厂接口 + `spawn(name, prompt)`

## 6.2 概念：扩展口三角色

```mermaid
flowchart LR
  D["Service Definition&lt;br/&gt;接口 + 生命周期 + 错误码约定"]
  P["Service Provider&lt;br/&gt;实现（可整体替换）"]
  C["Consumer&lt;br/&gt;只依赖接口"]
  P -->|"实现"| D
  C -->|"依赖"| D
```

一个角色单独不构成扩展口。新增能力 = 同时设计三个角色：定义接口（Service Definition）、提供实现（Provider）、写消费逻辑（Consumer）。三者缺一，替换性就不成立——没有接口，Consumer 直接依赖具体实现，一换就炸。

还有一个联动值得知道：**共享执行世界**。dsh 里 FS 与 subprocess 的 Provider 指向远程沙箱后，Bash / PTY / LSP 会全部跟随迁移——因为它们共享同一个"执行世界"对象，换一次配置，所有相关能力一起搬家。

## 6.3 扩展口 1：沙箱（Sandbox）

```python
class Sandbox:
    """Service Definition：把 argv 包裹进受限执行环境。"""
    def wrap(self, argv):
        raise NotImplementedError

class PassthroughSandbox(Sandbox):
    """本地直通（danger：不设防，等同 danger-full-access）。"""
    def wrap(self, argv):
        return argv

class ReadOnlySandbox(Sandbox):
    """模拟只读沙箱：含写操作标志的命令直接拒绝（失败即拒）。"""
    WRITE_MARKERS = ("-w ", "--write", "-o ", ">", ">>", "rm ", "mv ", "touch ", "mkdir ")

    def wrap(self, argv):
        cmd = " ".join(argv)
        if any(marker in cmd for marker in self.WRITE_MARKERS):
            raise PermissionError(f"只读沙箱拒绝写操作: {cmd}")
        return argv

class CommandConsumer:
    """Consumer：只依赖 Sandbox 接口。换 Provider 即换行为。"""
    def __init__(self, sandbox):
        self._sandbox = sandbox

    def run(self, command):
        if os.name == "nt":
            argv = self._sandbox.wrap([command])
            proc = subprocess.run(argv[0], shell=True, capture_output=True, text=True, timeout=10)
        else:
            argv = self._sandbox.wrap(shlex.split(command))
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return proc.stdout.strip() or proc.stderr.strip()
```

常规做法是把安全策略写死在工具内部（"这个工具自己小心点"）。dsh 反过来：策略是独立的 Provider，工具（Consumer）只声明"我要执行一个命令"，至于在什么环境里执行，由注入的沙箱决定。

`PassthroughSandbox` 对应 `danger-full-access`（不设防），`ReadOnlySandbox` 对应 `read-only` 的简化模拟。验收测试的关键是 Consumer 代码一字不改：

```python
passthrough = CommandConsumer(PassthroughSandbox())
readonly = CommandConsumer(ReadOnlySandbox())
# Consumer 代码不变，只换 Provider
with self.assertRaises(PermissionError):
    readonly.run("echo a > tmp_evil.txt")
assert passthrough.run("echo ok") == "ok"
```

注意 `ReadOnlySandbox` 的"失败即拒"（deny on failure）：检测到写操作标志直接抛错，而不是"试试看能不能写"。dsh 的真实约定也是如此——无法确认安全就拒绝执行。

> 真实 dsh：`sandbox-local` 用 bwrap / Landlock / Seatbelt / Windows ACL 后端；`native/landlock-run` 是 Node 插件。失败即拒是共同约定。

## 6.4 扩展口 2：凭据（Credentials）

```python
class CredentialProvider:
    """Service Definition：按操作解析凭据；配置只存引用，绝无明文。"""
    def resolve(self, key):
        raise NotImplementedError

class EnvCredentialProvider(CredentialProvider):
    """env-over-.env：配置项 -> 环境变量名（引用），每次调用解析。"""
    def __init__(self, mapping=None):
        self._mapping = mapping or {"api_key": "DEEPSEEK_API_KEY"}

    def resolve(self, key):
        env_name = self._mapping[key]
        value = os.environ.get(env_name, "")
        if not value:
            raise KeyError(f"凭据 {key}（环境变量 {env_name}）未配置")
        return value
```

常规做法是配置文件里直接写 key（或者至少把 key 读进内存常驻）。dsh 的纪律是：配置里存的是 `apiKeyEnv` 这样的**引用**，不是明文；`ctx.credentials` 让 API key **每次调用**解析——改环境变量不用重启进程，而且配置仓库里永远找不到一个真实的 key。

第 4 章 `DeepSeekAdapter` 里 `os.environ.get("DEEPSEEK_API_KEY")` 就是 `EnvCredentialProvider` 的雏形；这里把它升级成接口，是为了给 `.env` 文件、keyring、提示注入等来源留位置。

## 6.5 扩展口 3：子 agent（SubAgent）

```python
class SubAgent:
    def run(self, task): raise NotImplementedError

class SubAgentProvider:
    """Service Definition：子 agent 工厂。"""
    def spawn(self, name, system_prompt): raise NotImplementedError

class InProcessSubAgentProvider(SubAgentProvider):
    """in-process Provider：复用主循环（真实还有 fork / ACP / Codex / Claude Code）。"""
    def __init__(self, make_loop):
        self._make_loop = make_loop
    def spawn(self, name, system_prompt):
        return _InProcessSubAgent(self._make_loop(system_prompt))

class _InProcessSubAgent(SubAgent):
    def __init__(self, loop):
        self._loop = loop
    def run(self, task):
        self._loop.followup(task)
        return self._loop.last_response()
```

子 agent 的 Provider 选择直接决定"子 agent 跑在哪"：同进程复用主循环（in-process）、独立进程（fork）、外部协议（ACP）、甚至另一个商业 CLI（Codex / Claude Code）。Consumer（`subagent` 工具）只依赖 `spawn + run`，不知道也不关心子 agent 背后是什么。

> 真实 dsh 的 `ctx.subagents` Provider 有 in-process / fork / ACP / Codex / Claude Code / dsh-sdk 六个。把我们的 `InProcessSubAgentProvider` 换成任何一个，`spawn + run` 的调用方代码一字不改。

## 6.6 验收

```bash
python -m unittest tests.test_seams -v
```

## 6.7 检查点练习（挑一个做深）

1. **沙箱**：实现 `DenyListSandbox`（基于 deny 黑名单）与 `AllowListSandbox`（基于 allow 白名单）两个 Provider，共享一个测试套件证明 Consumer 不变。
2. **凭据**：实现 `FileCredentialProvider`（从 `.env` 文件读取，逐行 `KEY=VALUE`），与 `EnvCredentialProvider` 共用同一接口测试。
3. **子 agent**：用第 4 章的 `DeepSeekAdapter` 实现 `RemoteSubAgentProvider`（真实 API），跑一次"主 agent 派发任务给子 agent"的完整链路。

## 6.8 回到 dsh：真实源码对照

打开 `deepseek-harness/docs/capability-seams.md`：

- 完整扩展口列表与每个扩展口的三角色实例
- "共享执行世界"的迁移机制（FS/subprocess Provider 指向远程沙箱 → Bash/PTY/LSP 跟随迁移）

三个扩展口与上游的接口差异（简化命名，语义一致）：

| 扩展口 | 我们的接口 | 上游真实接口 | 备注 |
|---|---|---|---|
| 沙箱 | `Sandbox.wrap(argv)` | `SandboxProvider.confine(argv): ConfinedArgv`（含 runner/profile/分离符 + `allowedExitCodes`/`fatalSignatures`/`denialSignatures`） | `SandboxMode`：`read-only` / `workspace-write` / `danger-full-access`；"失败即拒"约定一致 |
| 凭据 | `CredentialProvider.resolve(key)` | `resolve(ref): ResolvedCredential`（值 + 来源层）+ `describe(ref)`；本地 provider 层：`env` / `file` / `project-env` / `user-env` | 引用是带 brand 的 POSIX 环境变量名语法；每次操作重新解析 ✓ |
| 子 agent | `SubAgentProvider.spawn(name, prompt)` | `SubagentProvider.start(...)` + `prepareContinuable`（可继续对话）+ `SubagentCapabilities` 能力门（不支持则 `UNSUPPORTED_CAPABILITY` 拒绝） | 六个真实 Provider：in-process / fork / ACP / Codex / Claude Code / dsh-sdk |

## 6.9 进阶实现：真后端 / 四层凭据 / 远程三通道

基础三件套讲清"换 Provider 不改 Consumer"；进阶三件把每个接缝推向与 dsh 对齐的形态（产出：`miniharness/sandbox_local.py` + `credentials_local.py` + `subagent_providers.py` + `subagent_worker.py`，`tests/test_stage6.py` 49 测试）。

**沙箱真后端**（`sandbox_local.py`，对应上游 `sandbox/sandbox-local`）：按平台选链（linux `bwrap → landlock`、darwin `seatbelt`、win32 `windows-acl`），多候选由功能探测仲裁、单候选免探测；候选全不可用 → `SandboxUnavailableError`（`SANDBOX_UNAVAILABLE`）fail closed，命令绝不裸跑。`confine(argv, policy)` 返回 `ConfinedArgv`：包裹后的 argv + `enforcement`（full/partial）+ 该后端专属的 denial 方言与 runner 失败规则——"命令没跑起来"与"被沙箱拦住"可区分。三个 profile 生成器与上游 `profiles.ts` 逐条对齐（bwrap 挂载、landlock grant、seatbelt SBPL 剖面，可写根与进程内 fs fence 共用 `writable_roots` 同一推导）。Windows 宿主机上 windows-acl runner 缺省探测恒失败（fail-closed 与真实宿主一致）；约定测试经 `internals` 注入钩子验证各链选择、探测仲裁与包裹形状（同上游 `SandboxInternals` 思路）。

**凭据四层**（`credentials_local.py`，对应上游 `credentials-local`）：`env > file > project-env > user-env` 按信任度排序——继承环境只读胜出（CI secret / `-e` 是显式意图且进程内不可编辑）、管理文件层可写（`set`/`unset` 读-改-写补丁单键，外部编辑合并、删掉的条目不残留）、project `.env` 优先于 user `.env`。文档解析严格：非映射根 / 非 POSIX 标识符 key / 非字符串 / 空串值整体拒绝，绝不静默跳过；`describe` 报告 `{configured, source, writable}`（只有 env 层不可写）；env 已提供时 `set`/`unset` 拒绝（写了也被遮蔽成无效果）；POSIX 上组/其他可读的文档读前直接拒绝（Windows 无 mode 可查则跳过）。载体简化：文档用 JSON 替代 YAML（解析语义不变），无跨进程锁与文件 watch。

**子 agent 远程三通道**（`subagent_providers.py` + `subagent_worker.py`，对应上游 `subagent-fork-in-process` / `subagent-acp` / `subagent-dsh-sdk`）：

- **fork**（进程内）：子 agent 以父会话日志的 completed-turn 前缀作 seed（到最后一个 `turn/end` 为止，in-flight 工具回合不平衡不能重放），`Session(seed=...)` 回放 + 自动补 `session/end-seed` 标记——子会话直接继承父上下文。
- **ACP**（真子进程）：`python -m miniharness.subagent_worker acp` 起独立进程，newline-delimited JSON-RPC over stdio（与上游 `ndJsonStream` 同帧形状）；`initialize → newSession → prompt → cancel → shutdown`；唯一从父读的是 workspace cwd；permission 策略自动应答（reject 默认 / allow），不上报人；事件通知先于响应帧写出（mini 同步载体约定，上游为并发流）。
- **SDK**（真子进程）：`subagent_worker sdk` 承载 `SdkRuntime`，`initialize → session/prompt`（懒创建会话）→ `shutdown`；assistant 输出经 `session.event` 通知收集。

三者保持同一 Consumer 接口 `spawn(name, prompt) -> SubAgent`：换通道只改 Provider 构造，消费方代码不动。

## 6.10 手册收尾

全部 6 章做完，你应该能用 Python 亲手证明这三件事（报告第 12 节同样强调）：

1. **事件溯源日志是唯一数据源**（第 1、5 章）
2. **注册 = 可逆副作用 + waterfall 短路**（第 2、3 章）
3. **能力扩展口三角色带来整体可替换性**（第 3、6 章）

如果这三件事你现在都能不查资料写出来，对 dsh 的理解就已经到位了。接下来可以：

- 继续第 12 章：把 MiniHarness 换成异步（`asyncio` + 真正的 parallel / parallel barrier）——已完成，见第 12 章
- 用官方 Python SDK（`deepseek-harness-sdk`）驱动真实 harness，对照你的约定
- 给 dsh 仓库提第一个插件 PR（`docs/cookbook/adding-a-tool.md`）