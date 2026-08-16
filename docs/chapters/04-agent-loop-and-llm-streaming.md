# 第 4 章：Agent Loop 状态机 + LLM 流式

> 对应 dsh 真实源码：`packages/core/agent-loop` + `packages/llm/llm` + `packages/llm/llm-deepseek`
>（`docs/agent-lifecycle.md`、`docs/subsystems/llm-streaming.md`）
> 前置：第 1~3 章。产出文件：`miniharness/llm/`、`miniharness/core/agent_loop/` + `tests/test_loop.py`

!!! warning "早期简化形态"
    本章代码为**教学简化形态**，与当前实现存在以下差异（学习时以当前实现为准，见 00-setup §0.5 简化表与 AGENTS.md 已核实清单）：

    - **`FakeLlmAdapter` finish reason**：本章为字符串（`"stop"` / `"tool-calls"`）；实现为对象 `{"kind": "stop"}` / `{"kind": "tool-calls"}`（`llm/fake.py:38,45`）。
    - **`DeepSeekAdapter` SSE**：实现要求字面 `[DONE]` 必须出现（EOF 未到 `[DONE]` 抛 `STREAM_CLOSED`）、畸形 SSE 载荷抛 `MALFORMED_RESPONSE`、HTTP 错误映射完整（401/403→AUTH、quota 措辞→QUOTA、429→RATE_LIMIT、400 上下文→CONTEXT_WINDOW_EXCEEDED 否则 INVALID_REQUEST、≥500→SERVER、其余 `HTTP_<status>`）、`usage` 归一为 `TokenUsage`（`llm/deepseek.py`，见 `llm/protocol.py` 的 `StreamChunk` 判别字段 `type`）。本章的 `AUTH_ERROR`/`REQUEST_ERROR` 二元映射已过时。
    - **空响应**：本章 §4.4 声称"空响应未实现"与同章 §4.8/§4.10 自相矛盾——实现已产出 `EMPTY_RESPONSE` 错误且默认可重试（`llm/retry_policy.py` 白名单）。
    - **loop 片段**：本章 `loop.py` 的 `_append` 方法、字符串 reason、扁平 `assistant/message` 形态均已过时；实现是 ContentBlock 消息对象 + 显式编号 + `request/header` 事件 + 每 chunk 落 `assistant/chunk`（`core/agent_loop/agent.py`）。
    - **重试接线**：真实调用入口必须挂载 `apply_retry_planner`（`llm/retry.py:196`），否则 `agent/request-error` 瀑布不生效（本章 §4.11 真实 API 示例未调用，已修正纪律见 AGENTS.md）。
    - **时序图**：完整时序含 `request/header` 事件与逐 chunk `assistant/chunk` 落盘（见 `core/agent_loop/agent.py` 的 requestHeaderLogged 语义）。

## 4.1 这一章要做什么

前三章分别备好了日志、插件骨架和工具管线，这一章把它们接起来：模型在循环里转起来，一个回合完整跑完。两个部分：

1. `llm.py`——统一流协议 `StreamChunk` + `LlmAdapter` 接口 + DeepSeek 官方 SSE 适配器（纯 stdlib，不装 SDK）
2. `loop.py`——turn/step 状态机：inbox 排队、pre-step 拒绝、工具回灌继续、turn 括号闭合

用 `FakeLlmAdapter` 不需要 API key 就能跑通"文本 + 工具调用"的完整回合——测试和演示都靠它。

## 4.2 概念：turn/step 时序

```mermaid
sequenceDiagram
  participant U as 用户
  participant A as Agent
  participant D as Driver
  participant S as Session 日志
  participant L as LLM 扩展口
  participant T as tools
  U->>A: followup(content)
  A->>S: turn/start [durable]
  D->>D: claim 输入
  D->>D: pre-step (waterfall)
  alt 拒绝
    D->>S: turn/end（零 step）
  else 进入
    D->>S: step/start → user/message [durable]
    D->>L: request → stream
    L-->>D: StreamChunk*
    D->>S: assistant/message [durable]
    D->>T: tool/call → 管线 → tool/result [durable]
    D->>S: step/end
    alt 还有工具请求
      D->>D: 同 turn 内下一步（回灌结果继续问模型）
    else 无未偿之责
      D->>S: turn/end [durable]
    end
  end
```

三个要点：

1. **turn 打开于认领输入之前**。"被拒绝的尝试"也留下 `turn/start + turn/end` 的持久化记录——审计要看到"发生过一次尝试"，即使它什么都没做。
2. **step = 一次模型请求 + 它调用的工具**。工具结果回灌后，同一 turn 内自动再问一次模型（`_continue`）。所以"一次对话回合"可能包含多次模型请求，这是 agent 循环和普通聊天 API 的本质区别。
3. **模型可见 ⟺ 已记录**（第 1 章那句话在这里落地）：`user/message` 在 pre-step 通过后才 append，模型永远看不到没进日志的输入。

## 4.3 代码 step-by-step（llm.py）

### 步骤 1：StreamChunk 统一协议

```python
STREAM_CHUNK_KINDS = frozenset({
    "block-start", "text-delta", "reasoning-delta",
    "tool-call-delta", "block-end", "usage", "finish",
})

class StreamChunk(dict):
    def __init__(self, kind, **payload):
        if kind not in STREAM_CHUNK_KINDS:
            raise ValueError(f"未知 chunk kind: {kind}")
        super().__init__({"kind": kind, **payload})
```

为什么要统一协议？因为不同模型厂商的流式格式各不相同（OpenAI 系、Anthropic 系、原生 SSE……），loop 不该关心厂商差异。所有适配器都吐同一种 `StreamChunk`，loop 只认这一种。

协议硬性规定（真实 dsh 逐条遵守，`docs/subsystems/llm-streaming.md` 有完整规范）：

- `block-end` 携带完整块；`usage` 必须在 `finish` 之前；`finish` 之后不再有值
- 块索引关联交错增量：多块并行时用 `index` 区分

### 步骤 2：接口 + 错误收口

```python
class LlmFailure(Exception):
    """统一错误收口：授权 / 请求 / 上下文溢出。"""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

class LlmAdapter:
    """Service Definition：Consumer（agent-loop）只依赖这个协议。"""
    provider = "base"
    def stream(self, messages, tools):
        raise NotImplementedError
```

常规做法的错误处理是"哪个 SDK 抛什么就 catch 什么"，不同厂商的报错对象还不一样。dsh 统一为 `LlmFailure(code, message)`：授权失败 `AUTH_ERROR`、网络/HTTP `REQUEST_ERROR`、上下文溢出 `CONTEXT_WINDOW_EXCEEDED`，全部一个异常类型。

> 真实 dsh 还有 `EMPTY_RESPONSE` 编码（空响应 = 可重试的规范错误）。简化版只覆盖前两个：溢出以 docstring 说明，空响应未实现——见 4.8 差异表。

### 步骤 3：FakeLlmAdapter —— 无 key 也能跑回合

```python
class FakeLlmAdapter(LlmAdapter):
    provider = "fake"

    def __init__(self, tool_call=None, final_text="任务完成。"):
        self._tool = tool_call
        self._text = final_text
        self.calls = 0

    def stream(self, messages, tools):
        self.calls += 1
        if self._tool and self.calls == 1:
            arguments = self._tool.get("arguments", {})
            arguments_text = json.dumps(arguments, ensure_ascii=False)
            yield StreamChunk("block-start", index=0, blockType="tool-call")
            yield StreamChunk("tool-call-delta", index=0, id="call_0",
                              name=self._tool["name"], argumentsDelta=arguments_text)
            yield StreamChunk("block-end", index=0, block={
                "type": "tool-call", "id": "call_0", "name": self._tool["name"],
                "arguments": arguments_text,
            })
            yield StreamChunk("finish", reason="tool_calls")
        else:
            yield StreamChunk("block-start", index=0, blockType="text")
            yield StreamChunk("text-delta", index=0, text=self._text)
            yield StreamChunk("block-end", index=0, block={
                "type": "text", "text": self._text,
            })
            yield StreamChunk("finish", reason="stop")
```

行为规则：第一次调用返回一次工具调用，之后返回最终文本。这样就能驱动"模型调工具 → 拿到结果 → 再回答"的完整回合，全程不需要真实模型。注意 `tool-call` 块的 `arguments` 是 **JSON 字符串**，与上游 `ContentBlock` 一致——模型侧协议里参数就是字符串，不是对象。

### 步骤 4：DeepSeekAdapter —— 官方 SSE（stdlib urllib）

```python
class DeepSeekAdapter(LlmAdapter):
    provider = "deepseek-official"

    def __init__(self, api_key=None, base_url=None, model="deepseek-chat"):
        self._key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
        self._base = (base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self._model = model

    def stream(self, messages, tools):
        body = {"model": self._model, "messages": messages, "stream": True}
        if tools:
            body["tools"] = [t["schema"] for t in tools]
        req = urllib.request.Request(self._base + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._key})
        try:
            resp = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            detail = e.read(200).decode("utf-8", "replace")
            code = "AUTH_ERROR" if e.code in (401, 403) else "REQUEST_ERROR"
            raise LlmFailure(code, f"HTTP {e.code}: {detail}") from e

        # tool-call 的 name / arguments 分片到达，需先收集；随后按 ContentBlock
        # 重放为 block-start → delta* → block-end（Consumer 做流式 UI 可改为逐片转发）
        texts, reasonings, pending = {}, {}, {}
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            piece = json.loads(data)
            for choice in piece.get("choices", []):
                delta = choice.get("delta", {})
                if delta.get("reasoning_content"):
                    reasonings[choice["index"]] = reasonings.get(choice["index"], "") + delta["reasoning_content"]
                if delta.get("content"):
                    texts[choice["index"]] = texts.get(choice["index"], "") + delta["content"]
                for tc in delta.get("tool_calls") or []:
                    slot = pending.setdefault(tc["index"], {"id": "", "name": "", "arguments": ""})
                    fn = tc.get("function", {})
                    slot["id"] = tc.get("id") or slot["id"]
                    slot["name"] += fn.get("name", "")
                    slot["arguments"] += fn.get("arguments", "")

        for idx in sorted(texts):
            yield StreamChunk("block-start", index=idx, blockType="text")
            yield StreamChunk("text-delta", index=idx, text=texts[idx])
            yield StreamChunk("block-end", index=idx, block={"type": "text", "text": texts[idx]})
        for idx in sorted(reasonings):
            yield StreamChunk("block-start", index=idx, blockType="reasoning")
            yield StreamChunk("reasoning-delta", index=idx, text=reasonings[idx])
            yield StreamChunk("block-end", index=idx, block={"type": "reasoning", "text": reasonings[idx]})
        if pending:
            for idx, slot in sorted(pending.items()):
                call_id = slot["id"] or f"call_{idx}"
                yield StreamChunk("block-start", index=idx, blockType="tool-call")
                yield StreamChunk("tool-call-delta", index=idx, id=call_id,
                                  name=slot["name"], argumentsDelta=slot["arguments"])
                yield StreamChunk("block-end", index=idx, block={
                    "type": "tool-call", "id": call_id, "name": slot["name"], "arguments": slot["arguments"],
                })
        yield StreamChunk("finish", reason="tool_calls" if pending else "stop")
```

核心逻辑在 SSE 循环里：`reasoning_content`、`content`、`tool_calls` 三类增量各自累积，其中 tool-call 的 name 和 arguments 是**分片到达**的，必须攒齐。攒齐之后按 ContentBlock 重放为 `block-start → delta* → block-end`——这样 Consumer（loop）看到的永远是完整的块结构，而不是碎片。

两个细节：

- `baseURL / apiKey` 从环境变量读取，代码里只存引用。凭据的完整处理在第 6 章（凭据扩展口）。
- 401/403 映射为 `AUTH_ERROR`，其余 HTTP 错误映射为 `REQUEST_ERROR`——错误在源头就分类，调用方不用猜。

## 4.4 代码 step-by-step（loop.py）

### 步骤 1：状态与入口

```python
class AgentLoop:
    def __init__(self, session, adapter, tools, ctx, system_prompt="你是一个助手。", max_steps=50):
        self.session = session
        self.adapter = adapter
        self.tools = tools
        self.ctx = ctx
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.status = "idle"
        self.inbox = deque()        # 排队输入（唯一入口）
        self._turn_open = False
        self._continue = False      # 工具回灌后是否继续

    def followup(self, content, source="user"):
        """用户输入：先进 inbox，pre-step 通过后才 append 进日志。"""
        self.inbox.append({"role": "user", "content": content, "source": source})
        self._pump()

    def run(self, content):
        self.followup(content)
        return self.last_response()
```

`inbox` 是唯一入口：用户的输入先进队列，由 `_pump` 消费。为什么排队而不是直接处理？因为一次 `followup` 可能触发多轮工具调用，期间再来新输入必须排队，不能打断当前回合。

### 步骤 2：turn 生命周期

```python
    def _open_turn(self):
        if self._turn_open:
            return
        self.status = "running"
        self.session.append({"type": "turn/start"})
        self._turn_open = True

    def _close_turn(self, reason="completed"):
        if not self._turn_open:
            return
        self.session.append({"type": "turn/end", "reason": reason})
        self._turn_open = False
        self.status = "idle"
```

`turn/start` / `turn/end` 是第 1 章的括号。`reason` 默认为 `"completed"`，被拒绝的尝试、被打断的回合会写别的值。

### 步骤 3：主循环

```python
    def _pump(self):
        steps = 0
        while self.inbox or self._continue:
            steps += 1
            if steps > self.max_steps:
                raise RuntimeError(f"超过最大 step 数 {self.max_steps}，疑似死循环")
            self._open_turn()
            claimed = self.inbox.popleft() if self.inbox else None
            self._run_step(claimed)
            if not self.inbox and not self._continue:
                self._close_turn()
```

循环条件：有排队输入，或有工具回灌待继续（`_continue`）。`max_steps` 是死循环守卫：模型如果永远调工具不结束，会在 50 步时报错而不是挂死（测试 `test_max_steps_guard` 钉住）。

### 步骤 4：一个 step

```python
    def _run_step(self, claimed):
        if claimed is not None:
            decision = self.ctx.waterfall("agent/pre-step", {"messages": [claimed]})
            if isinstance(decision, dict) and decision.get("verdict") == "reject":
                return   # 零 step 尝试：turn 照常闭合
            self._step += 1
            self._append("step/start")
            self._append("user/message", content=claimed["content"],
                         surfaceOp="append", source=claimed.get("source", "user"))
        else:
            self._step += 1
            self._append("step/start")   # 工具回灌后的继续

        history = derive_messages(self.session.events)
        messages = [{"role": "system", "content": self.system_prompt}] + history
        chunks = list(self.adapter.stream(messages, self._tool_definitions()))
        text = "".join(c.get("text", "") for c in chunks if c["kind"] == "text-delta")

        # tool-call-delta 是增量分片：按 (id) 累积 name 与 argumentsDelta
        pending_calls = {}
        for c in chunks:
            if c["kind"] != "tool-call-delta":
                continue
            key = c.get("id") or str(c.get("index"))
            slot = pending_calls.setdefault(key, {"name": "", "argumentsDelta": ""})
            slot["name"] += c.get("name", "")
            slot["argumentsDelta"] += c.get("argumentsDelta", "")
        tool_calls = [{"name": s["name"], "arguments": s["argumentsDelta"]} for s in pending_calls.values()]

        self._append("assistant/message", content=text, surfaceOp="append", toolCalls=tool_calls)
        for call in tool_calls:
            self._run_tool(call["name"], call["arguments"])
        self._append("step/end")
        self._continue = bool(tool_calls)
```

四个要点：

- `_append` 是回合事件的统一入口，自动注入 `turn` / `step` 编号——与上游一致，**从 1 起**（`session/invariant.ts` `nextTurn: 1, nextStep: 1`，每 turn 内 step 重置为 1）。这是对齐后与上游完全一致的字段。
- **pre-step 拒绝**：waterfall 返回 `{"verdict": "reject"}` → 不落 `step/start`，turn 直接闭合。这就是"零 step turn"：被拒绝的尝试也留下括号痕迹（4.2 要点 1）。
- 历史 = `derive_messages(日志)` + system prompt，绝不另存——第 1 章的投影在这里消费。
- `tool-call-delta` 是增量分片，loop 按 id 累积 name 与 `argumentsDelta`，组装成完整的 `toolCalls` 再落日志。所以日志里存的是完整参数（JSON 字符串），而不是碎片。
- `_continue = bool(tool_calls)`：有工具调用 → 同 turn 内再问模型。第二次 adapter 调用时，消息历史里已经多了 `tool/result`。

### 步骤 5：工具执行与落日志

```python
    def _run_tool(self, name, arguments):
        tool = self.tools.resolve(name)
        if tool is None:
            result = ToolResult(ok=False, is_error=True, error=f"未知工具: {name}")
        else:
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            self.session.append({"type": "tool/call", "name": name, "arguments": arguments})  # 执行前先记录
            result = run_pipeline(self.ctx, tool, arguments)
        self.session.append({"type": "tool/result", "name": name, "content": result.content,
                             "isError": result.is_error, "error": result.error, "surfaceOp": "append"})
```

`tool/call` 在**执行前**落日志（durable），`tool/result` 是唯一模型面向的结果——和第 3 章管线无缝对接。注意"未知工具"也被规范化成 `ToolResult(is_error=True)`：模型收到错误消息并自己决定怎么办，而不是整个回合崩溃。

## 4.5 验收：硬性规定 + 测试

`tests/test_loop.py` 钉住的规定：

1. `turn/start` 与 `turn/end` 成对且 `turn_balance == 0`
2. 拒绝的尝试：有 `turn/start + turn/end`，无 `step/start`
3. 工具调用回合：`tool/call → tool/result` 相邻且都 durable；模型在同 turn 内被请求 ≥2 次
4. 历史永远从日志派生；`max_steps` 防死循环
5. StreamChunk 协议：`finish` 是最后一个 chunk

```bash
python -m unittest tests.test_loop -v
```

## 4.6 用真实 API 跑一次（可选）

```python
from miniharness import Context, Session, ToolRegistry, Tool, AgentLoop, DeepSeekAdapter

session = Session("real-001")
ctx = Context()
reg = ToolRegistry(ctx)
reg.register(Tool(name="bash", description="Run a shell command.",
                  parameters={"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
                  execute=lambda args, e: f"stdout: {args['cmd']}"))
loop = AgentLoop(session, DeepSeekAdapter(model="deepseek-chat"), reg, ctx)
print(loop.run("用 bash 执行 echo hello，然后告诉我结果。"))
print([e["type"] for e in session.events])
```

需要环境变量 `DEEPSEEK_API_KEY`（可选 `DEEPSEEK_BASE_URL` 指向兼容代理）。跑完后 `session.events` 里能看到完整回合：turn/step 括号、消息、工具调用与结果，全都在。

## 4.7 检查点练习

1. **加拒绝理由**：让 pre-step 的 reject 带 `reason`，reject 时把它写进 `turn/end` 的 `reason` 字段，并断言日志可审计。
2. **chunk 落盘**：把 `assistant/chunk` 逐条 append（当前简化只落合并消息），再验证 `derive_messages` 不受 chunk 影响。
3. **并发工具**：给 `_run_tool` 加 `is_concurrency_safe` 并行执行（线程），非安全工具串行——跑通测试。

## 4.8 回到 dsh：真实源码对照

打开 `deepseek-harness/packages/core/agent-loop/src`：

- 主 Driver 的 `_run_step` 对应我们的 `_run_step`——真实实现更复杂（交错工具批、屏障、回灌顺序）
- `docs/agent-lifecycle.md` 顶部的 Mermaid 时序图：与我们 4.2 的图逐条对应

下面这些真实扩展点简化版没有实现，对照时不要找"上游为什么多这些东西"，它们是刻意省略的：

| 上游事件/扩展点 | 用途 | mini 对应 |
|---|---|---|
| `system-prompt/assemble` waterfall | 提示词按片段组装（hook 可注入上下文） | 直接拼接，无扩展点 |
| `agent/request` waterfall → `llm/stream` | 请求构造拦截（steering） | 未实现 waterfall，直连 adapter |
| `agent/request-error` waterfall | 规范错误（如上下文溢出）后的重试决策 | 已实现（§4.10 重试/退避） |
| `agent/turn-stopping` serial | turn 结束前串行终点检查 | 未实现（mini 的压力检查在 `agent/pre-step`，即 §4.10 压缩接线；turn-stopping 扩展点本身未复现） |
| `finish {kind:'error'\|'aborted'}` 带内失败 | 流中途失败也可经协议传递 | 只在 `stream()` 抛 `LlmFailure` |
| `EMPTY_RESPONSE` 编码 | 空响应 = 规范错误，可重试 | 已实现且默认可重试（§4.10） |

## 4.10 重试/退避与上下文溢出降级

对应 dsh：`packages/llm/llm/src/retry-policy.ts` + `packages/llm/llm-retry/src/index.ts` + `packages/core/agent/src/runtime-types.ts`（`agent/request-error`）。

**扩展点**：loop 在适配器抛 `LlmFailure` 时派发 `agent/request-error` waterfall，
payload `{agent, turn, step, provider, failure, retryPolicy, signal}`（与上游逐字段
一致）。监听器返回 `{kind:'retry'}` 且不调 `next()` = 自己接管恢复；调 `next()` 委派；
默认 `undefined` 失败终局。重试规划器由**装配方显式挂载**（`AgentLoop` 构造无副作用，
对齐上游插件 apply 时挂载）：headless / sessions / acp / sdk / demo / 示例在构造
loop 前调用 `apply_retry_planner(ctx)`（幂等，可重复调用）。

**策略解析**（`llm/retry_policy.py`，对齐 retry-policy.ts）：

- 两种模式：`normal`（`maxRetries` + `retryableCodes` 白名单）/ `always`（无限重试）
- 默认：`maxRetries 2`、`initialDelayMs 500`、`maxDelayMs 10000`、`jitterRatio 0.1`、
  可重试码 `[EMPTY_RESPONSE, RATE_LIMIT, SERVER, TIMEOUT, TRANSPORT]`
- 严格校验：未知键拒绝、backoff 正有限且 `initial ≤ max`、jitter ∈ [0,1]、
  `maxRetries` 非负整数、codes 非空无重复；解析结果冻结，provider 注册时捕获

**恢复决策**（`llm/retry.py`，对齐 llm-retry/index.ts）：

1. 策略 `undefined` → 直接委派（不重试）
2. `always`：先委派下游——下游给出 retry 决策即采用；失败/未接管则自己无限重试（不判 code）
3. `normal`：code 不在白名单 → 委派；同 turn/step/provider/policyKey 的 `llm/retry`
   计数 ≥ `maxRetries` → 委派（放弃）
4. 每次重试前先落 `llm/retry`（durable，含策略细节/`retryId`/序数/`delayMs`/failure 快照），
   可取消等待结束后落 `llm/retry-started` 并返回 `{kind:'retry'}`；`retryId` 同一对全程复用
5. 延迟决议：`providerRetryAfterMs`（429 的 `Retry-After`，纯数字秒 ×1000 或 HTTP-date）
   有效时优先——超过 `maxDelayMs` 则 normal 放弃 / always 改用本地延迟；否则本地退避
   `min(initial × 2^min(retry-1, 1024), max) × (1 - ratio + 2×ratio×rand)` 再封顶 `maxDelayMs`
6. 可取消：等待以分片 sleep 轮询 `signal.aborted`（mini 同步简化，无真实 AbortSignal；
   loop 的 `_AbortProxy` 反映取消标记）；normal 分支不在派发前检查 abort——
   `llm/retry` 仍落、等待立即放弃、不落 started（与上游一致）

**接线语义**：重试是同 step 内重新发起模型请求——`messages` 不变（失败 attempt
不产生任何消息事件，`derive_messages` 不受 `llm/retry` 影响）、`request/header`
只落一次（上游仅在 header 变化时追加）、`assistant/message` 的 `sourceEventSeqs`
只含成功 attempt 的 chunk。`LlmFailure` 扩展 `status` / `providerRetryAfterMs` /
`requestId`（`x-request-id` / `x-deepseek-request-id`）可选字段；socket 超时映射
`TIMEOUT`（原本混在 `TRANSPORT` 里）。

**上下文溢出降级**：`CONTEXT_WINDOW_EXCEEDED`（400 上下文超限）不在默认白名单
→ 重试规划器不接管，委派下游。装配方在 `apply_retry_planner(ctx)` 之后挂载
`install_compaction(ctx)`（幂等）：压缩引擎监听 `agent/request-error`，对
`CONTEXT_WINDOW_EXCEEDED` 强制减容（见 `miniharness/compaction/` 与报告 04 §9.4），且**仅当** surface
`replaceGeneration` 前进（检查点真实落盘）才返回 `{kind:'retry'}`，计数上限
`maxOverflowRetries`，成功响应/回合结束边界复位。既无压缩也无接管 → 终局
`turn/end` reason 为 `{kind:'error'}`。

验证：`python -m unittest tests.test_retry -v`（36 项：策略解析、退避边界、
Retry-After 解析、全部 recover 分支、loop 集成——重试成功/耗尽终局/非白名单终局）；
压缩/溢出见 `tests/test_compaction.py`。

## 4.9 收尾

回合跑通的那一刻，前三章的积木全部就位：日志在写、插件在拦、工具在跑、模型在转。这一章最后要记住的是 turn/step 的分层——turn 是对话的括号，step 是括号里的每一轮"请求 + 工具"。下一章处理一个没解决的实际问题：这些日志怎么落盘、崩溃怎么恢复、整个系统怎么组合启动。