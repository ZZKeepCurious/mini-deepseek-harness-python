"""第 5 章：启动与组合 —— 配置 + 按 id 补丁 + 依赖驱动激活。

对应 dsh 真实源码：packages/boot/app-boot + 组合层（bundle / profile / patch）。

组合不变量：
  1. 补丁算法是纯函数（replace 按 id 整段替换 config / insert 新条目）
  2. 补丁按层叠顺序应用：bundle 层 → profile 级 → home 级 → --patch overlay
  3. 启动结束必须断言"条目已加载 + 已激活"，否则 fail loud
"""
from __future__ import annotations

import importlib
import json
from typing import Any, Callable

from .bus import Context, PluginManager


def apply_patch(entries: list[dict], patches: list[dict]) -> list[dict]:
    """补丁算法（纯函数，禁止复制粘贴别处实现）。"""
    out = [dict(e) for e in entries]
    for patch in patches:
        if "replace" in patch:
            target_id = patch["replace"]["id"]
            new_cfg = patch["replace"]["config"]
            for e in out:
                if e["id"] == target_id:
                    e["config"] = dict(new_cfg)
                    break
            else:
                raise KeyError(f"patch 目标 id={target_id} 不存在")
        elif "insert" in patch:
            out.extend(dict(e) for e in patch["insert"])
        else:
            raise ValueError(f"未知补丁操作: {patch}")
    return out


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
) -> tuple[Context, list[tuple[str, Callable]]]:
    """boot()：加载配置 → 依序应用补丁 → 激活插件 → 断言全部就绪。"""
    env = env or {}
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)
    entries = list(config.get("plugins", []))
    for pp in patch_paths:
        with open(pp, encoding="utf-8") as f:
            patches = json.load(f)
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