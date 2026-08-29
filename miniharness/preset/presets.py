"""第 8 章：preset roster —— 会话级 agent 组合的选择、投影与挂载。

对应 dsh 真实源码（dsh-v0.1.2-alpha.1）：packages/preset/agent-presets/src/
  preset.ts（词汇 / PRESET_ID / PresetLockedError）
  discovery.ts（PresetRoot path+trust / scanRoot / discoverPresets）
  index.ts（resolvedRoots 分层 + resolve/defaultId + swap + remove + metadata）
  session.ts（agentPresetProjectionDefinition 会话投影）
  authoring.ts（crossRootPresets 投影：非 user trust 只读）
  mount.ts（resolveMountable → PresetMountError；作用域审计）
  metadata.ts（preset.yml：name/description/order，读失败降级 {}）
以及 apps/cli/config/agent-presets/（roster = 目录列表即名单，shipped 随包）。

契约（对齐上游）：
  1. roster = 目录列表即名单（filesystem discovery），不维护第二份清单
  2. root 分层 = shipped(system) → 配置 roots → harness-home(user)；同名 id
     first-root-wins（上游 discoverPresets 的 resolvedRoots 顺序）
  3. preset 只携带"这一个 agent 贡献的工具选择 / persona"，注册表留在 host plane
  4. 声明进程级服务的 preset 在挂载时被拒绝（不与下一个会话冲突，fail loud）；
     工具目录跨模式可变；模式切换不重建内核（同一 host 注册表，只是换视图）
  5. 投影 = 把"预设 id / 缺省"经分层 root 集合解析为有效 preset
     （project_preset）；会话投影 = header.agentPreset ?? null，fold
     'agent-preset/selected' 事件（project_session_agent_preset，对齐
     session.ts 的 agentPresetProjectionDefinition）
  6. PresetLockedError：会话已开始（存在 turn/start）再选/换 preset → 拒绝
     （对齐 preset.ts swap 的 lock 检查，"its agent preset is fixed"）
  7. 内置 shipped root（system trust）对 authoring 只读：delete/overwrite 非
     user trust → PresetNotWritableError "it ships with the deployment"
     （对齐 authoring.ts deleteComposition；即「核心字段锁定」的可靠对应物）

载体简化（须标注）：
  * 上游预设组合是 YAML（agent.cordis.yml）+ preset.yml 元数据；mini 数据目录
    用 JSON（preset.json，既有载体）。loader 双读：有 preset.json 读 JSON；
    否则 translate_cordis_composition 把上游 agent.cordis.yml 行语义翻译成
    mini 的 Preset（工具名清单 / persona / preset.yml 元数据）——便于把上游
    preset 目录拷入 harness-home 直接用。工具覆盖不判 broken（上游 health 的
    可解析性），由挂载期 host 覆盖 fail loud 兜底（与手写 JSON 同契约）
  * 无 Cordis fiber 常驻挂载：mini 的 mount 仍是每 agent 一次的工具视图安装
    （既有载体差异）；服务 realm 审计退化为 provides 冲突检查 + 无作用域
    上下文拒绝
  * 平台门 !!js process.platform ===/!== 'win32'|'darwin'|'linux' 静态求值；
    其余 !!js 表达式保守按 truthy（跳过该行）
  * 发现为实例级缓存（上游 unmemoized 每次调用重扫）；mini 缓存到首次访问，
    delete_preset 后失效
  * builtin_roster 不含用户根（include_user_root=False）——内置 roster 保持
    确定性；需要用户自管预设根时显式 default_roster()（默认 preset =
    standard，对齐上游 config 缺省）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from ..boot.composition import load_document
from ..core.scope import Context
from ..core.tools import Tool, ToolRegistry

__all__ = [
    "PRESET_ID",
    "SHIPPED_PRESET_ROOT",
    "USER_PRESET_DIR",
    "UnknownPresetError",
    "PresetLockedError",
    "PresetMountError",
    "PresetNotWritableError",
    "PersonaConfig",
    "Preset",
    "PresetRoot",
    "PresetProjection",
    "PresetRoster",
    "assert_preset_selectable",
    "builtin_roster",
    "default_roster",
    "delete_preset",
    "load_preset",
    "project_preset",
    "project_session_agent_preset",
    "select_preset",
    "session_has_started",
    "translate_cordis_composition",
    "user_preset_root",
]

# shipped root：随 miniharness 包分发的 preset 数据目录（system trust，只读）。
# 旧名 BUILTIN_PRESETS 保留为兼容别名（第 8 章前版本引用）。
SHIPPED_PRESET_ROOT = Path(__file__).parent
BUILTIN_PRESETS = SHIPPED_PRESET_ROOT

# preset id 语法（上游 discovery.ts PRESET_ID）：不匹配的目录名直接跳过
# （.DS_Store 级残渣不占位、不报 broken）
PRESET_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# 用户自管预设根：harness-home/.agent-presets（上游 config.agentPresets.path
# 缺省值 '--user-preset-dir'，src/index.ts:31）
USER_PRESET_DIR = ".agent-presets"

# 会话「已开始」判定用的类型（对齐上游 preset session 投影 fetch 'turn/start'）
_TURN_START = "turn/start"
_SELECTED_EVENT = "agent-preset/selected"

# trust 字面量（对齐上游 'system' | 'user'）
PresetTrust = Literal["system", "user"]


def user_preset_root(environ: dict | None = None) -> Path:
    """用户预设根：MINIHARNESS_HOME（或 ~/.miniharness）下的 .agent-presets。

    上游默认 config.agentPresets.path 由 app-boot 的项目(cwd)解析到用户目录；
    mini 固定 harness-home（cwd 无关，对齐 sessions 子命令的 MINIHARNESS_HOME
    挂载点，见 cli/session_cmds.py）。
    """
    home = Path((environ or os.environ).get("MINIHARNESS_HOME", Path.home() / ".miniharness"))
    return home / USER_PRESET_DIR


class UnknownPresetError(Exception):
    """preset id 在 roster 找不到（上游 `preset` 依赖 host 命名空间解析，无法
    解析时报 preset not found；mini 在 roster 显式命名该语义）。"""

    def __init__(self, preset_id: str, available: Iterable[str] = ()):
        self.preset_id = preset_id
        self.available = sorted(available)
        joined = ", ".join(self.available) or "none"
        super().__init__(f'agent-presets: preset "{preset_id}" not found (available: {joined})')


class PresetLockedError(RuntimeError):
    """会话已开始（已落 turn/start）再选/换 preset → 固定不可变。

    对齐上游 preset.ts swap 的 lock 检查：文案 "agent-presets: session `"X"` has
    already started; its agent preset is fixed"。投影不变：后续 select/swap 一律
    被拒，直到会话重开。
    """

    def __init__(self, session_id: str, preset_id: str):
        self.session_id = session_id
        self.preset_id = preset_id
        super().__init__(
            f'agent-presets: session "{session_id}" has already started; '
            f"its agent preset is fixed"
        )


class PresetMountError(RuntimeError):
    """挂载期拒绝（对齐上游 mount.ts resolveMountable → PresetMountError）。"""

    def __init__(self, preset_id: str, reason: str):
        self.preset_id = preset_id
        self.reason = reason
        super().__init__(f'agent-presets: preset "{preset_id}" failed to mount: {reason}')


class PresetNotWritableError(RuntimeError):
    """authoring 拒绝：预设不在可写（user trust）根下（对齐 authoring.ts
    对非 user root 的 delete/overwrite 拒绝）。"""

    def __init__(self, preset_id: str, reason: str):
        self.preset_id = preset_id
        self.reason = reason
        super().__init__(f'agent-presets: preset "{preset_id}" cannot be written: {reason}')


@dataclass(frozen=True)
class PresetRoot:
    """一个预设来源根（对齐上游 discovery.ts PresetRoot：path + trust）。

    trust 语义：system = 随包/部署附带、authoring 只读（shipped root）；
    user = 可写可管理（harness-home 等）。
    """

    path: Path
    trust: PresetTrust = "user"


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
    # 发现期健康标记（上游 discovery 的 broken）：缺组合 / 组合损坏的目录仍占位
    # roster 行（id 被磁盘占据），挂载期拒绝
    broken: str | None = None
    # 来源 trust（对齐上游 PresetRoot.trust；'system' = shipped 只读）与组合
    # 文件路径（preset.json 或 agent.cordis.yml，供展示/删除定位）
    trust: PresetTrust = "user"
    path: Path | None = None

    def mount(self, ctx: Context, agent_scope: Context, host_tools: ToolRegistry) -> ToolRegistry:
        """挂载到 agent 作用域，返回 agent 专属工具视图。

        挂载不变量（与上游 preset 挂载语义一致，失败一律 PresetMountError）：
          * broken preset 在挂载期被拒绝（上游 resolveMountable → PresetMountError）
          * 只从 host 注册表挑工具，不新造工具实现（注册表留在 host plane）
          * host 缺工具 → fail loud（preset 声明了 host 没有的东西）
          * provides 命中 host 已有服务 → 拒绝挂载（进程级冲突）
          * 无作用域上下文 → 拒绝挂载（注册会漏进 root/全局层，污染每个 agent，
            对齐上游 mount.ts 的作用域审计）
        """
        if self.broken is not None:
            raise PresetMountError(self.id, f"preset {self.id} is broken: {self.broken}")
        missing = [t for t in self.tools if host_tools.resolve(t) is None]
        if missing:
            raise PresetMountError(
                self.id,
                f"preset {self.id} 声明了 host 未提供的工具: {', '.join(missing)}",
            )
        for key in self.provides:
            if ctx.get(key) is not None:
                raise PresetMountError(
                    self.id,
                    f"preset {self.id} 声明进程级服务 {key}，但 host 已提供"
                    f"（拒绝挂载，避免与下个会话冲突）",
                )

        if not agent_scope.is_scope():
            # 对齐上游 mount.ts scoped context 审计：把 preset 的工具装进无作用域
            # 上下文 = 注册进 root/全局层，会漏进每个 agent —— 拒绝
            raise PresetMountError(
                self.id,
                "refusing to mount into a non-scoped context; "
                "its registrations would leak into every agent",
            )

        # agent 作用域独立的 tools 服务标签（对齐上游 agent scope realm：per-agent
        # 工具注册进 agent 自己的层，不冲撞 host/根标签）；须先于视图创建
        agent_scope._isolate.setdefault("tools", object())
        view = ToolRegistry(agent_scope)
        for name in self.tools:
            view.register(host_tools.resolve(name), scope=agent_scope)
        return view


def load_preset(directory: Path, trust: PresetTrust = "user") -> Preset:
    """从目录读预设：有 preset.json 读 JSON（mini 载体），否则翻译 agent.cordis.yml。

    目录不存在 / JSON 非法 / YAML 不可加载 → fail loud（roster 捕获为 broken 占位）。
    """
    manifest = directory / "preset.json"
    if manifest.is_file():
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
            trust=trust,
            path=manifest,
        )
    comp = directory / "agent.cordis.yml"
    if not comp.is_file():
        comp = directory / "agent.yml"
    if comp.is_file():
        return translate_cordis_composition(directory, trust)
    raise FileNotFoundError(f"preset {directory.name}: 缺少 preset.json 或 agent.cordis.yml 组合")


class PresetRoster:
    """roster：目录列表即名单（上游 scanRoot，跨 root first-root-wins）。

    构造（对齐上游 DiscoverPresetsOptions + config.agentPresets）：
      roots=[...]            附加配置 root（缺省空）
      default=<id>           缺省 preset（None → resolve(None)/无参投影报错）
      include_shipped_root   True → 最前加 shipped root（system trust）
      include_user_root      True → 最后加 harness-home user root

    健康语义（对齐 discovery.ts:139-163）：
      * 根缺失（ENOENT）→ 空名单（用户根首次使用前不存在）
      * 目录名不匹配 PRESET_ID → 跳过（残渣不占位）
      * 名字合法但缺组合 / 组合损坏 → 占位 broken 行（id 仍被占据）
      * broken preset 可 resolve（展示/删除需要行），挂载期拒绝
    """

    def __init__(
        self,
        roots: Path | Iterable[Path] = (),
        *,
        default: str | None = None,
        include_shipped_root: bool = False,
        include_user_root: bool = False,
        environ: dict | None = None,
    ):
        if isinstance(roots, Path):
            roots = [roots]
        self.default = default
        self._shipped_root = (
            PresetRoot(SHIPPED_PRESET_ROOT, "system") if include_shipped_root else None
        )
        self._roots = [PresetRoot(Path(r).resolve()) for r in roots]
        self._user_root = (
            PresetRoot(user_preset_root(environ), "user") if include_user_root else None
        )
        self._presets: dict[str, Preset] | None = None
        self._source: dict[str, PresetRoot] = {}

    @property
    def resolved_roots(self) -> list[PresetRoot]:
        """分层 root 顺序（对齐上游 index.ts composeRoots）：
        shipped(system) → 配置 roots → harness-home(user)。"""
        out: list[PresetRoot] = []
        if self._shipped_root is not None:
            out.append(self._shipped_root)
        out.extend(self._roots)
        if self._user_root is not None:
            out.append(self._user_root)
        return out

    @property
    def user_root(self) -> PresetRoot | None:
        """构造期加进来的 harness-home user root（作者可写区，可能不存在）。"""
        return self._user_root

    @property
    def authorable(self) -> bool:
        """有 user trust 根 → authoring 可写（上游对只含 system root 的 roster
        投影 crossRootPresets 告诫：shipped 预设不可覆盖）。"""
        return any(r.trust == "user" for r in self.resolved_roots)

    # ---------------- 发现（磁盘扫描，实例级缓存） ----------------

    def _scan(self) -> dict[str, Preset]:
        by_id: dict[str, Preset] = {}
        self._source = {}
        for root in self.resolved_roots:
            for pid, preset in self._scan_root(root).items():
                if pid in by_id:
                    continue  # first-root-wins（上游 discoverPresets 同名取先）
                by_id[pid] = preset
                self._source[pid] = root
        return by_id

    def _scan_root(self, root: PresetRoot) -> dict[str, Preset]:
        presets: dict[str, Preset] = {}
        try:
            children = sorted(root.path.iterdir())
        except FileNotFoundError:
            return presets  # 根缺失 → 空（上游 ENOENT → []）
        for child in children:
            if not child.is_dir() or not PRESET_ID.match(child.name):
                continue
            if not self._has_composition(child):
                presets[child.name] = Preset(
                    id=child.name, name=child.name, description="", order=0,
                    trust=root.trust,
                    broken="the composition file preset.json is missing — "
                           "the directory still occupies the id; delete it or restore the file",
                )
                continue
            try:
                p = load_preset(child, root.trust)
            except Exception as error:
                # 组合损坏 → 占位 broken 行（上游 compositionProblem → broken）
                presets[child.name] = Preset(
                    id=child.name, name=child.name, description="", order=0,
                    trust=root.trust, broken=f"the composition is unloadable: {error}",
                )
                continue
            presets[p.id] = p
        return presets

    @staticmethod
    def _has_composition(directory: Path) -> bool:
        return any(
            (directory / name).is_file()
            for name in ("preset.json", "agent.cordis.yml", "agent.yml")
        )

    def _refresh(self) -> None:
        self._presets = self._scan()

    # ---------------- 查询 / 解析 ----------------

    def ids(self) -> list[str]:
        if self._presets is None:
            self._presets = self._scan()
        return sorted(self._presets, key=lambda i: self._presets[i].order)

    def locate(self, preset_id: str) -> tuple[PresetRoot, Preset]:
        """解析到 (来源根, preset)：未知 id → UnknownPresetError。"""
        if self._presets is None:
            self._presets = self._scan()
        if preset_id not in self._presets:
            raise UnknownPresetError(preset_id, self._presets.keys())
        source = self._source.get(preset_id) or PresetRoot(SHIPPED_PRESET_ROOT, "system")
        return source, self._presets[preset_id]

    def resolve(self, preset_id: str | None = None) -> Preset:
        """解析 preset：显式 id；缺省 → roster.default（未配置 → ValueError）。

        缺省语义对齐上游 resolve(defaultId)：defaultId 为 null 时无缺省可解析
        （上游由 host 的 defaultId 断言兜底，mini 直接在 roster 层报错）。
        """
        if preset_id is None:
            preset_id = self.default
            if preset_id is None:
                raise ValueError(
                    "no default preset is configured; pass a preset id or set the roster default"
                )
        _, preset = self.locate(preset_id)
        return preset

    def all(self) -> list[Preset]:
        return [self._presets[i] for i in self.ids()]

    def rows(self) -> list[dict]:
        """AgentPresetRow 形态（对齐上游 Remote AgentPresetRow）：
        {id, trust, isDefault, name?, description?, broken?}。"""
        out: list[dict[str, Any]] = []
        for preset in self.all():
            row: dict[str, Any] = {
                "id": preset.id,
                "trust": preset.trust,
                "isDefault": preset.id == self.default,
            }
            if preset.name:
                row["name"] = preset.name
            if preset.description:
                row["description"] = preset.description
            if preset.broken is not None:
                row["broken"] = preset.broken
            out.append(row)
        return out


@dataclass(frozen=True)
class PresetProjection:
    """投影结果：把一个 preset 选择解析到 roster 内的具体 preset + 来源上下文。

    对齐上游 Remote selectPreset 的返回：trust 记录来源根（AgentPresetRow.trust），
    is_default 等价于 row.isDefault（id == roster.default），source_root 是该
    行的磁盘来源。
    """

    id: str
    trust: PresetTrust
    preset: Preset
    is_default: bool
    source_root: Path
    default_id: str | None = None


def project_preset(roster: PresetRoster, preset_id: str | None = None) -> PresetProjection:
    """投影：把 id / 缺省解析到 roster 的有效 preset（含来源根与 trust）。

    preset_id 缺省 → roster.default（未配置 → ValueError）。行是投影的物证：
    正是这一行 "preset {id}（trust={trust}）"，随缺省与分层 root 变化而变。
    """
    chosen = preset_id if preset_id is not None else roster.default
    if chosen is None:
        raise ValueError(
            "no default preset is configured; pass a preset id or set the roster default"
        )
    source, preset = roster.locate(chosen)
    return PresetProjection(
        id=preset.id,
        trust=source.trust,
        preset=preset,
        is_default=(chosen == roster.default),
        source_root=source.path,
        default_id=roster.default,
    )


def session_has_started(events: list[dict]) -> bool:
    """会话是否已开始：任何 turn/start 事件（对齐上游 swap 取 'turn/start'）。"""
    return any(ev.get("type") == _TURN_START for ev in events)


def project_session_agent_preset(events: list[dict], header: str | None = None) -> str | None:
    """会话投影：header.agentPreset ?? null，fold 'agent-preset/selected'。

    对齐上游 packages/preset/agent-presets/src/session.ts 的
    agentPresetProjectionDefinition（init: header.agentPreset ?? null；
    apply: data.agentPreset → 覆盖）。投影可空——会话从未声明确认过 preset。
    只读：mini 的持久化层不落此事件（归属 web 会话域，core/session/types.py），
    投影仅用于决策。
    """
    current = header
    for ev in events:
        if ev.get("type") == _SELECTED_EVENT:
            data = ev.get("data") or {}
            current = data.get("agentPreset")
    return current


def assert_preset_selectable(events: list[dict], session_id: str, preset_id: str) -> None:
    """选择锁门：会话已开始 → PresetLockedError（对齐上游 swap 的 lock 检查）。"""
    if session_has_started(events):
        raise PresetLockedError(session_id, preset_id)


def select_preset(
    roster: PresetRoster,
    events: list[dict],
    preset_id: str,
    session_id: str | None = None,
) -> PresetProjection:
    """在 roster 上完成「选择」投影：合法性 → 会话锁 → 分层解析。

    顺序对齐上游 swap（preset.ts:112-117）：先 validatePresetId，再 lock 检查
    （会话已开始 → PresetLockedError），后按 roster 解析（未知 id →
    UnknownPresetError）。session_id 用于锁定报错文案（缺省 'unknown'）。
    """
    if not isinstance(preset_id, str) or preset_id == "":
        raise ValueError("agentPreset must be a non-empty string")
    sid = session_id if session_id is not None else "unknown"
    assert_preset_selectable(events, sid, preset_id)
    return project_preset(roster, preset_id)


def delete_preset(roster: PresetRoster, preset_id: str) -> None:
    """从 user trust 根删除 preset 目录（对齐上游 Remote deletePreset → authoring）。

    shipped / 配置 root（非 user trust）→ PresetNotWritableError
    "it ships with the deployment"；路径逃逸防御：只能删被 roster 定位到、
    位于来源根下的 PRESET_ID 目录。删后失效发现缓存（见模块头简化说明）。
    """
    source, preset = roster.locate(preset_id)
    if preset.trust != "user":
        raise PresetNotWritableError(preset.id, "it ships with the deployment")
    target = source.path / preset.id
    if not target.resolve().is_relative_to(source.path.resolve()):
        raise PresetNotWritableError(preset.id, "does not resolve inside its preset root")
    shutil.rmtree(target)
    roster._presets = None
    roster._source = {}


def builtin_roster(default: str = "standard") -> PresetRoster:
    """内置 roster：shipped root（system trust），缺省 standard。

    mini 随包 standard / minimal 两个预设（JSON 载体）；上游随包 4 个
    （cordis / minimal / ptc / standard，YAML 载体）——数量差异是既有载体差异
    的外延。不含用户根：内置 roster 保持确定性；需要用户自管预设根时用
    default_roster()。
    """
    return PresetRoster([], default=default, include_shipped_root=True)


def default_roster(
    environ: dict | None = None,
    *,
    default: str = "standard",
) -> PresetRoster:
    """完整分层 roster：shipped + harness-home 用户根（对齐上游 config 缺省；
    默认 preset 仅参数化，不支持 config 文件覆盖，见模块头简化说明）。"""
    return PresetRoster(
        [], default=default, include_shipped_root=True,
        include_user_root=True, environ=environ,
    )


# ---------------- YAML 翻译（agent.cordis.yml → mini Preset） ----------------
#
# 上游 preset 组合是「插件行列表」，每行挂一个插件的 config（顶层行含 name +
# config）；group:true 的行把 config 当行列表嵌套。mini 把这组行语义翻译成
# 自己的 Preset（工具名清单 + persona + preset.yml 元数据）。映射是 curated +
# 约定：
#   * curated 表：上游 dsh-* 插件族 → mini catalog 工具名
#   * 兜底：dsh-tool-<suffix> → 同名工具（挂载期由 host 覆盖 fail loud 兜底）
#   * persona 行 → PersonaConfig；preset.yml 读 name/description/order
#   * 其它 host-plane 基础设施行（terminal / fs-local / skill-filesystem /
#     compaction / command / workflow-worker-thread 等）不产工具，跳过


_PLATFORM_EXPR = re.compile(r"^process\.platform\s*(===|!==)\s*'(win32|darwin|linux)'\s*$")

_PLUGIN_TOOL_NAMES: dict[str, tuple[str, ...]] = {
    "dsh-tool-bash": ("bash",),
    "dsh-tool-bash-persistent": ("bash",),
    "dsh-tool-pwsh": ("pwsh",),
    "dsh-tool-pwsh-persistent": ("pwsh",),
    "dsh-tool-str-replace-editor": ("str_replace_editor",),
    "dsh-tool-fs": ("fs_read", "fs_write"),
    "dsh-tool-skill": ("skills",),
    "dsh-tool-goal": ("goal",),
    "dsh-plan-mode": ("plan",),
    "dsh-tool-web": ("web_search",),
}


def _plugin_tool_names(plugin_name: str) -> tuple[str, ...]:
    """插件名 → mini 工具名清单（无映射配置 → 空元组）。
    上游插件名带 scope（@deepseek-ai/dsh-tool-bash），掐 scope 尾匹配。"""
    tail = plugin_name.rsplit("/", 1)[-1]
    mapped = _PLUGIN_TOOL_NAMES.get(tail)
    if mapped is not None:
        return mapped
    if tail.startswith("dsh-tool-"):
        return (tail[len("dsh-tool-"):],)
    return ()


def _is_persona_row(plugin_name: str) -> bool:
    return plugin_name.rsplit("/", 1)[-1] == "dsh-persona"


def _eval_platform_expr(expr: str) -> bool:
    """!!js process.platform 门静态求值；不认识的表达式保守为真（跳过该行）。"""
    m = _PLATFORM_EXPR.match(expr.strip())
    if m is None:
        return True
    op, target = m.group(1), m.group(2)
    match = sys.platform == target
    return match if op == "===" else not match


def _row_disabled(row: dict) -> bool:
    disabled = row.get("disabled")
    if isinstance(disabled, dict) and set(disabled) == {"__jsExpr"}:
        return _eval_platform_expr(disabled["__jsExpr"])
    return bool(disabled)


def _validate_rows(rows: Any, at: str) -> None:
    """结构校验（对齐上游 composition 的 entryListProblem）：形状错 → fail loud。

    平台门不影响结构校验：被禁用于当前平台的行仍须形状合法。
    """
    if not isinstance(rows, list):
        raise ValueError("composition 顶层必须是插件行列表")
    for i, row in enumerate(rows):
        mark = f"row {i + 1}" if not at else f"{at} row {i + 1}"
        if not isinstance(row, dict):
            raise ValueError(f"{mark} 不是插件行（须为含 name 的 map）")
        name = row.get("name")
        if not isinstance(name, str) or name == "":
            raise ValueError(f'{mark} 未命名插件（"name" 非空字符串必需）')
        if row.get("group") is True:
            config = row.get("config")
            if not isinstance(config, list):
                raise ValueError(f"group {mark} 必须持有行列表（config）")
            _validate_rows(config, mark)


def _fold_tools(rows: list[dict], into: list[str]) -> None:
    """行列表 → 启用行的工具名清单（含 group 递归；平台门静态求值）。"""
    for row in rows:
        if row.get("group") is True:
            if not _row_disabled(row):
                _fold_tools(row.get("config") or [], into)
            continue
        if _row_disabled(row):
            continue
        name = row.get("name")
        if not isinstance(name, str) or _is_persona_row(name):
            continue
        for tool_name in _plugin_tool_names(name):
            if tool_name not in into:
                into.append(tool_name)


def _first_enabled_persona(rows: list[dict]) -> dict | None:
    """第一个启用中的 persona 行（对齐上游 persona 只有一行的现实，取先后语义）。"""
    for row in rows:
        if row.get("group") is True:
            if not _row_disabled(row):
                found = _first_enabled_persona(row.get("config") or [])
                if found is not None:
                    return found
            continue
        if _row_disabled(row):
            continue
        name = row.get("name")
        if isinstance(name, str) and _is_persona_row(name):
            return row
    return None


def _read_preset_metadata(directory: Path) -> dict:
    """preset.yml 元数据；缺失/读失败 → {}（对齐上游 metadata.ts 降级）。"""
    meta = directory / "preset.yml"
    if not meta.is_file():
        return {}
    try:
        data = load_document(meta, "miniharness", "preset metadata")
    except RuntimeError:
        return {}
    return data if isinstance(data, dict) else {}


def _meta_text(meta: dict, key: str, default: str) -> str:
    v = meta.get(key)
    return v if isinstance(v, str) else default


def _meta_int(meta: dict, key: str, default: int) -> int:
    v = meta.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        return default
    return v


def translate_cordis_composition(directory: Path, trust: PresetTrust = "user") -> Preset:
    """把上游 agent.cordis.yml（+preset.yml）翻译成 mini Preset。

    找不到组合 / 不可加载 / 结构非法 → fail loud（roster 捕获为 broken 占位）。
    """
    comp = directory / "agent.cordis.yml"
    if not comp.is_file():
        comp = directory / "agent.yml"
    entries = load_document(comp, "miniharness", "composition")
    if isinstance(entries, dict) and isinstance(entries.get("plugins"), list):
        entries = entries["plugins"]
    _validate_rows(entries, "")
    meta = _read_preset_metadata(directory)

    tools: list[str] = []
    _fold_tools(entries, tools)

    persona_row = _first_enabled_persona(entries)
    if persona_row is None:
        persona = PersonaConfig()
    else:
        pc = persona_row.get("config") or {}
        persona = PersonaConfig(
            complete=bool(pc.get("complete")),
            include_runtime_context=bool(pc.get("includeRuntimeContext", True)),
            system_prompt=pc.get("text"),
        )

    return Preset(
        id=directory.name,
        name=_meta_text(meta, "name", directory.name),
        description=_meta_text(meta, "description", ""),
        order=_meta_int(meta, "order", 0),
        tools=tools,
        persona=persona,
        trust=trust,
        path=comp,
    )