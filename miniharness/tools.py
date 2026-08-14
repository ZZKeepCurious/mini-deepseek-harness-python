"""第 3 章：工具注册表 + 执行管线。

对应 dsh 真实源码：packages/core/tools —— 作用域化注册表 +
pre-execute / execute / post-execute 三段 waterfall 管线。

管线不变量：
  1. 参数在策略前一次性无损物化 + 深度冻结
  2. 守卫只能减权（单调），deny 后工具体被跳过
  3. 任何异常都规范化为结构化 ToolResult(isError=True)，不中断回合
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable

from .bus import Context
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
    """执行上下文：signal 是唯一可替换的字段（用于超时/取消）。"""
    signal: threading.Event = field(default_factory=threading.Event)


@dataclass
class Tool:
    name: str
    description: str
    execute: Callable[[dict, ToolExec], Any]
    parameters: dict = field(default_factory=dict)      # JSON Schema
    output: dict = field(default_factory=dict)          # canonical schema
    is_concurrency_safe: bool = False                   # False = 串行屏障
    timeout_ms: int | None = None                       # 由管线 wrapper 强制
    present_call: Callable | None = None                # UI 挂起卡片（纯函数）
    present_result: Callable | None = None              # UI 完成卡片（纯函数）


@dataclass(frozen=True)
class ToolResult:
    """冻结的权威结果：ok / content 是执行局部的，isError 是规范化的。"""
    ok: bool
    content: Any = None
    is_error: bool = False
    error: str | None = None
    meta: dict = field(default_factory=dict)


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


# ---------- 执行管线 ----------

def run_pipeline(ctx: Context, tool: Tool, args: dict, exec_: ToolExec | None = None) -> ToolResult:
    """pre-execute → 守卫 → execute → post-execute → 规范化 → 冻结结果。"""
    exec_ = exec_ or ToolExec()

    # 1. 参数一次性无损物化 + 深度冻结
    try:
        frozen_args = deep_freeze(dict(args))
    except Exception as e:
        return ToolResult(ok=False, is_error=True, error=f"参数无法物化: {e}")

    # 2. schema 校验（模型给错参数 = 工具的 isError，不进回合）
    schema_errors = validate_schema(frozen_args, tool.parameters)
    if schema_errors:
        return ToolResult(ok=False, is_error=True, error="; ".join(schema_errors))

    # 3. pre-execute waterfall（hooks / 权限 / 沙箱）：allow | deny | ask
    decision = ctx.waterfall("tools/pre-execute", {"tool": tool.name, "args": frozen_args})
    verdict = decision.get("verdict", "allow") if isinstance(decision, dict) else "allow"
    if verdict == "deny":
        return ToolResult(ok=False, is_error=True, error="denied by tools/pre-execute")
    if verdict == "ask":
        approved = ctx.waterfall("tools/ask", {"tool": tool.name, "args": frozen_args})
        if approved is not True:
            return ToolResult(ok=False, is_error=True, error="approval refused")

    # 4. 单调守卫（只减权；此处复用 waterfall 通道，语义上不可加回）
    guard = ctx.waterfall("tools/guards", {"tool": tool.name, "args": frozen_args})
    guard_verdict = guard.get("verdict", "allow") if isinstance(guard, dict) else "allow"
    if guard_verdict == "deny":
        return ToolResult(ok=False, is_error=True, error="denied by monotonic guard")

    # 5. execute（超时由管线 wrapper 强制，绝不发给模型）
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
            return ToolResult(ok=False, is_error=True, error=f"timeout after {tool.timeout_ms}ms")
    else:
        target()

    # 6. post-execute waterfall：accept | block(+feedback)
    raw = box.get("value")
    post = ctx.waterfall("tools/post-execute", {"tool": tool.name, "result": raw})
    if isinstance(post, dict) and post.get("action") == "block":
        return ToolResult(ok=False, is_error=True, error=post.get("feedback", "blocked by tools/post-execute"))

    # 7. 外层规范化：异常 / 非法值 → isError
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