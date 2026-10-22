"""当前 profile 的插件启用管理（对齐 packages/boot/plugin-manager/src/index.ts）。

mini 载体只覆盖 Loader 条目的**列举与启用/停用**：
  * `listPlugins()` → `PluginInfo[]`（inventory 四字段 + `patchId` 或
    `readOnlyReason`，对齐上游 index.ts:291-313）；
  * `setPluginEnabled(id, enabled)` → `ChangeResult`：把 `disabled` 意图写入
    profile 的 `cordis.patch.yml`，经 `reconcileProfilePatches` 正常路径应用，
    失败回滚文件（对齐上游 patch.ts `writePluginEnabled` + index.ts:491-502）。

载体差异（登记）：
  * 安装/卸载/市场/版本豁免依赖 npm/pnpm/GitHub 与 package.json 锁文件，
    mini 无 bundle 解析载体 —— 这些方法不提供（`profile.py` 已把 profile
    manifest 的 bundles 全量记为 `skipped_bundles`）。
  * 上游 `profileContext`（`profile.dir`/`patchPath`/`installAnchor`）在 mini
    由 `ConfigEditor` 的 profile 目录与 patch 路径承担。
  * 上游 `hmr` 缺失时返回 `restart-required`；mini 的 Loader 对账总是同步
    应用，故成功即 `applied`。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from ..core.scope import Context, FiberState, Service
from .boot import reconcile_profile_patches
from .config_editor import (
    DOCUMENT_LOCK_WAIT_SECONDS,
    ConfigEditor,
    _flatten,
    _round_trip_dump,
    _write_atomic,
)
from .profile import compose_entries, read_profile_patches

__all__ = [
    "ManagementFailure",
    "PluginManager",
    "install_plugin_manager",
]


class ManagementFailure(Exception):
    """可预期的管理拒绝（对齐上游 ManagementFailure：code 进 ChangeResult.error）。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _row_module(row: dict) -> Any:
    return row.get("name") or row.get("module")


class PluginManager(Service):
    """`ctx.pluginManager`：当前 profile 的插件启用管理服务。"""

    provide = "pluginManager"

    def __init__(self, ctx: Context, editor: ConfigEditor):
        super().__init__(ctx, "pluginManager")
        self._editor = editor

    # ---------- Remote 方法 ----------

    def list_plugins(self) -> list[dict]:
        """当前 Loader 条目 + 可持久化的 patch 目标（对齐上游 listPlugins）。"""
        loader = self.ctx.get("loader")
        if loader is None:
            return []
        rows = self._composed_rows()
        plugins: list[dict] = []
        for entry in loader.entries():
            if entry.options.get("group"):
                continue
            module_name = _row_module(entry.options)
            item = {
                "entryId": entry.id,
                "moduleName": module_name,
                "enabled": not entry.disabled,
                "fiberPhase": (None if entry.fiber is None
                               or entry.fiber.state == FiberState.DISPOSED
                               else entry.fiber.state),
            }
            candidates = [row for row in rows if row.get("id") == entry.options.get("id")]
            candidate = candidates[0] if len(candidates) == 1 else None
            if (candidate is None
                    or _row_module(candidate) != module_name
                    or not self._root_include_owned(entry)):
                item["readOnlyReason"] = "unaddressable"
            else:
                item["patchId"] = candidate.get("id")
            plugins.append(item)
        return plugins

    def set_plugin_enabled(self, entry_id: str, enabled: bool) -> dict:
        """写入并应用一个条目的启用意图（对齐上游 setPluginEnabled）。"""
        result: dict = {
            "changed": False,
            "application": "applied",
            "stage": "enable",
            "target": entry_id,
            "enabled": enabled,
        }
        try:
            row = next((item for item in self.list_plugins()
                        if item["entryId"] == entry_id), None)
            if row is None:
                raise ManagementFailure("unknown-plugin")
            if "readOnlyReason" in row:
                raise ManagementFailure(row["readOnlyReason"])
            result["changed"] = self._write_enabled(
                row["patchId"], row["moduleName"], enabled)
            current = next((item for item in self.list_plugins()
                            if item["entryId"] == entry_id), None)
            if current is None or current["enabled"] != enabled:
                result["application"] = "overridden"
        except ManagementFailure as error:
            result["application"] = "failed"
            result["error"] = {"code": error.code}
        except Exception as error:  # noqa: BLE001 - 意外失败折算 operation-error
            result["application"] = "failed"
            result["error"] = {"code": "operation-error", "diagnostic": str(error)}
        finally:
            self.ctx.emit("plugin-manager/changed", {"reason": "plugin"})
        return result

    # ---------- 内部 ----------

    def _loaded_profile(self):
        return self._editor._loaded_profile()

    def _composed_rows(self) -> list[dict]:
        loaded = self._loaded_profile()
        patches = read_profile_patches(
            "miniharness", loaded, home=self._editor._home, overlays=None)
        base_rows = self._editor._include_base_rows(
            self._editor._root_include_entry())
        return _flatten(compose_entries([patches], base_rows))

    @staticmethod
    def _root_include_owned(entry: Any) -> bool:
        parent = getattr(entry, "parent", None)
        tree = getattr(parent, "tree", None) if parent is not None else None
        owner = getattr(tree, "ctx", None) if tree is not None else None
        fiber = getattr(owner, "fiber", None) if owner is not None else None
        include_entry = getattr(fiber, "entry", None) if fiber is not None else None
        return getattr(include_entry, "id", None) == "include"

    def _write_enabled(self, patch_id: str, module_name: str, enabled: bool) -> bool:
        lock_path = os.path.join(self._editor._profile_dir, "package.json")
        try:
            with FileLock(lock_path + ".lock", timeout=DOCUMENT_LOCK_WAIT_SECONDS):
                return self._write_enabled_locked(patch_id, module_name, enabled)
        except Timeout:
            raise RuntimeError(
                f"plugin-manager: timed out acquiring profile lock: {lock_path}") from None

    def _write_enabled_locked(self, patch_id: str, module_name: str,
                              enabled: bool) -> bool:
        path = self._editor.document_path
        try:
            before = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            before = "[]\n"
        from ruamel.yaml import YAML
        yaml_rt = YAML()
        yaml_rt.preserve_quotes = True
        document = yaml_rt.load(before) if before.strip() else []
        if document is None:
            document = []
        if not isinstance(document, list):
            raise RuntimeError("Profile patch must be a YAML sequence")
        index = self._find_patch_row(document, patch_id, module_name)
        if index < 0:
            document.append({"id": patch_id, "disabled": not enabled})
        else:
            document[index]["disabled"] = not enabled
        buffer = _round_trip_dump(document)

        loaded = self._loaded_profile()
        from .profile import Profile
        edited = Profile(name=loaded.name, dir=loaded.dir, layers=loaded.layers,
                         patch_path=loaded.patch_path, patches=document,
                         skipped_bundles=loaded.skipped_bundles)
        effective_patches = read_profile_patches(
            "miniharness", edited, home=self._editor._home, overlays=None)
        base_rows = self._editor._include_base_rows(
            self._editor._root_include_entry())
        row = next((candidate for candidate in _flatten(
            compose_entries([effective_patches], base_rows))
            if candidate.get("id") == patch_id), None)
        if (row.get("disabled") or False) != (not enabled):
            raise RuntimeError(
                f'Enablement for "{patch_id}" is overridden by a home patch '
                "or command-line overlay")
        if buffer == before:
            return False
        _write_atomic(path, buffer)
        try:
            reconcile_profile_patches(
                self.ctx, effective_patches, bin_name="miniharness",
                required_ids=[patch_id] if enabled else None)
        except Exception:
            _write_atomic(path, before)
            reconcile_profile_patches(
                self.ctx,
                read_profile_patches("miniharness", loaded, home=self._editor._home,
                                     overlays=None),
                bin_name="miniharness")
            raise
        return True

    @staticmethod
    def _find_patch_row(document: list, patch_id: str, module_name: str) -> int:
        for index in range(len(document) - 1, -1, -1):
            row = document[index]
            if not isinstance(row, dict):
                continue
            if row.get("id") != patch_id or "insert" in row:
                continue
            if "name" not in row or row.get("name") == module_name:
                return index
        return -1


def install_plugin_manager(ctx: Context, config_editor: ConfigEditor) -> PluginManager:
    """幂等装配 `ctx.pluginManager`（须在 `install_config_editor` 之后）。"""
    existing = ctx.get("pluginManager")
    if existing is not None:
        return existing
    return PluginManager(ctx, config_editor)
