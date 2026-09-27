"""权限预设（PermissionPresetService）：一个产品级「权限」选择器。

上游：packages/interaction/permission-presets/src/index.ts（475 行）+ types.ts。

一个预设捆绑两个独立旋钮（沙箱模式 + 审批策略），切换经各自 canonical
setter 写入；`permission/preset` 是 durable 用户意图（log-only 非 surface，
整值替换最后一条胜出），`permissions` 投影（stateVersion 2）fold 它 +
`sandbox/mode` + `approval/policy` + `session/end-seed`。

语义（已核实）：
  * CUSTOM_PRESET='custom'（派生"无匹配预设"态，只读）、AUTO_PRESET='auto'
    （实验 per-call 审查，register_auto 注册）。
  * `derive(state)`：sandbox = state.sandbox ?? ctx.shell.sandboxMode（组合
    缺省）；approval = state.approval ?? ctx.approval.config.policy ?? 'ask'。
    持久 `permission/preset` 若仍匹配其 bundle 优先（shared-bundle 平手）；
    否则按声明序第一个匹配预设；无匹配 → 'custom'。
  * `set(session, name)`：resolve → 必要旋钮写入（只写变更者）→ 非当前
    preset 追加 `permission/preset`。事件序：permission/preset 在前，
    随后 sandbox/mode、approval/policy（测试钉死）。选择当前生效预设 → 零
    事件（净零）。
  * `pin_initial_permission(session)`：新会话（全空且未 seeded）按当前用户
    default 写 preset+sandbox+approval；seeded/部分补齐只填缺失事实。
  * `/permission` 命令（commands 在场时注册）：bare 报当前 preset 与可用集；
    已知名切换成功；未知名报错。
  * `catalog()`：{options, defaultOptions, defaultPreset}（Remote 读面）。

载体差异：上游 TypertRemoteService + `@Remote('catalog')`；mini 以 Service
承载 catalog 方法，web/api.py 代理。组合缺省读 `ctx.shell.sandboxMode`（
沙箱执行器能力事实）与 approval 配置；mini 无 confining shell 时以
`ctx.sandboxPolicy.default_mode` 兜底（web 组合未装 shell）。
"""
from __future__ import annotations

from typing import Any

from ..core.scope import Context, Service
from ..core.session.session import Session
from ..core.session import create_message, text_block
from .approval import APPROVAL_POLICIES, set_approval_policy

__all__ = [
    "AUTO_PRESET",
    "CUSTOM_PRESET",
    "PermissionPresetError",
    "PermissionPresetService",
    "install_permission_presets",
]

CUSTOM_PRESET = "custom"
AUTO_PRESET = "auto"

#: SANDBOX_MODES（上游 dsh-sandbox-policy/session-mode.ts:42）。
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

_DEFAULT_PRESETS = {
    "workspace-write": {
        "sandbox": "workspace-write", "approval": "ask",
        "name": "workspace-write",
        "description": "Write inside the workspace and permitted temporary "
                       "directories; wider retries require approval.",
    },
    "danger-full-access": {
        "sandbox": "danger-full-access", "approval": "never",
        "name": "danger-full-access",
        "description": "Full file access without approval prompts.",
    },
}


class PermissionPresetError(RuntimeError):
    """权限预设失败（code 进 web/envelope RPC_ERROR_CODES）。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class PermissionPresetService(Service):
    """ctx.permissionPresets：预设表 + 派生 + 写路径 + 会话钉扎 + /permission。"""

    provide = "permissionPresets"

    def __init__(self, ctx: Context, config: dict | None = None):
        config = dict(config or {})
        presets = dict(config.get("presets") or _DEFAULT_PRESETS)
        for name in (CUSTOM_PRESET, AUTO_PRESET):
            if name in presets:
                raise PermissionPresetError(
                    "permission/invalid",
                    f'permission: "{name}" is reserved and cannot name a configured preset')
        for name, spec in presets.items():
            self._validate_spec(name, spec)
        self.presets = presets
        self._auto_admit = None
        self._auto_count = 0
        # 组合缺省：沙箱模式 = confining shell 的能力事实 ?? sandboxPolicy 缺省；
        # 审批 = approval 配置 ?? 'ask'。
        self._composition_sandbox = self._resolve_composition_sandbox(ctx)
        self._composition_approval = self._resolve_composition_approval(ctx)
        inferred = self._derive_knobs(None, None, seeded=False)
        default = config.get("defaultPreset") or inferred
        if default == CUSTOM_PRESET:
            raise PermissionPresetError(
                "permission/invalid",
                "permission: composed sandbox and approval defaults match no preset; "
                "configure defaultPreset explicitly")
        if default not in presets:
            raise PermissionPresetError(
                "permission/invalid", f'permission: unknown default preset "{default}"')
        self._default_preset = default
        super().__init__(ctx, "permissionPresets")
        self._disposers: list = []
        self._disposers.append(ctx.on("session/created", self._on_session_created))
        # 已存在的会话在装配时钉扎（上游 mount 时遍历既有会话）
        sessions = ctx.get("sessions")
        if sessions is not None:
            for session in sessions.list():
                self.pin_initial_permission(session)
        self._install_permission_command()
        self._register_projection()

    # ---------- 配置校验 ----------

    @staticmethod
    def _validate_spec(name: str, spec: Any) -> None:
        if not isinstance(spec, dict):
            raise PermissionPresetError(
                "permission/invalid", f'permission: preset "{name}" must be an object')
        sandbox = spec.get("sandbox")
        if sandbox not in SANDBOX_MODES:
            raise PermissionPresetError(
                "permission/invalid",
                f'permission: preset "{name}" sandbox must be one of {list(SANDBOX_MODES)}')
        approval = spec.get("approval")
        if approval not in APPROVAL_POLICIES:
            raise PermissionPresetError(
                "permission/invalid",
                f'permission: preset "{name}" approval must be one of {list(APPROVAL_POLICIES)}')

    def _resolve_composition_sandbox(self, ctx: Context) -> str:
        shell = ctx.get("shell")
        if shell is not None and hasattr(shell, "sandbox_mode"):
            return shell.sandbox_mode
        policy = ctx.get("sandboxPolicy")
        if policy is not None and getattr(policy, "default_mode", None) is not None:
            return policy.default_mode
        return "read-only"

    def _resolve_composition_approval(self, ctx: Context) -> str:
        approval = ctx.get("approval")
        if approval is not None:
            default = getattr(approval, "_config_policy", None)
            if default in APPROVAL_POLICIES:
                return default
        return "ask"

    # ---------- 只读面 ----------

    @property
    def names(self) -> list[str]:
        names = list(self.presets)
        if self._auto_admit is not None:
            names.append(AUTO_PRESET)
        return names

    def default_preset(self) -> str:
        return self._default_preset

    def catalog(self) -> dict:
        """`catalog` Remote：进程级读目录。"""
        options = [self.option_of(name) for name in self.presets]
        if self._auto_admit is not None:
            options.append(self.option_of(AUTO_PRESET))
        return {
            "options": options,
            "defaultOptions": [self.option_of(name) for name in self.presets],
            "defaultPreset": self._default_preset,
        }

    def resolve(self, name: str) -> dict:
        """按名解析预设 bundle；未知（且非 live Auto）抛错。"""
        if name == AUTO_PRESET and self._auto_admit is not None:
            return {"sandbox": "danger-full-access", "approval": "never"}
        spec = self.presets.get(name)
        if spec is None:
            raise PermissionPresetError(
                "permission/invalid",
                f'permission: unknown preset "{name}" (known: {", ".join(self.names)})')
        return dict(spec)

    def option_of(self, name: str) -> dict:
        if name == CUSTOM_PRESET:
            return {"value": CUSTOM_PRESET, "name": "Custom",
                    "description": "Current sandbox and approval settings do not match a preset."}
        if name == AUTO_PRESET:
            return {"value": AUTO_PRESET, "name": AUTO_PRESET}
        spec = self.presets.get(name, {})
        option: dict = {"value": name, "name": spec.get("name") or name}
        if spec.get("description"):
            option["description"] = spec["description"]
        return option

    # ---------- 状态 ----------

    def _permission_state(self, session: Session) -> dict:
        registry = self.ctx.get("sessionProjections")
        if registry is not None:
            state = registry.state_of(session, "permissions")
            if state is not None:
                return state
        preset = None
        sandbox = None
        approval = None
        seeded = False
        for event in session.events:
            etype = event["type"]
            data = event.get("data") or {}
            if etype == "permission/preset":
                preset = data.get("preset")
            elif etype == "sandbox/mode":
                sandbox = data.get("mode")
            elif etype == "approval/policy":
                approval = data.get("policy")
            elif etype == "session/end-seed":
                seeded = True
        return {"preset": preset, "sandbox": sandbox, "approval": approval,
                "seeded": seeded}

    def _derive_knobs(self, sandbox: str | None, approval: str | None,
                      seeded: bool) -> str:
        """derive(state)：组合缺省补齐后按声明序匹配预设。"""
        sandbox = sandbox or self._composition_sandbox
        approval = approval or self._composition_approval
        for name, spec in self.presets.items():
            if spec["sandbox"] == sandbox and spec["approval"] == approval:
                return name
        return CUSTOM_PRESET

    def derive(self, state: dict) -> str:
        """按投影状态派生当前预设（含 durable preset 优先）。"""
        sandbox = state.get("sandbox") or self._composition_sandbox
        approval = state.get("approval") or self._composition_approval
        preset = state.get("preset")
        if preset is not None and preset != CUSTOM_PRESET and preset != AUTO_PRESET:
            spec = self.presets.get(preset)
            if spec is not None and spec["sandbox"] == sandbox \
                    and spec["approval"] == approval:
                return preset
        for name, spec in self.presets.items():
            if spec["sandbox"] == sandbox and spec["approval"] == approval:
                return name
        return CUSTOM_PRESET

    def current(self, session: Session) -> str:
        return self.derive(self._permission_state(session))

    # ---------- 写路径 ----------

    def set(self, session: Session, name: str, agent: Any = None) -> None:
        """程序化切换预设（`set`）；`agent` 在场时经 approval.set_policy 注入
        模型通知（命令路径），否则只写旋钮（上游 set() 不注入通知）。

        事件序对齐上游 apply()：permission/preset（durable 身份）在前，随后
        只写变更的旋钮（sandbox/mode、approval/policy）。
        """
        spec = self.resolve(name)
        if name == AUTO_PRESET and self._auto_admit is not None:
            self._auto_admit()
        current = self.current(session)
        knobs = self._permission_state(session)
        sandbox = knobs.get("sandbox") or self._composition_sandbox
        approval = knobs.get("approval") or self._composition_approval
        if current != name:
            session.append("permission/preset", {"preset": name})
        if spec["sandbox"] != sandbox:
            self._set_sandbox_mode(session, spec["sandbox"])
        if spec["approval"] != approval:
            self._set_approval_policy(session, spec["approval"], agent)

    def _set_sandbox_mode(self, session: Session, mode: str) -> None:
        from ..seams.sandbox_policy import set_sandbox_mode
        set_sandbox_mode(session, mode)

    def _set_approval_policy(self, session: Session, policy: str, agent: Any) -> None:
        approval = self.ctx.get("approval")
        if agent is not None and approval is not None \
                and hasattr(approval, "set_policy"):
            approval.set_policy(agent, policy)
            return
        set_approval_policy(session, policy)

    # ---------- Auto 集成 ----------

    def register_auto(self, admit) -> object:
        """为某集成的 effect 生命周期发布 Auto；重复注册抛错；返回 disposer。"""
        if self._auto_admit is not None:
            raise PermissionPresetError("permission/duplicate-auto",
                                        "permission: Auto is already registered")
        self._auto_admit = admit
        self._emit_catalog_changed()

        def dispose() -> None:
            if self._auto_admit is not admit:
                return
            self._auto_admit = None
            self._emit_catalog_changed()

        return dispose

    def _emit_catalog_changed(self) -> None:
        try:
            self.ctx.emit("permission-presets/catalog-changed")
        except Exception:  # noqa: BLE001 - 通知失败不影响（上游各自 contain）
            pass

    # ---------- 会话钉扎 ----------

    def _on_session_created(self, payload: dict) -> None:
        session = payload.get("session")
        if session is not None:
            self.pin_initial_permission(session)

    def pin_initial_permission(self, session: Session) -> None:
        """新会话/恢复会话填初始权限（index.ts:422-450）。"""
        state = self._permission_state(session)
        preset = state.get("preset")
        sandbox = state.get("sandbox")
        approval = state.get("approval")
        seeded = state.get("seeded")

        if preset == AUTO_PRESET:
            if self._auto_admit is None:
                raise PermissionPresetError(
                    "permission/auto-unavailable",
                    'permission: cannot restore preset "auto" without its active integration')
            self._auto_admit()

        if preset is None and sandbox is None and approval is None and not seeded:
            # 全新会话：按当前用户 default
            name = self._default_preset
            spec = self.resolve(name)
            session.append("permission/preset", {"preset": name})
            self._set_sandbox_mode(session, spec["sandbox"])
            self._set_approval_policy(session, spec["approval"], None)
            return

        # seeded/部分补齐：只填缺失事实
        effective = self.derive(state)
        if preset is None and effective != CUSTOM_PRESET:
            session.append("permission/preset", {"preset": effective})
        if sandbox is None:
            self._set_sandbox_mode(session, self._composition_sandbox)
        if approval is None:
            self._set_approval_policy(session, self._composition_approval, None)

    # ---------- /permission 命令 ----------

    def _install_permission_command(self) -> None:
        commands = self.ctx.get("commands")
        if commands is None:
            return
        from ..commands import CommandInvocation  # noqa: F401 - 类型

        def handler(invocation) -> dict:
            raw = invocation.raw_input.strip()
            if raw == "":
                return {
                    "kind": "success",
                    "text": f"current preset {self.current(invocation.agent.session)} "
                            f"(available: {', '.join(self.names)})",
                }
            if raw not in self.names:
                return {
                    "kind": "error",
                    "text": f'unknown preset "{raw}" (available: {", ".join(self.names)})',
                }
            self.set(invocation.agent.session, raw, agent=invocation.agent)
            return {"kind": "success", "text": f"preset {raw}"}

        self._command_disposer = commands.register(
            "permission", "Switch the permission preset (sandbox mode + approval policy)",
            handler, input_hint="<preset>")

    # ---------- 投影 ----------

    def _register_projection(self) -> None:
        from ..session_projection import ProjectionDefinition
        registry = self.ctx.get("sessionProjections")
        if registry is None:
            return
        self._projection_disposer = registry.register(ProjectionDefinition(
            "permissions",
            init=lambda header, inherited_event_count: {
                "preset": None, "sandbox": None, "approval": None, "seeded": False},
            apply=self._apply_permission_event,
            state_version=2,
            view=lambda state: {"currentValue": self.derive(state)},
        ))

    @staticmethod
    def _apply_permission_event(state: dict, event: dict) -> dict:
        etype = event["type"]
        data = event.get("data") or {}
        if etype == "permission/preset":
            return {**state, "preset": data.get("preset")}
        if etype == "sandbox/mode":
            return {**state, "sandbox": data.get("mode")}
        if etype == "approval/policy":
            return {**state, "approval": data.get("policy")}
        if etype == "session/end-seed":
            return {**state, "seeded": True}
        return state

    # ---------- 生命周期 ----------

    def dispose(self) -> None:
        for fn in reversed(self._disposers):
            fn()
        self._disposers.clear()


def install_permission_presets(ctx: Context, config: dict | None = None,
                               approval: Any = None) -> PermissionPresetService:
    """装配 ctx.permissionPresets（幂等，重复装返回既有实例）。

    @param approval - 可选 ApprovalService 实例（web 组合通常已在 ctx 提供
    'approval'；此处供未装配审批服务的组合显式注入）。
    """
    existing = ctx.get("permissionPresets")
    if existing is not None:
        return existing
    if approval is not None and ctx.get("approval") is None:
        ctx.provide("approval", approval)
    service = PermissionPresetService(ctx, config)
    return service