import asyncio
import os
import re
from typing import Any

JS_ENV_EXPR = re.compile(r"^process\.env\.([A-Za-z_][A-Za-z0-9_]*)$")


def is_js_expr(value: Any) -> bool:
    """是否 !!js 惰性节点（对齐 config/utils.ts isJsExpr：`'__jsExpr' in value`，
    不要求单键）。"""
    return isinstance(value, dict) and "__jsExpr" in value


def base_url_of(ctx: Any) -> str:
    """沿 ctx 祖先链取最近设置的 baseUrl（上游 ctx.baseUrl 原型继承等价：
    ctx.extend 仅携带 meta 自有属性，baseUrl 需上溯父链读取）。"""
    node = ctx
    while node is not None:
        base = getattr(node, "baseUrl", None)
        if base:
            return base
        node = getattr(node, "parent", None)
    return ""


async def _drain(coros: list) -> list:
    """gather 放到运行中的 loop 里执行（asyncio.run(asyncio.gather(...)) 在
    Py3.10+ 会在 gather() 调用期就抛没有当前 loop）。"""
    return await asyncio.gather(*coros, return_exceptions=True)


def settle_gathered(coros: list) -> list:
    """无运行 loop 时以瞬态事件循环排空在途转换（awaitable 可能来自
    fiber.inertia / fiber.wait）。"""
    return asyncio.run(_drain(coros))


def evaluate_js_expr(expr: str, environ: dict[str, str] | None = None) -> str:
    """!!js 表达式求值：仅支持 process.env.<NAME> 完整匹配，其它 fail loud。"""
    m = JS_ENV_EXPR.match(expr.strip())
    if not m:
        raise ValueError(
            f"不支持的 !!js 表达式: {expr!r}（mini 仅支持 process.env.<NAME>）"
        )
    return (environ if environ is not None else os.environ).get(m.group(1), "")


def resolve_js_exprs(value: Any, environ: dict[str, str] | None = None) -> Any:
    """递归求值 __jsExpr 节点（读取时求值，上游为激活时 —— 简化标注）。"""
    if isinstance(value, dict):
        if is_js_expr(value):
            return evaluate_js_expr(value["__jsExpr"], environ)
        return {k: resolve_js_exprs(v, environ) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_js_exprs(v, environ) for v in value]
    return value


def evaluate(expr: str) -> str:
    """!!js 表达式激活期求值（mini 仅支持 process.env.<NAME>，见 loader/utils）。"""
    return evaluate_js_expr(expr)


def interpolate(value: Any) -> Any:
    """递归把数据里的 __jsExpr 节点换成求值结果（对齐 loader internal/config
    的 interpolate：树载体条目保持字面，普通条目激活期求值）。"""
    return resolve_js_exprs(value)


def sort_keys(options: dict, prepend: tuple = ("id", "name"), append: tuple = ("config",)) -> dict:
    """按 cosokit 排序重排条目写出键序：prepend 原始序 → 其余 locale 升序 → append 原始序。"""
    part1 = []
    for key in prepend:
        if key in options:
            part1.append((key, options.pop(key)))
    part2 = []
    for key in append:
        if key in options:
            part2.append((key, options.pop(key)))
    rest = sorted(options.items(), key=lambda kv: kv[0])
    ordered = dict([*part1, *rest, *part2])
    options.clear()
    options.update(ordered)
    return options