"""第 5 章：启动与组合 —— 配置 + 按 id 补丁 + 依赖驱动激活。

对应 dsh 真实源码：packages/boot/app-boot + 组合层（bundle / profile / patch）。

组合不变量：
  1. 补丁算法是纯函数（replace 按 id 整段替换 config / insert 新条目）
  2. 补丁按层叠顺序应用：bundle 层 → profile 级 → home 级 → --patch overlay
  3. 启动结束必须断言"条目已激活"，否则 fail loud：ACTIVE 通过、FAILED 重抛
     装载错误、PENDING 点名缺失的注入服务（对齐 assertEntriesActivated）
  4. 配置载体支持 JSON 与 YAML（pyyaml 可选依赖）；!!js 表达式求值见 composition

激活语义（对齐 vendor/cordis registry）：每个 entry 经 ctx.plugin() 铸造一
枚 fiber；entry 的 inject 依赖缺失时 fiber 保持 PENDING，提供方在 apply 期
动态 ctx.provide() 后经依赖追踪器唤醒 → LOADING → ACTIVE。同步 body 同步
激活；异步 body 由启动阶段用瞬态事件循环排空（mini 同步门面简化）。
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from typing import Any, Callable

from ..core.scope import Context, FiberState

from .composition import apply_patch, load_composition, load_patch_list, resolve_js_exprs

__all__ = ["boot", "load_plugin"]


def load_plugin(entry: dict) -> dict:
    """从 'module' 导入插件：模块内须定义 apply(ctx, **config)。

    插件形态为对象插件 {name, inject, apply}（对齐上游 registry 的插件形态）；
    apply 以 (ctx, config) 调用（对齐上游插件 body 签名）；服务在 apply 期经
    ctx.provide() 动态登记，不再使用声明式 provides 字段。
    """
    module = importlib.import_module(entry["module"])
    return {
        "name": entry.get("id", module.__name__),
        "inject": entry.get("inject") or getattr(module, "inject", None),
        "apply": lambda ctx, config, m=module: m.apply(ctx, **config),
    }


def _drain(fibers: list) -> None:
    """排空装载期在途转换（异步 body 需事件循环结算；同步 body 无 inertia）。"""
    pending = [f for f in fibers if f.inertia is not None]
    if not pending:
        return

    async def _settle() -> None:
        for fiber in pending:
            await fiber.wait()

    asyncio.run(_settle())


def _assert_entries_activated(fibers: list, entries: list, bin_name: str) -> None:
    """对齐上游 assertEntriesActivated（app-boot index.ts:692）：ACTIVE 通过、
    FAILED 重抛装载错误、PENDING 点名缺失的注入服务。"""
    failures = []
    for entry, fiber in zip(entries, fibers):
        state = fiber.state
        if state == FiberState.ACTIVE:
            continue
        if state == FiberState.FAILED and fiber._error is not None:
            raise fiber._error
        if state == FiberState.PENDING:
            missing = [n for n in fiber.inject if fiber.context.get(n) is None]
            failures.append(
                f"{entry.get('id')}: 依赖缺失 {', '.join(missing)}，未能激活")
        else:
            failures.append(f"{entry.get('id')}: fiber state {state}")
    if failures:
        raise RuntimeError(f"{bin_name}: 以下条目未激活: " + "; ".join(failures))


def boot(
    config_path: str,
    *patch_paths: str,
    env: dict[str, Any] | None = None,
    bin_name: str = "miniharness",
) -> tuple[Context, list[tuple[str, Callable]]]:
    """boot()：加载配置 → 依序应用补丁 → 动态激活插件 → 断言全部就绪。

    config_path 与补丁支持 .json/.yaml/.yml；YAML 内 !!js 表达式读取时求值。
    返回 (root, activations)：activations 为 [(entry_id, fiber.dispose)]，
    按 entry 创建序（含补丁插入序）。
    """
    env = env or {}
    entries = resolve_js_exprs(load_composition(config_path, bin_name))
    for pp in patch_paths:
        patches = resolve_js_exprs(load_patch_list(pp, bin_name, label="overlay"))
        # 目标缺失的补丁条目 → warn + 跳过该条（上游 per-entry Loader
        # warning，index.ts:309-311），boot 继续
        entries = apply_patch(
            entries, patches,
            on_missing=lambda tid: print(
                f"{bin_name}: [warn] patch 目标 id={tid} 不存在，已跳过", file=sys.stderr),
        )

    root = Context(name="root")
    for key, value in env.items():
        root.provide(key, value)

    fibers = []
    for e in entries:
        fibers.append(root.plugin(load_plugin(e), e.get("config", {})))

    _drain(fibers)
    _assert_entries_activated(fibers, entries, bin_name)
    activations = [
        (e["id"], fiber.dispose)
        for e, fiber in zip(entries, fibers)
        if fiber.state == FiberState.ACTIVE
    ]
    return root, activations