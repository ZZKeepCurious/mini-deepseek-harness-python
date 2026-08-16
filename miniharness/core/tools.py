"""第 3 章：工具注册表 + 执行管线。

对应 dsh 真实源码：packages/core/tools —— 作用域化注册表 +
pre-execute / execute / post-execute 三段 waterfall 管线。

管线不变量：
  1. 参数在策略前一次性无损物化 + 深度冻结
  2. 守卫只能减权（单调），deny 后工具体被跳过
  3. 任何异常都规范化为结构化 ToolResult(isError=True)，不中断回合
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable

from .scope import Context
from .session import deep_freeze, is_json_safe


# ---------- JSON Schema 子集校验器 ----------

def validate_schema(value: Any, schema: dict) -> list[str]:
    """校验 value 是否符合 schema（type / properties / required / items / enum）。"""
    errors: list[str] = []
    _check(value, schema, "$", errors)
    return errors


def _check(value: Any, schema: dict, path: str, errors: list[str]) -> None:
    if schema is None:
        return
    typ = schema.get("type")
    if typ == "object":
        if not isinstance(value, (dict, MappingProxyType)):
            errors.append(f"{path}: 期望 object，得到 {type(value).__name__}")
            return
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}.{req}: 缺少必填字段")
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                _check(value[k], sub, f"{path}.{k}", errors)
    elif typ == "array":
        if not isinstance(value, list):
            errors.append(f"{path}: 期望 array")
            return
        for i, item in enumerate(value):
            _check(item, schema.get("items", {}), f"{path}[{i}]", errors)
    elif typ == "string":
        if not isinstance(value, str):
            errors.append(f"{path}: 期望 string")
    elif typ == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{path}: 期望 number")
    elif typ == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{path}: 期望 integer")
    elif typ == "boolean":
        if not isinstance(value, bool):
            errors.append(f"{path}: 期望 boolean")
    elif typ == "null":
        if value is not None:
            errors.append(f"{path}: 期望 null")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: 值 {value!r} 不在枚举 {schema['enum']} 中")


# ---------- 领域对象 ----------

@dataclass
class ToolExec:
    """执行上下文：signal 是唯一可替换的字段（用于超时/取消）。

    agent 由 AgentLoop/scheduler 在派发时填入（上游 ToolExecution.agent），
    供作业等按调用者会话栅栏的工具使用；缺省 None = 无 agent 调用方。
    """
    signal: threading.Event = field(default_factory=threading.Event)
    agent: Any = None


@dataclass
class Tool:
    name: str
    description: str
    execute: Callable[[dict, ToolExec], Any]
    parameters: dict = field(default_factory=dict)      # JSON Schema
    output: dict = field(default_factory=dict)          # canonical schema
    is_concurrency_safe: Callable[[dict], bool] | bool = False  # True/恒真 → 可并行
    timeout_ms: int | None = None                       # 由管线 wrapper 强制
    present_call: Callable | None = None                # UI 挂起卡片（纯函数）
    present_result: Callable | None = None              # UI 完成卡片（纯函数）


@dataclass(frozen=True)
class ToolResult:
    """冻结的权威结果：ok / content 是执行局部的，isError 是规范化的。

    error_info：可选 {name, code}，仅当存在时写入 tool/result 的 error
    字段（对齐上游 llm/src/types.ts:295 —— error.info 存在才携带）。
    """
    ok: bool
    content: Any = None
    is_error: bool = False
    error: str | None = None
    meta: dict = field(default_factory=dict)
    _aborted: bool = field(default=False, repr=False, compare=False)
    error_info: dict | None = field(default=None, repr=False, compare=False)


# ---------- 作用域化注册表 ----------

class ToolRegistry:
    """可见性解析：自身注册 → 祖先作用域链 → 全局层。"""

    def __init__(self, root: Context):
        self.root = root
        self._tools: dict[str, Tool] = {}
        root.provide("tools", self)          # ctx.tools 服务

    def register(self, tool: Tool, scope: Context | None = None) -> Callable:
        bucket = self._bucket(scope)
        if tool.name in bucket:
            raise RuntimeError(f"工具 {tool.name} 已注册")
        bucket[tool.name] = tool
        return lambda: bucket.pop(tool.name, None)

    def _bucket(self, scope: Context | None) -> dict:
        if scope is None:
            return self._tools
        if not hasattr(scope, "_scoped_tools"):
            scope._scoped_tools = {}
        return scope._scoped_tools

    def resolve(self, name: str, scope: Context | None = None) -> Tool | None:
        node = scope
        while node is not None:
            bucket = getattr(node, "_scoped_tools", {})
            if name in bucket:
                return bucket[name]
            node = node.parent
        return self._tools.get(name)

    def names(self, scope: Context | None = None) -> list[str]:
        node = scope
        names: list[str] = []
        while node is not None:
            names.extend(getattr(node, "_scoped_tools", {}))
            node = node.parent
        names.extend(self._tools)
        return sorted(set(names))

    def restrict(self, allow: set[str] | None = None, deny: set[str] | None = None) -> Callable[[str], bool]:
        """ToolRestriction：deny 优先，其次 allow 白名单（继承过滤）。"""

        def allowed(name: str) -> bool:
            if deny and name in deny:
                return False
            if allow is not None and name not in allow:
                return False
            return True

        return allowed


# ---------- 执行模式分类（阶段 7，对齐上游 executionMode） ----------

def execution_mode(tool: Tool | None, args: dict) -> str:
    """调度模式：'parallel' | 'exclusive'。

    与上游 tools/src/index.ts executionMode 逐条一致：只有 `isConcurrencySafe`
    声明的精确 `True`（bool True 或 callable 返回 True）才 parallel；
    未声明、False、callable 抛错或返回非布尔 → exclusive（fail 到独占）。
    """
    if tool is None:
        return "exclusive"
    declared = tool.is_concurrency_safe
    if isinstance(declared, bool):
        return "parallel" if declared else "exclusive"
    if callable(declared):
        try:
            safe = declared(dict(args))
        except Exception:
            return "exclusive"
        return "parallel" if safe is True else "exclusive"
    return "exclusive"


# ---------- 执行管线 ----------

def pipeline_policy(
    ctx: Context, tool: Tool, frozen_args: Any,
) -> ToolResult | None:
    """政策段（同步版）：pre-execute waterfall / ask / guards。返回拒绝结果或 None。"""
    schema_errors = validate_schema(frozen_args, tool.parameters)
    if schema_errors:
        return ToolResult(ok=False, is_error=True, error="; ".join(schema_errors))

    decision = ctx.waterfall("tools/pre-execute", {"tool": tool.name, "args": frozen_args})
    # 对齐上游 PreToolDecision：{kind:'allow'} / {kind:'deny', reason} / {kind:'ask', reason?}
    # （tools/src/index.ts:588-591；hooks 插件产出的正是 kind 形状）
    verdict = decision.get("kind", "allow") if isinstance(decision, dict) else "allow"
    if verdict == "deny":
        return ToolResult(ok=False, is_error=True, error="denied by tools/pre-execute")
    if verdict == "ask":
        approved = ctx.waterfall("tools/ask", {"tool": tool.name, "args": frozen_args})
        if approved is not True:
            return ToolResult(ok=False, is_error=True, error="approval refused")

    guard = ctx.waterfall("tools/guards", {"tool": tool.name, "args": frozen_args})
    guard_verdict = guard.get("kind", "allow") if isinstance(guard, dict) else "allow"
    if guard_verdict == "deny":
        return ToolResult(ok=False, is_error=True, error="denied by monotonic guard")
    return None


async def pipeline_policy_async(
    ctx: Context, tool: Tool, frozen_args: Any,
) -> ToolResult | None:
    """政策段（async 版）：同一语义，waterfall 走 awaterfall。
    供调度器在事件循环按模型序有序 await（上游 prepare 的 pre-execute 有序）。"""
    schema_errors = validate_schema(frozen_args, tool.parameters)
    if schema_errors:
        return ToolResult(ok=False, is_error=True, error="; ".join(schema_errors))

    decision = await ctx.awaterfall("tools/pre-execute", {"tool": tool.name, "args": frozen_args})
    # 对齐上游 PreToolDecision：{kind:'allow'} / {kind:'deny', reason} / {kind:'ask', reason?}
    verdict = decision.get("kind", "allow") if isinstance(decision, dict) else "allow"
    if verdict == "deny":
        return ToolResult(ok=False, is_error=True, error="denied by tools/pre-execute")
    if verdict == "ask":
        approved = await ctx.awaterfall("tools/ask", {"tool": tool.name, "args": frozen_args})
        if approved is not True:
            return ToolResult(ok=False, is_error=True, error="approval refused")

    guard = await ctx.awaterfall("tools/guards", {"tool": tool.name, "args": frozen_args})
    guard_verdict = guard.get("kind", "allow") if isinstance(guard, dict) else "allow"
    if guard_verdict == "deny":
        return ToolResult(ok=False, is_error=True, error="denied by monotonic guard")
    return None


def pipeline_body(
    ctx: Context, tool: Tool, frozen_args: Any, exec_: ToolExec, *,
    async_: bool = False,
) -> tuple[Any, Exception | None]:
    """执行体段：execute（可选线程超时）+ post-execute。返回 (raw, error)。"""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = tool.execute(dict(frozen_args), exec_)
        except Exception as e:
            box["error"] = e

    if tool.timeout_ms:
        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(tool.timeout_ms / 1000)
        if t.is_alive():
            exec_.signal.set()
            t.join()   # 排干：等待执行体到达静止点
            return None, TimeoutError(f"timeout after {tool.timeout_ms}ms")
    else:
        target()

    raw = box.get("value")
    post = ctx.waterfall("tools/post-execute", {"tool": tool.name, "result": raw})
    if isinstance(post, dict) and post.get("action") == "block":
        # 对齐上游：block decision 的 feedback 是 ContentBlock[]（text 块）
        return None, RuntimeError(_feedback_text(post.get("feedback", "blocked by tools/post-execute")))
    return raw, box.get("error")


def _feedback_text(feedback: Any) -> str:
    """post_tool block feedback（ContentBlock[] 或字符串）→ 错误文本。"""
    if isinstance(feedback, (list, tuple)):
        return "".join(b.get("text", "") for b in feedback if isinstance(b, dict) and b.get("type") == "text") or str(feedback)
    return str(feedback)


def run_pipeline(ctx: Context, tool: Tool, args: dict, exec_: ToolExec | None = None) -> ToolResult:
    """pre-execute → 守卫 → execute → post-execute → 规范化 → 冻结结果。"""
    exec_ = exec_ or ToolExec()

    # 1. 参数一次性无损物化 + 深度冻结
    try:
        frozen_args = deep_freeze(dict(args))
    except Exception as e:
        return ToolResult(ok=False, is_error=True, error=f"参数无法物化: {e}")

    # 2-4. 政策段
    rejected = pipeline_policy(ctx, tool, frozen_args)
    if rejected is not None:
        return rejected

    # 5-6. 执行体段
    raw, error = pipeline_body(ctx, tool, frozen_args, exec_)

    # 7. 外层规范化：异常 / 非法值 → isError
    if error is not None:
        e = error
        return ToolResult(ok=False, is_error=True, error=f"{type(e).__name__}: {e}")
    if isinstance(raw, ToolResult):
        return raw
    if isinstance(raw, dict) and raw.get("isError"):
        return ToolResult(ok=False, content=raw.get("content"), is_error=True, error=raw.get("error"))
    if not is_json_safe(raw):
        return ToolResult(ok=False, is_error=True, error="工具返回了不可 JSON 序列化的值")

    # 8. 冻结的权威结果
    return ToolResult(ok=True, content=deep_freeze(raw))


async def pipeline_async_body(
    ctx: Context, tool: Tool, frozen_args: Any, exec_: ToolExec | None = None,
) -> ToolResult:
    """政策通过后的执行体段（async 版）：execute 在线程池，post-execute
    回事件循环（awaterfall），超时 wait_for + 置位 signal + shield 排干。
    供调度器复用——政策段已按模型序有序跑过，此处只做 body。"""
    exec_ = exec_ or ToolExec()
    loop = asyncio.get_running_loop()

    def body() -> tuple[Any, Exception | None]:
        try:
            raw = tool.execute(dict(frozen_args), exec_)
        except Exception as e:
            return None, e
        return raw, None

    if tool.timeout_ms:
        try:
            raw, error = await asyncio.wait_for(
                loop.run_in_executor(None, body), tool.timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            exec_.signal.set()
            # 排干：线程无法取消，必须等它到达静止点（上游排干语义）
            raw, error = await asyncio.shield(loop.run_in_executor(None, body))
            if error is None:
                error = TimeoutError(f"timeout after {tool.timeout_ms}ms")
    else:
        raw, error = await loop.run_in_executor(None, body)

    # post-execute 回事件循环（与上游 finalize 在事件循环跑一致）
    post = await ctx.awaterfall("tools/post-execute", {"tool": tool.name, "result": raw})
    if isinstance(post, dict) and post.get("action") == "block":
        # 对齐上游：block decision 的 feedback 是 ContentBlock[]（text 块）
        error = RuntimeError(_feedback_text(post.get("feedback", "blocked by tools/post-execute")))

    if error is not None:
        e = error
        return ToolResult(ok=False, is_error=True, error=f"{type(e).__name__}: {e}")
    if isinstance(raw, ToolResult):
        return raw
    if isinstance(raw, dict) and raw.get("isError"):
        return ToolResult(ok=False, content=raw.get("content"), is_error=True, error=raw.get("error"))
    if not is_json_safe(raw):
        return ToolResult(ok=False, is_error=True, error="工具返回了不可 JSON 序列化的值")
    return ToolResult(ok=True, content=deep_freeze(raw))


async def run_pipeline_async(
    ctx: Context, tool: Tool, args: dict, exec_: ToolExec | None = None,
) -> ToolResult:
    """管线异步版（阶段 7）：政策段在事件循环按序 await（awaterfall），
    执行体在线程池真并行；超时用 wait_for + 置位 signal + 排干，
    与上游"已启动的 promise 必须排干到静止"一致。"""
    exec_ = exec_ or ToolExec()

    try:
        frozen_args = deep_freeze(dict(args))
    except Exception as e:
        return ToolResult(ok=False, is_error=True, error=f"参数无法物化: {e}")

    rejected = await pipeline_policy_async(ctx, tool, frozen_args)
    if rejected is not None:
        return rejected
    return await pipeline_async_body(ctx, tool, frozen_args, exec_)