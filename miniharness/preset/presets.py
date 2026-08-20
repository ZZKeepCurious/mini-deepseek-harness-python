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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.scope import Context
from ..core.tools import Tool, ToolRegistry

BUILTIN_PRESETS = Path(__file__).parent

# preset id 语法（上游 discovery.ts PRESET_ID）：不匹配的目录名直接跳过
# （.DS_Store 级残渣不占位、不报 broken）
PRESET_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


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
    # 发现期健康标记（上游 discovery 的 broken）：缺 preset.json / 清单损坏的
    # 目录仍占位 roster 行（id 被磁盘占据），挂载期拒绝
    broken: str | None = None

    def mount(self, ctx: Context, agent_scope: Context, host_tools: ToolRegistry) -> ToolRegistry:
        """挂载到 agent 作用域，返回 agent 专属工具视图。

        挂载不变量（与上游 preset 挂载语义一致）：
          * broken preset 在挂载期被拒绝（上游 resolveMountable → PresetMountError）
          * 只从 host 注册表挑工具，不新造工具实现（注册表留在 host plane）
          * host 缺工具 → fail loud（preset 声明了 host 没有的东西）
          * provides 命中 host 已有服务 → 拒绝挂载（进程级冲突）
        """
        if self.broken is not None:
            raise RuntimeError(f"preset {self.id} is broken: {self.broken}")
        missing = [t for t in self.tools if host_tools.resolve(t) is None]
        if missing:
            raise RuntimeError(
                f"preset {self.id} 声明了 host 未提供的工具: {', '.join(missing)}"
            )
        for key in self.provides:
            if ctx.get(key) is not None:
                raise RuntimeError(
                    f"preset {self.id} 声明进程级服务 {key}，但 host 已提供（拒绝挂载，避免与下个会话冲突）"
                )

        # agent 作用域独立的 tools 服务标签（对齐上游 agent scope realm：per-agent
        # 工具注册进 agent 自己的层，不冲撞 host/根标签）；须先于视图创建
        agent_scope._isolate.setdefault("tools", object())
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
    """roster：目录列表即名单。发现 = 扫描 preset 根目录（上游 scanRoot）。

    健康语义（对齐 discovery.ts:139-163）：
      * 根缺失（ENOENT）→ 空名单（用户根首次使用前不存在）
      * 目录名不匹配 PRESET_ID → 跳过（残渣不占位）
      * 名字合法但缺 preset.json / 清单损坏 → 占位 broken 行（id 仍被占据）
      * broken preset 可 resolve（展示/删除需要行），挂载期拒绝
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self._presets: dict[str, Preset] | None = None

    def _scan(self) -> dict[str, Preset]:
        presets: dict[str, Preset] = {}
        try:
            children = sorted(self.root.iterdir())
        except FileNotFoundError:
            return presets  # 根缺失 → 空（上游 ENOENT → []）
        for child in children:
            if not child.is_dir() or not PRESET_ID.match(child.name):
                continue
            manifest = child / "preset.json"
            if not manifest.is_file():
                presets[child.name] = Preset(
                    id=child.name, name=child.name, description="", order=0,
                    broken=f"the composition file preset.json is missing — "
                           f"the directory still occupies the id; delete it or restore the file",
                )
                continue
            try:
                p = load_preset(child)
            except (OSError, ValueError, KeyError, TypeError) as error:
                # 清单损坏 → 占位 broken 行（上游 compositionProblem → broken）
                presets[child.name] = Preset(
                    id=child.name, name=child.name, description="", order=0,
                    broken=f"the composition is unloadable: {error}",
                )
                continue
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