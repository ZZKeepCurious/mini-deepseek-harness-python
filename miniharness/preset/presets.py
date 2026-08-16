"""第 8 章：preset roster —— 会话级 agent 组合的选择与挂载。

对应 dsh 真实源码：packages/preset（per-session agent composition）+
apps/cli/config/agent-presets/（roster = 目录列表即名单）。

契约（对齐上游）：
  1. roster = 目录列表即名单（filesystem discovery），不维护第二份清单
  2. preset 只携带"这一个 agent 贡献的工具选择 / persona"，注册表留在 host plane
  3. 声明进程级服务的 preset 在挂载时被拒绝（不与下一个会话冲突，fail loud）
  4. 工具目录跨模式可变；模式切换不重建内核（同一 host 注册表，只是换视图）

载体简化说明：上游 preset 组合是 YAML（agent.cordis.yml），mini 用 JSON；
契约对齐的是"组合选择语义"，不是文件格式。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.scope import Context
from ..core.tools import Tool, ToolRegistry

BUILTIN_PRESETS = Path(__file__).parent


@dataclass(frozen=True)
class PersonaConfig:
    """persona 块（上游 agent.cordis.yml 的 persona 行）。
    complete=True 表示系统提示即全部上下文（不注入运行时快照）；
    include_runtime_context=False 关闭运行时上下文注入。"""
    complete: bool = False
    include_runtime_context: bool = True
    system_prompt: str | None = None


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    description: str
    order: int
    tools: list[str] = field(default_factory=list)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    provides: list[str] = field(default_factory=list)   # 进程级服务声明（默认空）

    def mount(self, ctx: Context, agent_scope: Context, host_tools: ToolRegistry) -> ToolRegistry:
        """挂载到 agent 作用域，返回 agent 专属工具视图。

        挂载不变量（与上游 preset 挂载语义一致）：
          * 只从 host 注册表挑工具，不新造工具实现（注册表留在 host plane）
          * host 缺工具 → fail loud（preset 声明了 host 没有的东西）
          * provides 命中 host 已有服务 → 拒绝挂载（进程级冲突）
        """
        missing = [t for t in self.tools if host_tools.resolve(t) is None]
        if missing:
            raise RuntimeError(
                f"preset {self.id} 声明了 host 未提供的工具: {', '.join(missing)}"
            )
        for key in self.provides:
            try:
                ctx.inject(key)
            except KeyError:
                continue
            raise RuntimeError(
                f"preset {self.id} 声明进程级服务 {key}，但 host 已提供（拒绝挂载，避免与下个会话冲突）"
            )

        view = ToolRegistry(agent_scope)
        for name in self.tools:
            view.register(host_tools.resolve(name), scope=agent_scope)
        return view


def load_preset(directory: Path) -> Preset:
    """从目录读 preset.json。目录不存在 / JSON 非法 → fail loud。"""
    manifest = directory / "preset.json"
    with open(manifest, encoding="utf-8") as f:
        raw = json.load(f)
    persona_raw = raw.get("persona", {})
    return Preset(
        id=raw["id"],
        name=raw["name"],
        description=raw.get("description", ""),
        order=raw.get("order", 0),
        tools=list(raw.get("tools", [])),
        persona=PersonaConfig(
            complete=persona_raw.get("complete", False),
            include_runtime_context=persona_raw.get("include_runtime_context", True),
            system_prompt=persona_raw.get("system_prompt"),
        ),
        provides=list(raw.get("provides", [])),
    )


class PresetRoster:
    """roster：目录列表即名单。发现 = 扫描 preset 根目录下每个含 preset.json 的目录。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._presets: dict[str, Preset] | None = None

    def _scan(self) -> dict[str, Preset]:
        presets: dict[str, Preset] = {}
        for child in sorted(self.root.iterdir()):
            if not child.is_dir():
                continue
            if not (child / "preset.json").is_file():
                continue
            p = load_preset(child)
            if p.id in presets:
                raise RuntimeError(f"roster 发现重复 preset id: {p.id}")
            presets[p.id] = p
        return presets

    def ids(self) -> list[str]:
        if self._presets is None:
            self._presets = self._scan()
        return sorted(self._presets, key=lambda i: self._presets[i].order)

    def resolve(self, preset_id: str) -> Preset:
        if self._presets is None:
            self._presets = self._scan()
        if preset_id not in self._presets:
            raise KeyError(f"未知 preset: {preset_id}")
        return self._presets[preset_id]

    def all(self) -> list[Preset]:
        return [self.resolve(i) for i in self.ids()]


def builtin_roster() -> PresetRoster:
    """内置 roster（standard / minimal 两个预设，镜像上游 agent-presets 结构）。"""
    return PresetRoster(BUILTIN_PRESETS)