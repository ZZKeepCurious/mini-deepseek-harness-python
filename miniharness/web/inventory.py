"""pluginInventory 投影（对齐 packages/host/plugin-inventory + agent-presets composition-inventory）。

- `entries_snapshot`：读 Loader 活树的非 group 条目，投影四字段
  `{entryId, moduleName, enabled, fiberPhase}`；
- `composition_inventory`：给定 preset roster，逐 preset 读组合文件的行
  （`file_composition`，JSON-schema 方言 + `!!js` disabled 求值；求值被拒 → `'conditional'`）；
- `build_inventory`：聚合 `{entries}`（有 roster 时附 `agentPresets`——上游在
  `ctx.get('agentPresets') === undefined` 时省略该键）；
- `PluginInventoryService`：`ctx.pluginInventory` 服务（`list()`），
  `install_plugin_inventory(ctx, roster=...)` 幂等装配。

载体差异：mini preset 为工具清单模型，只有组合形态（`agent.cordis.yml`/`agent.yml`）的
preset 有插件行；`preset.json` 载体行投影为 `rows: []`（无行级数据）。组合行 flatten
工具（disabled/group/conditional）归口 `preset/registry.py`（单一实现，对齐上游
composition-inventory.ts）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.scope import Context, FiberState, Service
from ..loader.include import load_entry_list_yaml
from ..preset.registry import entry_list_problem, flatten_rows

__all__ = [
    "PluginInventoryService",
    "build_inventory",
    "composition_inventory",
    "entries_snapshot",
    "entry_list_problem",
    "file_composition",
    "install_plugin_inventory",
]

_COMPOSITION_FILES = ("agent.cordis.yml", "agent.yml")


def _fiber_phase(fiber: Any) -> str | None:
    """Root fiber 生命周期阶段（对齐上游 FIBER_PHASE）：无 fiber / DISPOSED → None，
    其余直接用 mini 状态名（与上游 phase 词表一致）。"""
    if fiber is None or fiber.state == FiberState.DISPOSED:
        return None
    return fiber.state


def entries_snapshot(ctx: Context) -> list[dict]:
    """读 Loader 活树（缺 loader 服务 → 空）的非 group 条目四字段投影。"""
    loader = ctx.get("loader") if ctx is not None else None
    if loader is None:
        return []
    entries: list[dict] = []
    for entry in loader.entries():
        if entry.options.get("group"):
            continue
        entries.append({
            "entryId": entry.id,
            "moduleName": entry.options.get("name") or entry.options.get("module"),
            "enabled": not entry.disabled,
            "fiberPhase": _fiber_phase(entry.fiber),
        })
    return entries


def file_composition(path: Path) -> dict:
    """一个 preset 组合文件的行（对齐 composition-inventory.ts:159-178）。

    组合形态文件（`agent.cordis.yml`/`agent.yml`）按 entryListSchema 解析并 flatten；
    `preset.json` 载体无插件行 → `{rows: []}`；读取/解析/形状失败 → `{broken: reason}`。
    """
    if Path(path).name not in _COMPOSITION_FILES:
        return {"rows": []}
    try:
        rows = load_entry_list_yaml(Path(path).read_text(encoding="utf-8"))
    except Exception as error:  # noqa: BLE001 - 文件读取/解析错误折算 broken
        return {"broken": str(error)}
    problem = entry_list_problem(rows)
    if problem is not None:
        return {"broken": problem}
    found: list[dict] = []
    flatten_rows(rows, False, found)
    return {"rows": found}


def composition_inventory(roster: Any) -> list[dict]:
    """roster 每个 preset 的 `AgentPresetPluginGroup` 投影（组合行 + broken）。

    roster 需提供 `all() -> Preset[]` 与可选 `default`（id）；preset 需提供
    `id`/`name`/`trust`/`broken`/`path`。
    """
    groups: list[dict] = []
    default_id = getattr(roster, "default", None)
    for preset in roster.all():
        group: dict = {
            "id": preset.id,
            "trust": preset.trust,
            "isDefault": preset.id == default_id,
        }
        if preset.name:
            group["name"] = preset.name
        if preset.broken:
            group["broken"] = preset.broken
            group["rows"] = []
        else:
            result = file_composition(preset.path) if preset.path is not None else {"rows": []}
            if "broken" in result:
                group["broken"] = result["broken"]
                group["rows"] = []
            else:
                group["rows"] = result["rows"]
        groups.append(group)
    return groups


def build_inventory(ctx: Context, roster: Any = None) -> dict:
    """`PluginInventorySnapshot`：`{entries}`，有 roster 时附 `agentPresets`。"""
    snapshot: dict = {"entries": entries_snapshot(ctx)}
    if roster is not None:
        snapshot["agentPresets"] = composition_inventory(roster)
    return snapshot


class PluginInventoryService(Service):
    """`ctx.pluginInventory` 服务（对齐上游 PluginInventoryGateway，方法 `list`）。"""

    provide = "pluginInventory"

    def __init__(self, ctx: Context, roster: Any = None):
        super().__init__(ctx, "pluginInventory")
        self.roster = roster

    def list(self) -> dict:
        return build_inventory(self.ctx, self.roster)


def install_plugin_inventory(ctx: Context, roster: Any = None) -> PluginInventoryService:
    """幂等装配 `ctx.pluginInventory`（已存在则原样返回）。"""
    existing = ctx.get("pluginInventory")
    if existing is not None:
        return existing
    return PluginInventoryService(ctx, roster)
