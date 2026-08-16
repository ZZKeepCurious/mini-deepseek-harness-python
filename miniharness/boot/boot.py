"""第 5 章：启动与组合 —— 配置 + 按 id 补丁 + 依赖驱动激活。

对应 dsh 真实源码：packages/boot/app-boot + 组合层（bundle / profile / patch）。

组合不变量：
  1. 补丁算法是纯函数（replace 按 id 整段替换 config / insert 新条目）
  2. 补丁按层叠顺序应用：bundle 层 → profile 级 → home 级 → --patch overlay
  3. 启动结束必须断言"条目已加载 + 已激活"，否则 fail loud
  4. 配置载体支持 JSON 与 YAML（pyyaml 可选依赖）；!!js 表达式求值见 composition
"""
from __future__ import annotations

import importlib
from typing import Any, Callable

from ..core.scope import Context, PluginManager
from .composition import apply_patch, load_composition, load_patch_list, resolve_js_exprs

__all__ = ["boot", "load_plugin"]


def load_plugin(entry: dict) -> dict:
    """从 'module' 导入插件：模块内须定义 apply(ctx, **config)。"""
    module = importlib.import_module(entry["module"])
    return {
        "name": entry.get("id", module.__name__),
        "inject": entry.get("inject") or getattr(module, "inject", []),
        "provides": entry.get("provides") or getattr(module, "provides", []),
        "apply": lambda ctx, m=module, c=entry.get("config", {}): m.apply(ctx, **c),
    }


def boot(
    config_path: str,
    *patch_paths: str,
    env: dict[str, Any] | None = None,
    bin_name: str = "miniharness",
) -> tuple[Context, list[tuple[str, Callable]]]:
    """boot()：加载配置 → 依序应用补丁 → 激活插件 → 断言全部就绪。

    config_path 与补丁支持 .json/.yaml/.yml；YAML 内 !!js 表达式读取时求值。
    """
    env = env or {}
    entries = resolve_js_exprs(load_composition(config_path, bin_name))
    for pp in patch_paths:
        patches = resolve_js_exprs(load_patch_list(pp, bin_name))
        entries = apply_patch(entries, patches)

    root = Context(name="root")
    for key, value in env.items():
        root.provide(key, value)

    manager = PluginManager(root)
    activations = manager.activate([load_plugin(e) for e in entries])

    activated_ids = {name for name, _ in activations}
    missing = [e["id"] for e in entries if e["id"] not in activated_ids]
    if missing:
        raise RuntimeError(f"启动断言失败：以下条目未激活: {missing}")
    return root, activations