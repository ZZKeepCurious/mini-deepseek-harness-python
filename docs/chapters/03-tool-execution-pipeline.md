# 第 3 章：工具执行管线（Tool Execution Pipeline）

> 对应 dsh 真实源码：`packages/core/tools`（`docs/subsystems/tools.md`、`docs/tool-execution-pipeline.md`）
> 前置：第 1、2 章。产出文件：`miniharness/miniharness/tools.py` + `tests/test_tools.py`

## 3.1 这一章要做什么

工具是 agent 与世界的接口，执行管线则是这个接口上的全部把关。常规的 agent 框架里，"调用工具"往往就是一行 `tool(args)`：没有参数校验、没有权限检查、超时靠进程级兜底、出错直接抛异常打断回合。dsh 的 `packages/core/tools` 把这一行扩成了一条八段管线，每一段解决一个常规做法留下的问题：

1. 参数物化冻结——策略和工具体必须看同一个不可变参数
2. schema 校验——坏参数在进工具前就被拦下
3. `pre-execute`——谁允许调用这个工具
4. 单调守卫——权限只能减、不能加
5. `execute`——超时由管线强制执行
6. `post-execute`——结果先过审再给模型
7. 规范化——任何异常都变成结构化错误
8. 冻结结果——模型只能看到不可变数据

三个硬性规定先记住：

1. **参数在策略前一次性无损物化并冻结**——策略和工具体看到的是同一个不可变视图。常规做法是各看各的副本，策略改了参数、工具体看到的却是旧值，排查起来很痛苦。
2. **守卫只能减权**（单调）：deny 后工具体被跳过；乱序也无法撤销一个已发生的 deny。
3. **任何异常都规范化为 `isError` 的结构化结果**——回合继续，模型拿到错误消息自己处理。常规做法是异常向上抛，把回合打断。

## 3.2 概念：管线全貌

```mermaid
flowchart TD
  M["assistant 消息含 tool-call block"]
  PRE["tools/pre-execute waterfall&lt;br/&gt;allow / deny / ask"]
  ASK["ask → 一次性询问&lt;br/&gt;absent → deny"]
  DEN["denied&lt;br/&gt;工具体被跳过"]
  G["单调守卫&lt;br/&gt;只减权"]
  EX["tools/execute&lt;br/&gt;超时 / 重试（around-dispatch）"]
  BODY["execute() 体"]
  POST["tools/post-execute&lt;br/&gt;accept / replace / block"]
  NORM["规范化&lt;br/&gt;异常 → isError"]
  RES["冻结的权威结果"]
  M --> PRE --> G --> EX --> BODY --> POST --> NORM --> RES
  PRE -->|deny| DEN
  PRE -->|ask| ASK
  ASK -->|拒绝| DEN
  G -->|deny| DEN
```

## 3.3 代码 step-by-step

### 步骤 1：JSON Schema 子集校验器

```python
def validate_schema(value, schema):
    """type / properties / required / items / enum 子集校验。"""
    errors = []
    _check(value, schema, "$", errors)
    return errors
```

只实现 `type` / `properties` / `required` / `items` / `enum` 这个子集。为什么不做一个完整的 JSON Schema 实现？因为工具的 `parameters` 声明用到的就是这些，完整实现（引用、条件校验、格式断言）在工具场景里几乎用不到，复杂度却高一个量级。真实 dsh 的做法更重：先做"16 层容器精确推断"，推断不出再回退 `JsonValue`。我们取子集，够用且诚实——简化声明见 00 章。

一个容易漏的兼容点：参数此刻已经被 `deep_freeze` 冻结成了 `MappingProxyType` / `tuple`，校验器遍历时不能依赖 `isinstance(x, dict)` 判断结构——`MappingProxyType` 不是 `dict` 的子类。实现里对这两类都要认。

### 步骤 2：领域对象

```python
@dataclass
class ToolExec:
    """执行上下文：signal 是唯一可替换的字段（超时/取消用）。"""
    signal: threading.Event = field(default_factory=threading.Event)

@dataclass
class Tool:
    name: str
    description: str
    execute: Callable[[dict, ToolExec], Any]
    parameters: dict = field(default_factory=dict)   # JSON Schema
    output: dict = field(default_factory=dict)       # canonical schema
    is_concurrency_safe: bool = False                # False = 串行屏障
    timeout_ms: int | None = None                    # 由管线 wrapper 强制
    present_call: Callable | None = None             # UI 挂起卡片（纯函数）
    present_result: Callable | None = None           # UI 完成卡片（纯函数）

@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: Any = None
    is_error: bool = False
    error: str | None = None
    meta: dict = field(default_factory=dict)
```

`frozen=True`：结果不可变，这就是"冻结的权威结果"——模型的输入永远来自不可变数据。

`Tool` 上有几个字段值得先认识：

- `timeout_ms` 由管线强制，**绝不发给模型**。这一点到第 4 章会看到它落进日志时是过滤掉的——工具配置是宿主机密，不是上下文内容。
- `is_concurrency_safe = False` 是默认值：不确定安全就按不安全处理（串行屏障），宁可慢不能乱。第 7 阶段的并行化就靠这个标记调度。
- `present_call` / `present_result` 是给 UI 的"挂起卡片"回调，纯函数，不影响管线语义。常规框架把 UI 逻辑塞进工具内部，dsh 把它抽出来挂在工具声明上。

### 步骤 3：作用域化注册表

```python
class ToolRegistry:
    def __init__(self, root):
        self.root = root
        self._tools = {}
        root.provide("tools", self)     # ctx.tools 服务

    def register(self, tool, scope=None):
        bucket = self._bucket(scope)
        if tool.name in bucket:
            raise RuntimeError(f"工具 {tool.name} 已注册")
        bucket[tool.name] = tool
        return lambda: bucket.pop(tool.name, None)   # disposer

    def resolve(self, name, scope=None):
        """可见性：自身 → 祖先作用域链 → 全局层。"""
        node = scope
        while node is not None:
            bucket = getattr(node, "_scoped_tools", {})
            if name in bucket:
                return bucket[name]
            node = node.parent
        return self._tools.get(name)

    def restrict(self, allow=None, deny=None):
        """ToolRestriction：deny 优先，其次 allow 白名单。"""
        def allowed(name):
            if deny and name in deny:
                return False
            if allow is not None and name not in allow:
                return False
            return True
        return allowed
```

常规工具注册表是一个全局 `name → 工具` 字典，所有 agent 共享。dsh 的问题是"per-agent 工具隔离"：一个 agent 应该只能看到它被允许的工具。`resolve` 的三段式查找（自身 → 祖先作用域链 → 全局层）就是第 2 章作用域链的直接应用。

`restrict` 是继承过滤谓词：agent 级策略用"白名单 + deny 黑名单"裁剪继承来的工具，而不用逐个注册。`deny` 优先于 `allow`——被点名的工具无论如何都不放行。

重复注册直接抛错（fail loud），与第 2 章 `provide` 的行为一致。静默覆盖会让"我注册的工具为什么变成了别人的实现"成为谜。

### 步骤 4：执行管线（核心）

```python
def run_pipeline(ctx, tool, args, exec_=None):
    exec_ = exec_ or ToolExec()

    # 1. 参数一次性无损物化 + 深度冻结
    frozen_args = deep_freeze(dict(args))

    # 2. schema 校验
    schema_errors = validate_schema(frozen_args, tool.parameters)
    if schema_errors:
        return ToolResult(ok=False, is_error=True, error="; ".join(schema_errors))

    # 3. pre-execute waterfall（allow | deny | ask）
    decision = ctx.waterfall("tools/pre-execute", {"tool": tool.name, "args": frozen_args})
    verdict = decision.get("verdict", "allow") if isinstance(decision, dict) else "allow"
    if verdict == "deny":
        return ToolResult(ok=False, is_error=True, error="denied by tools/pre-execute")
    if verdict == "ask":
        approved = ctx.waterfall("tools/ask", {"tool": tool.name, "args": frozen_args})
        if approved is not True:
            return ToolResult(ok=False, is_error=True, error="approval refused")

    # 4. 单调守卫（只减权）
    guard = ctx.waterfall("tools/guards", {"tool": tool.name, "args": frozen_args})
    guard_verdict = guard.get("verdict", "allow") if isinstance(guard, dict) else "allow"
    if guard_verdict == "deny":
        return ToolResult(ok=False, is_error=True, error="denied by monotonic guard")

    # 5. execute（超时由管线强制）
    box = {}
    def target():
        try:
            box["value"] = tool.execute(dict(frozen_args), exec_)
        except Exception as e:
            box["error"] = e
    if tool.timeout_ms:
        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(tool.timeout_ms / 1000)
        if t.is_alive():
            exec_.signal.set()      # 通知工具体"你超时了"
            return ToolResult(ok=False, is_error=True, error=f"timeout after {tool.timeout_ms}ms")
    else:
        target()

    # 6. post-execute waterfall（accept | block）
    raw = box.get("value")
    post = ctx.waterfall("tools/post-execute", {"tool": tool.name, "result": raw})
    if isinstance(post, dict) and post.get("action") == "block":
        return ToolResult(ok=False, is_error=True, error=post.get("feedback", "blocked"))

    # 7. 规范化：异常 / 非法值 → isError
    if "error" in box:
        e = box["error"]
        return ToolResult(ok=False, is_error=True, error=f"{type(e).__name__}: {e}")
    if isinstance(raw, ToolResult):
        return raw
    if isinstance(raw, dict) and raw.get("isError"):
        return ToolResult(ok=False, content=raw.get("content"), is_error=True, error=raw.get("error"))
    if not is_json_safe(raw):
        return ToolResult(ok=False, is_error=True, error="工具返回了不可 JSON 序列化的值")

    # 8. 冻结的权威结果
    return ToolResult(ok=True, content=deep_freeze(raw))
```

逐段拆开说：

**步骤 3-4 是第 2 章 waterfall 的实际应用**：策略插件不调 `next()` 即 deny。`ask` 分支是"需要人类批准"的场景：一次性询问，缺席（没人回答）按拒绝处理——审批不能默认放行。第 4 章 Agent Loop 会把 `tool/call`、`tool/result` 事件挂在步骤 1 和 8 前后，形成"先记录、后执行"的审计链。

**超时**：用线程 + `join(timeout)` 强制。超时后设置 `exec_.signal`——这是给工具体的协作取消信号，愿意配合的工具可以借此尽快收尾。为什么不直接在工具内部做超时？因为超时是宿主策略，不该让每个工具作者各自实现一遍。真实 dsh 挂在 `tools/execute` 的 around-dispatch 上，语义相同。

**规范化**：三种"出错形态"——抛异常、返回 `isError` dict、返回不可序列化的值——全部收敛为 `ToolResult(is_error=True)`。这是"回合不中断"承诺的落实：模型收到的是结构化错误消息，而不是一个崩溃的回合。常规做法里异常向上抛，一次工具崩溃整个会话就没了。

## 3.4 验收：硬性规定 + 测试

`tests/test_tools.py` 钉住的规定：

1. 参数在策略前冻结；schema 违规 = 结构化错误（不进回合）
2. `pre-execute` deny / approval 拒绝 → 工具体被跳过
3. 超时被强制执行，且绝不把 timeout 配置发给模型
4. 异常 / 非法值规范化成 `isError`，不抛出
5. 结果冻结不可变；作用域注册卸载后不可见

```bash
python -m unittest tests.test_tools -v
```

## 3.5 检查点练习

1. **并行屏障**：实现一个简化版 `is_concurrency_safe` 调度——安全工具并行（线程池），非安全工具串行。给注册表加一个 `concurrency_batches()`，并写测试。
2. **重试 wrapper**：写一个挂在 `tools/execute` waterfall 上的监听器，对第一次失败自动重试一次（around-dispatch 语义：`nxt` 包住真实执行）。断言最终结果。
3. **限制工具集**：用 `restrict` 给一个作用域做"只允许 bash + read_file"的白名单，写测试断言白名单外工具被过滤。

## 3.6 回到 dsh：真实源码对照

打开 `deepseek-harness/packages/core/tools/src` + `docs/tool-execution-pipeline.md`：

- `tools/pre-execute` / `execute` / `post-execute` 三个 waterfall 的真实事件约定，字段名与我们一致
- 注册表外层规范化的真实位置（snapshot 异常 → isError）
- `finalizeContent`：最后一个内容只读不变量（简化版没有实现，值得知道它存在）

一个语义差异需要明说：上游 `tools/post-execute` 是"无损物化前的 content 替换钩子"——返回 `undefined` 即保留原内容，返回其他值替换 content。简化版把它做成 accept/block 门（拒绝时返回 `{verdict: "block", feedback}`）。方向不同，但落点相同：模型最终只能看到规范化的结果。

## 3.7 收尾

八段管线看完，记住一条主线：**所有把关都发生在参数和结果两端，中间只有执行**。策略、守卫、超时、规范化——每一段都是"防止意外状态逃出管线"。下一章把这些接到 Agent Loop 上：`tool/call` 先落日志再执行，`tool/result` 回灌给模型，回合因此可审计、可回放。