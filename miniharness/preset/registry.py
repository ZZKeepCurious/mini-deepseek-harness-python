"""preset 声明式载体：register(definition) + 声明行 composition inventory。

对齐上游 rc.1 `packages/preset/agent-preset-registry/src/`（index.ts /
definition.ts / composition-inventory.ts / mount.ts）。

语义（与上游一致）：
- **register(definition)**：声明式注册一个 preset——`{id, name?, description?,
  order?, plugins: 行列表}`。重复 id 拒绝；返回 unregister disposer（删除声明
  并失效）。注册即把声明行折叠为可读组合行（声明态）。
- **definition_composition(rows)**：声明态行投影（对齐 definitionComposition）
  ——`entryListProblem` 形状校验 + `flattenRows`（group 结构性跳过、children 继承
  `disabled`、`!!js` 求值被拒标 `'conditional'`）。
- **composition_inventory()**：每声明 preset 的 `AgentPresetComposition`（id/
  name?/isDefault/broken?/rows），与目录发现侧共用投影面。
- **roster()**：`AgentPresetRoster` 形态——**只含 `presets`**（上游 0.2.0-rc.2
  删除 `modeSelectionEnabled` 后 roster 恒 `{presets}`）。缺省 id 语义对齐上游
  `defaultId = selectedDefault ?? default`（见 `default_id`）。
- **mounted_composition_rows**：上游激活态读 EntryTree 真实 fiber 状态——mini
  无 live fiber 常驻挂载（preset 是工具清单模型 + 每 agent 工具视图安装，既有
  载体差异登记），只提供声明态行；挂载树载体差异随 verified-diffs §2.77 延续。

flat 工具（`_disabled_contribution`/`_combine_disabled`/`_flatten_rows`）从
`web/inventory.py` 上移到本域共享（单一实现，web 侧改为复用；上游两者同源于
composition-inventory.ts）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.scope import Context
from ..loader.include import load_entry_list_yaml
from ..loader.utils import evaluate_js_expr, is_js_expr

__all__ = [
    "AgentPresetCompositionRow",
    "AgentPresetRegistry",
    "CompositionRowEnablement",
    "PresetDefinition",
    "combine_disabled",
    "definition_composition",
    "disabled_contribution",
    "entry_list_problem",
    "flatten_rows",
    "install_agent_preset_registry",
]

CompositionRowEnablement = bool | str


@dataclass(frozen=True)
class PresetDefinition:
    """一个声明式 preset 的定义（对齐上游 PresetDefinition）。"""

    id: str
    name: str | None = None
    description: str | None = None
    order: int = 0
    plugins: list = field(default_factory=list)


@dataclass(frozen=True)
class AgentPresetCompositionRow:
    """声明态组合行（对齐上游 AgentPresetCompositionRow）。"""

    entry_id: str | None
    module_name: str
    enabled: CompositionRowEnablement
    condition: str | None = None


def entry_list_problem(rows: Any, at: str = "") -> str | None:
    """组合行形状检查（对齐 agent-presets discovery.ts:77-98，措辞逐字）。"""
    if not isinstance(rows, list):
        return ("the composition must be a top-level list of plugin rows"
                if at == "" else f"group {at} must hold a list of plugin rows")
    for index, row in enumerate(rows):
        label = f"row {index + 1}" if at == "" else f"{at} row {index + 1}"
        if not isinstance(row, dict):
            return f'{label} is not a plugin row (expected a map with a "name")'
        name = row.get("name")
        if not isinstance(name, str) or name == "":
            return f'{label} names no plugin (a "name" string is required)'
        if row.get("group") is True:
            nested = entry_list_problem(row.get("config"), label)
            if nested is not None:
                return nested
    return None


def disabled_contribution(value: Any) -> bool | str:
    """一行 disabled 节点对有效启用的贡献（对齐 composition-inventory.ts:79-94）：
    `!!js` 求值被拒 → `'conditional'`，其余即 `bool(value)`。"""
    if is_js_expr(value):
        try:
            return bool(evaluate_js_expr(value["__jsExpr"]))
        except BaseException:
            return "conditional"
    return bool(value)


def combine_disabled(outer: bool | str, own: bool | str) -> bool | str:
    if outer is True or own is True:
        return True
    if outer == "conditional" or own == "conditional":
        return "conditional"
    return False


def flatten_rows(rows: list, outer_disabled: bool | str, found: list) -> None:
    for row in rows:
        disabled = combine_disabled(outer_disabled, disabled_contribution(row.get("disabled")))
        if row.get("group") is True:
            flatten_rows(row.get("config") or [], disabled, found)
            continue
        entry = {
            "entryId": row.get("id") if isinstance(row.get("id"), str) and row.get("id") else None,
            "moduleName": row.get("name"),
            "enabled": (False if disabled is True
                        else "conditional" if disabled == "conditional" else True),
        }
        if is_js_expr(row.get("disabled")):
            entry["condition"] = row["disabled"]["__jsExpr"]
        found.append(entry)


def definition_composition(rows: Any) -> dict:
    """声明态行投影（对齐 definitionComposition）：校验 + flatten。

    @returns `{"rows": [...]}` 或 `{"broken": reason}`。
    """
    problem = entry_list_problem(rows)
    if problem is not None:
        return {"broken": problem}
    found: list = []
    flatten_rows(rows, False, found)
    return {"rows": found}


def load_definition_rows(path) -> list:
    """从组合文件读声明行（agent.cordis.yml/agent.yml，entry-list YAML 方言）。"""
    data = load_entry_list_yaml(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("plugins"), list):
        data = data["plugins"]
    if not isinstance(data, list):
        raise ValueError("preset composition must be a top-level list of plugin rows")
    return data


class AgentPresetRegistry:
    """声明式 preset 注册表（对齐上游 AgentPresetRegistry 的声明面）。

    注册即折叠声明行（声明态 compositionInventory 可直接读）；unregister 删除
    声明。mini 无 live fiber 常驻挂载（PresetTree），激活态行不提供——挂载树
    载体差异延续（§2.77）。
    """

    def __init__(self, ctx: Context | None = None, *,
                 default: str | None = None,
                 selected_default: str | None = None,
                 roster: Any = None):
        self.ctx = ctx
        self._definitions: dict[str, PresetDefinition] = {}
        self._default_id: str | None = None
        self._configured_default = default
        self._selected_default = selected_default
        self._roster = roster

    @property
    def roster(self) -> Any:
        """目录发现 roster（提供工具清单 + persona 的活组合面）；未接线 → None。"""
        return self._roster

    @property
    def default_id(self) -> str | None:
        """随后创建会话的缺省 preset（上游 `defaultId = selectedDefault ?? default`）。

        用户选择（`selected_default`，经 Settings 写入）优先于部署缺省
        （`default`）；两者皆无时回落到首个注册的声明（mini 声明式载体补充）。
        """
        return self._selected_default or self._configured_default or self._default_id

    def register(self, definition: PresetDefinition) -> Callable[[], None]:
        """注册一个声明式 preset；重复 id → ValueError。

        @returns unregister disposer（删除声明；重复调用幂等）。
        """
        if not isinstance(definition.id, str) or not definition.id.strip():
            raise ValueError("Preset id must not be empty")
        if definition.id in self._definitions:
            raise ValueError(f"Duplicate agent preset: {definition.id}")
        self._definitions[definition.id] = definition
        self._default_id = self._default_id or definition.id
        disposed = False

        def unregister() -> None:
            nonlocal disposed
            if disposed:
                return
            disposed = True
            self._definitions.pop(definition.id, None)
            if self._default_id == definition.id:
                self._default_id = next(iter(self._definitions), None)

        return unregister

    def definitions(self) -> list[PresetDefinition]:
        return [self._definitions[i] for i in sorted(self._definitions)]

    def ids(self) -> list[str]:
        return sorted(self._definitions)

    def locate(self, preset_id: str) -> PresetDefinition:
        try:
            return self._definitions[preset_id]
        except KeyError:
            raise KeyError(preset_id) from None

    def composition_inventory(self) -> list[dict]:
        """每声明 preset 的 `AgentPresetComposition`（声明态行 + broken）。"""
        groups: list[dict] = []
        for definition in self.definitions():
            group: dict = {
                "id": definition.id,
                "isDefault": definition.id == self.default_id,
            }
            if definition.name:
                group["name"] = definition.name
            result = definition_composition(definition.plugins)
            if "broken" in result:
                group["broken"] = result["broken"]
                group["rows"] = []
            else:
                group["rows"] = result["rows"]
            groups.append(group)
        return groups

    def list_presets(self) -> list[dict]:
        """每声明 preset 的显示元数据（对齐上游 `list()` 的 `AgentPreset` 行）。"""
        rows: list[dict] = []
        for definition in self.definitions():
            row: dict = {"id": definition.id}
            if definition.name is not None:
                row["name"] = definition.name
            if definition.description is not None:
                row["description"] = definition.description
            result = definition_composition(definition.plugins)
            if "broken" in result:
                row["broken"] = result["broken"]
            rows.append(row)
        return rows

    def roster(self) -> dict:
        """选择名单 `AgentPresetRoster`（对齐上游 remoteExportList）。

        上游 0.2.0-rc.2 起 roster **只有 `presets`**——`modeSelectionEnabled`
        字段删除。每条行按当前 `default_id` 标 `isDefault`。
        """
        default_id = self.default_id
        return {"presets": [
            {**row, "isDefault": row["id"] == default_id}
            for row in self.list_presets()
        ]}

    def resolve_composition(self, preset_id: str) -> dict | None:
        """解析一个 preset 的**活组合**（工具清单 + persona），供子代理挂载。

        对齐上游 `AgentPresetRegistry.mount(ctx, id)` 的组合产出面；mini 无 live
        fiber 常驻挂载，故只产出组合数据，由调用方（子代理物化）应用到子作用域。
        目录 roster 在场时以其活组合为准（唯一工具/persona 解析面）；仅持声明注
        册表时回退为空组合（声明行无工具/persona 可解析）。带 `broken` 的组合表示
        该 preset 不可挂载（上游 resolveMountable → PresetMountError 同向）。
        未知 id → None。
        """
        if self._roster is not None:
            if preset_id not in self._roster.ids():
                return None
            preset = self._roster.resolve(preset_id)
            broken = getattr(preset, "broken", None)
            if broken is not None:
                return {"id": preset.id, "tools": [], "persona": None,
                        "broken": broken}
            return {"id": preset.id, "tools": list(preset.tools),
                    "persona": preset.persona}
        if preset_id in self._definitions:
            return {"id": preset_id, "tools": [], "persona": None}
        return None


def install_agent_preset_registry(
    ctx: Context,
    *,
    default: str | None = None,
    selected_default: str | None = None,
    roster: Any = None,
) -> AgentPresetRegistry:
    """幂等装配 `ctx.agentPresets` 声明式注册表。

    mini 既有 `ctx.agentPresets` 服务由 web 组合装配（`install_settings_controller`
    的 roster 是目录发现）；本注册表是声明式补充。已装返回既有实例。
    `default` / `selected_default` 配置缺省 id（对齐上游 Config 的
    `default` / `selectedDefault`；后者为 Settings 写入的用户选择）。
    `roster` 提供活组合（工具清单 + persona）解析面，供子代理挂载。
    """
    existing = ctx.get("agentPresets")
    if isinstance(existing, AgentPresetRegistry):
        existing._roster = roster if roster is not None else existing._roster
        return existing
    registry = AgentPresetRegistry(
        ctx, default=default, selected_default=selected_default, roster=roster)
    ctx.provide("agentPresets", registry)
    return registry
