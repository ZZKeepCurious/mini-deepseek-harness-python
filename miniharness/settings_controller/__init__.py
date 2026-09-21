"""settings / credentials Remote 控制器（对齐 packages/api/settings-controller）。

承载两个 namespace 属主：
  * `SettingsController`（`ctx.settingsController`）：`settings` namespace——脱敏读
    （`describe`）、三种写（`update`/`replace`/`mutate`）、原生文档/预设目录打开；
  * `CredentialsController`（`ctx.credentialsController`）：`credentials` namespace——
    引用名的批量 `describe`、`set`/`unset`（值只进不出）。

两个 namespace 在 provider 缺席时仍注册，并在调用时给出可操作的配置错误。所有
设置读使用 `redact_secrets`，secret 字段不随响应出行。

载体差异（登记）：
  * minh 无原生桌面打开器（`native-command` 无对应物）——`canOpenAgentPresetDirectory`
    恒 False、`openSettingsDocument`/预设目录打开返回 gateway/internal 或
    `{opened:false, path}`（如实标注部署能力，不臆造 opener）。
  * 上游 schema 由 schemastery 序列化进入 namespace 视图；mini 无 schema 面，
    `schema` 以 `{}` 占位（表单渲染载体差异）。
  * 上游写路径为 Promise；mini settings 服务 async（同步体），经常驻循环驱动。
"""
from __future__ import annotations

import os
import re
from typing import Any

from ..core.agent_loop.resident_loop import run_on_resident
from ..core.scope import Context, Service
from ..settings import SettingsConflictError

__all__ = [
    "CredentialsController",
    "SettingsController",
    "SettingsFault",
    "install_settings_controller",
]

#: credentials.describe 单次批量上限（credentials.ts:20）。
MAX_DESCRIBE_REFS = 64

_REF_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SettingsFault(Exception):
    """带稳定配置域码的控制器失败（对齐上游 RemoteError）。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _namespace_view(entry: dict) -> dict:
    """把一个脱敏描述符投影为其 wire 视图（逐字段，index.ts namespaceView）。"""
    view: dict[str, Any] = {
        "ns": entry["ns"],
        "schema": entry.get("schema", {}),
        "value": entry.get("value"),
        "applies": entry.get("applies"),
        "secrets": [{"path": list(secret["path"]), "set": secret["set"]}
                    for secret in entry.get("secrets", [])],
        "revision": entry.get("revision"),
    }
    if "base" in entry:
        view["base"] = entry["base"]
    if "user" in entry:
        view["user"] = entry["user"]
    return view


class SettingsController(Service):
    """`ctx.remote.settings` 背后的 Host 服务（index.ts SettingsController）。"""

    provide = "settingsController"

    def __init__(self, ctx: Context, roster: Any = None, native_open: bool = False):
        self.roster = roster
        self.native_open = bool(native_open)
        super().__init__(ctx, "settingsController")

    # ---------- 读面 ----------

    def describe(self) -> dict:
        settings = self._provider()
        described = settings.describe(redact_secrets=True)
        return {
            "writable": described["writable"],
            "hasDocument": described["hasDocument"],
            "namespaces": [_namespace_view(entry) for entry in described["namespaces"]],
        }

    def can_open_agent_preset_directory(self) -> bool:
        return self.native_open

    # ---------- 写面 ----------

    def update(self, ns: str, patch: Any, expected_revision: Any = None) -> dict:
        return self._write(ns, "update", patch, expected_revision)

    def replace(self, ns: str, section: Any, expected_revision: Any = None) -> dict:
        return self._write(ns, "replace", section, expected_revision)

    def mutate(self, ns: str, ops: Any, expected_revision: Any = None) -> dict:
        return self._write(ns, "mutate", ops, expected_revision)

    def _write(self, ns: Any, mode: str, payload: Any, expected_revision: Any) -> dict:
        if not isinstance(ns, str) or ns == "":
            raise SettingsFault("gateway/bad-request",
                                f"invalid payload for settings.{mode}", {})
        settings = self._provider()
        try:
            if mode == "update":
                run_on_resident(settings.update(ns, payload, expected_revision=expected_revision))
            elif mode == "replace":
                run_on_resident(settings.replace(ns, payload, expected_revision=expected_revision))
            else:
                run_on_resident(settings.mutate(ns, payload, expected_revision=expected_revision))
        except SettingsConflictError as error:
            raise SettingsFault("settings/conflict", str(error),
                                {"ns": ns, "expected": error.expected,
                                 "actual": error.actual}) from error
        except Exception as error:  # noqa: BLE001 - 其余拒绝一律 settings/rejected
            raise SettingsFault("settings/rejected", str(error), {"ns": ns}) from error
        described = settings.describe(redact_secrets=True)
        entry = next((candidate for candidate in described["namespaces"]
                      if candidate["ns"] == ns), None)
        if entry is None:
            raise SettingsFault(
                "gateway/internal", f'settings namespace "{ns}" was disposed after the {mode}', {})
        return _namespace_view(entry)

    # ---------- 文档 / 预设目录 ----------

    def open_settings_document(self) -> dict:
        settings = self._provider()
        try:
            path = settings.prepare_document()
        except Exception as error:  # noqa: BLE001 - 准备失败折 gateway/internal
            raise SettingsFault("gateway/internal",
                                f"settings document preparation failed: {error}", {}) from error
        if path is None:
            raise SettingsFault("gateway/internal",
                                "settings provider has no local document to open", {})
        raise SettingsFault(
            "gateway/internal",
            "path open failed: no native desktop opener is available in this deployment",
            {})

    def open_agent_preset_directory(self, agent_preset: Any) -> dict:
        if not isinstance(agent_preset, str) or agent_preset == "":
            raise SettingsFault("gateway/bad-request",
                                "agent preset id must not be empty", {})
        if self.roster is None:
            raise SettingsFault("agent-preset/not-found",
                                "this deployment composes no agent presets",
                                {"agentPreset": agent_preset, "available": []})
        from ..preset.presets import UnknownPresetError
        try:
            preset = self.roster.resolve(agent_preset)
        except UnknownPresetError as error:
            raise SettingsFault("agent-preset/not-found", str(error),
                                {"agentPreset": agent_preset,
                                 "available": list(self.roster.ids())}) from error
        if preset.trust != "user":
            raise SettingsFault(
                "agent-preset/read-only",
                f'agent-presets: preset "{preset.id}" cannot be written: it ships with the deployment',
                {"agentPreset": preset.id, "reason": "it ships with the deployment"})
        directory = os.path.dirname(str(preset.path))
        if not self.native_open:
            return {"opened": False, "path": directory}
        raise SettingsFault(
            "gateway/internal",
            "path open failed: no native desktop opener is available in this deployment", {})

    # ---------- 内部 ----------

    def _provider(self):
        settings = self.ctx.get("settings")
        if settings is None:
            raise SettingsFault(
                "gateway/internal",
                "settings service is absent: this deployment does not mount a settings "
                "provider (e.g. miniharness.settings SettingsFileProvider) in its composition",
                {})
        return settings


class CredentialsController(Service):
    """`ctx.remote.credentials` 背后的 Host 服务（credentials.ts CredentialsController）。"""

    provide = "credentialsController"

    def __init__(self, ctx: Context):
        super().__init__(ctx, "credentialsController")

    def describe(self, refs: Any) -> dict:
        if (not isinstance(refs, list) or len(refs) > MAX_DESCRIBE_REFS
                or any(not isinstance(ref, str) or not _REF_PATTERN.match(ref) for ref in refs)):
            raise SettingsFault("gateway/bad-request",
                                "invalid payload for credentials.describe", {})
        credentials = self._provider()
        result: dict[str, dict] = {}
        for ref in refs:
            info = credentials.describe(ref)
            view: dict[str, Any] = {"configured": info["configured"]}
            if info.get("source") is not None:
                view["source"] = info["source"]
            view["writable"] = info["writable"]
            result[ref] = view
        return result

    def set(self, ref: Any, value: Any) -> None:
        if not isinstance(ref, str) or not _REF_PATTERN.match(ref):
            raise SettingsFault("gateway/bad-request",
                                "invalid payload for credentials.set", {})
        if not isinstance(value, str) or value == "":
            raise SettingsFault("gateway/bad-request",
                                "invalid payload for credentials.set", {})
        credentials = self._provider()
        self._write(ref, lambda: credentials.set(ref, value))

    def unset(self, ref: Any) -> None:
        if not isinstance(ref, str) or not _REF_PATTERN.match(ref):
            raise SettingsFault("gateway/bad-request",
                                "invalid payload for credentials.unset", {})
        credentials = self._provider()
        self._write(ref, lambda: credentials.unset(ref))

    @staticmethod
    def _write(ref: str, write) -> None:
        try:
            write()
        except Exception as error:  # noqa: BLE001 - 一切拒绝折 credential/rejected
            raise SettingsFault("credential/rejected", str(error), {"ref": ref}) from error

    def _provider(self):
        credentials = self.ctx.get("credentials")
        if credentials is None:
            raise SettingsFault(
                "gateway/internal",
                "credentials service is absent: this deployment does not mount a credential "
                "provider (e.g. miniharness.seams.credentials_local) in its composition",
                {})
        return credentials


def install_settings_controller(ctx: Context, roster: Any = None,
                                native_open: bool = False) -> SettingsController:
    """幂等装配 `ctx.settingsController` + `ctx.credentialsController`。"""
    existing = ctx.get("settingsController")
    if existing is not None:
        return existing
    controller = SettingsController(ctx, roster=roster, native_open=native_open)
    if ctx.get("credentialsController") is None:
        CredentialsController(ctx)
    return controller
