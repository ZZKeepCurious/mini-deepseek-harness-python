"""rc.1 settings 核心：SettingsForms —— profile 条目驱动的配置表单。

对齐上游 rc.1 `packages/settings/settings/src/index.ts`（SettingsForms，431 行）。

语义（与上游一致）：
- **无注册机制**：命名空间 = `configEditor.entries()` 中唯一 profile entry id
  （不再有「注册命名空间 schema」概念）；schema 来自
  `entry.fiber.runtime.Config`（schemastery，mini 插件无 Config 时用宽松占位）。
- **读**：`describe()` 遍历 `configEditor.configuration()`（`{entry, inherited,
  override}`）——value = 条目活配置、base = inherited 层、user = profile patch
  override 层；revision 基于（fiber.uid + schema + config）变化追踪；每次变化
  emit `settings/document-updated`。
- **写**：`update`/`replace`/`mutate` 经 `configEditor.edit(entry, change)` 持久化
  到 profile `cordis.patch.yml`（filelock + reconcile + 原子写 + HMR runExclusive）。
- **configure({auto}, owner)**：页面策略（preset-registry 用它把自己挡在自动生成
  页之外）。
- **legacy 导入**：`<profile.home>/settings.yaml` → 首次写前 rename `.imported`，
  逐 section 映射到条目 id 后 update；被组合拒绝的节留在改名文件里。
- **documentPath** = `configEditor.documentPath`（profile patch 即唯一文档）。

载体差异（登记）：
- mini 插件配置是 dict（无 schemastery Config 声明）——schema 面有则投影
  （`runtime.Config.to_json`）、无则 `{}` 占位（settings-controller 既有惯例）；
  无 volatile 概念，表单即整条配置。
- `SettingsProvider`（注册式 + settings.json 文档）保留为无 config-editor 装配的
  既有载体；生产装配在 ctx 已 boot（有 configEditor）时改用本核心。
"""
from __future__ import annotations

import os
from typing import Any, Callable

import yaml

from ..core.scope import Context, FiberState, Service
from . import (
    SettingsConflictError,
    _deep_copy,
    _merge,
    _set_path,
    _unset_path,
    redact_secrets,
)

__all__ = [
    "SettingsForms",
    "install_settings_forms",
]

#: 已移除的 settings.yaml section → 目标 entry id（上游 index.ts:201-206）。
LEGACY_SECTION_ENTRIES: dict[str, str] = {
    "ui-developer-tools": "ui-settings",
    "ui-onboarding": "ui-settings-general",
    "shell": "pwsh-sandbox" if os.name == "nt" else "bash-sandbox",
}


def _collect_secret_paths(schema: Any, prefix: tuple = ()) -> list:
    """从 schemastery schema 遍历收集 `role('secret')` 路径（对齐 redact.ts walk）。

    上游 `redactSecrets(schema, value)` 把 role('secret') 字段从值剥离；mini 的
    `redact_secrets(value, paths)` 需要路径集。mini 插件无 Config 时返回空。
    """
    paths: list = []
    if schema is None or not hasattr(schema, "meta"):
        return paths
    if schema.meta.get("role") == "secret":
        paths.append(list(prefix))
    for key, child in (schema.dict or {}).items():
        paths.extend(_collect_secret_paths(child, prefix + (key,)))
    if getattr(schema, "inner", None) is not None:
        paths.extend(_collect_secret_paths(schema.inner, prefix))
    for child in schema.list or []:
        paths.extend(_collect_secret_paths(child, prefix))
    return paths


def _schema_json(schema: Any) -> Any:
    """schema 的线视图：schemastery 实例 to_json；无 schema → {}（占位）。"""
    if schema is not None and callable(getattr(schema, "to_json", None)):
        try:
            return schema.to_json()
        except Exception:  # noqa: BLE001 - 投影失败退宽松占位
            return {}
    return {}


class SettingsForms(Service):
    """`ctx.settings` 的 rc.1 核心：读 configEditor 条目 + 写经 config-editor。

    构造要求 ctx 已装配 `configEditor`（boot 过的上下文）；缺则 fail loud（说明
    装配顺序：先 boot/profile + `install_config_editor`，再本服务）。
    """

    provide = "settings"

    def __init__(self, ctx: Context, config_editor: Any, profile_home: str | None = None):
        super().__init__(ctx, "settings")
        if config_editor is None:
            raise RuntimeError(
                "SettingsForms requires ctx.configEditor (boot/profile context); "
                "install config-editor before settings-forms")
        self._config_editor = config_editor
        self._profile_home = profile_home
        self._revisions: dict[str, dict] = {}
        self._presentations: dict[Any, dict] = {}
        self._closed = False
        self.ctx.on("app-boot/config-reload", lambda _payload: self._invalidate())
        self._import_legacy_document()

    # ---------- 页面策略 ----------

    def configure(self, presentation: dict, owner: Any = None) -> Callable:
        """注册调用插件实例的页面策略（对齐 configure({auto}, owner)）。

        同一 fiber 重复注册 → 抛错；返回 disposer（删除策略并 invalidate）。
        """
        fiber = owner if owner is not None else self.ctx.fiber
        if fiber in self._presentations:
            raise RuntimeError("Settings presentation is already configured for this plugin instance")
        policy = dict(presentation)
        self._presentations[fiber] = policy
        self._invalidate()

        def disposer() -> None:
            if self._presentations.get(fiber) is not policy:
                return
            self._presentations.pop(fiber, None)
            self._invalidate()

        return disposer

    def _invalidate(self) -> None:
        if self._closed or self.ctx.fiber.state != FiberState.ACTIVE:
            return
        try:
            self.describe()
        except Exception:  # noqa: BLE001 - 描述失败记录不抛出（对齐上游 invalidate）
            pass

    # ---------- 文档面 ----------

    @property
    def writable(self) -> bool:
        return True

    @property
    def document_path(self) -> str | None:
        return self._config_editor.document_path

    def prepare_document(self) -> str | None:
        """profile patch 即文档，恒存在（上游 prepareDocument 直接返回路径）。"""
        return self.document_path

    # ---------- 读面 ----------

    def describe(self, *, redact_secrets: bool = False) -> dict:
        """profile 活动条目的表单投影（对齐上游 describe）。

        命名空间 = configEditor.entries() 唯一 id；无 schema 的条目仍投影
        （value/base/user 三层，schema 占位 {}）。返回 SettingsProvider 同形态
        `{writable, hasDocument, namespaces}`（settings-controller 消费面不变）。
        """
        active: set[str] = set()
        namespaces: list[dict] = []
        for row in self._config_editor.configuration():
            entry = row["entry"]
            entry_id = entry.options.get("id")
            if not isinstance(entry_id, str) or entry_id == "":
                continue
            fiber = getattr(entry, "fiber", None)
            runtime = getattr(fiber, "runtime", None) if fiber is not None else None
            schema = runtime.get("Config") if isinstance(runtime, dict) else None
            active.add(entry_id)
            raw = _raw_key(entry, schema)
            auto = self._presentations.get(fiber, {}).get("auto", True)
            previous = self._revisions.get(entry_id)
            revision = 0 if previous is None else previous["revision"] + (
                0 if previous["raw"] == raw else 1)
            self._revisions[entry_id] = {"raw": raw, "revision": revision}
            if previous is None or previous["raw"] != raw or previous.get("auto") != auto:
                self.ctx.emit("settings/document-updated",
                              {"ns": entry_id, "revision": revision})
            value = _deep_copy(entry.options.get("config") or {})
            base = _deep_copy(row.get("inherited") or {})
            user = _deep_copy(row.get("override") or {})
            secrets = _collect_secret_paths(schema)
            namespace: dict[str, Any] = {
                "ns": entry_id,
                "autoGenerate": auto,
                "schema": _schema_json(schema),
                "value": value,
                "applies": "live",
                "revision": revision,
            }
            if base:
                namespace["base"] = base
            if user:
                namespace["user"] = user
            if redact_secrets:
                namespace["value"], views = redact_secrets(value, secrets)
                if "base" in namespace:
                    namespace["base"], _ = redact_secrets(namespace["base"], secrets)
                if "user" in namespace:
                    namespace["user"], _ = redact_secrets(namespace["user"], secrets)
                namespace["secrets"] = views
            namespaces.append(namespace)
        for entry_id, previous in list(self._revisions.items()):
            if entry_id in active or previous.get("raw") is None:
                continue
            revision = previous["revision"] + 1
            self._revisions[entry_id] = {**previous, "raw": None, "revision": revision}
            self.ctx.emit("settings/document-updated", {"ns": entry_id, "revision": revision})
        return {"writable": True, "hasDocument": True, "namespaces": namespaces}

    # ---------- 写面 ----------

    async def update(self, ns: str, patch: Any, *, expected_revision: int | None = None) -> None:
        self._write(ns, lambda current, inherited: _merge(current, patch),
                    expected_revision)

    async def replace(self, ns: str, section: Any, *, expected_revision: int | None = None) -> None:
        self._write(ns, lambda current, inherited: _deep_copy(section) if section else {},
                    expected_revision)

    async def mutate(self, ns: str, ops: Any, *, expected_revision: int | None = None) -> None:
        def apply(current: dict, inherited: dict) -> dict:
            section = _deep_copy(current)
            for op in ops:
                if op.get("op") == "set":
                    _set_path(section, list(op.get("path", [])), op.get("value"))
                elif op.get("op") == "unset":
                    _unset_path(section, list(op.get("path", [])))
                else:
                    raise ValueError(f"unknown settings op: {op.get('op')}")
            return section

        self._write(ns, apply, expected_revision)

    def _write(self, ns: str, change: Callable[[dict, dict], dict],
               expected_revision: int | None) -> None:
        entry = self._find_entry(ns)
        if entry is None:
            raise KeyError(ns)
        if getattr(entry, "fiber", None) is None:
            raise RuntimeError(f'Plugin entry "{ns}" is no longer active')

        def do_edit(current: dict, inherited: dict) -> dict:
            descriptor = next((d for d in self.describe()["namespaces"] if d["ns"] == ns), None)
            if descriptor is None:
                raise RuntimeError(f'Plugin entry "{ns}" is no longer configurable')
            if expected_revision is not None and descriptor["revision"] != expected_revision:
                raise SettingsConflictError(ns, expected_revision, descriptor["revision"])
            return change(current, inherited)

        self._config_editor.edit(entry, do_edit)
        self.describe()

    # ---------- legacy 导入 ----------

    def _import_legacy_document(self) -> None:
        """把已移除的 settings.yaml section 导入活动 profile（对齐上游 importLegacyDocument）。

        文档在首次写前改名 `.imported`（部分导入不重复）；逐 section 映射到条目
        id 后 update；被组合拒绝的节记录并留在改名文件里。
        """
        if not self._profile_home:
            return
        path = os.path.join(self._profile_home, "settings.yaml")
        if not os.path.exists(path):
            return
        imported = path + ".imported"
        os.replace(path, imported)
        with open(imported, encoding="utf-8") as f:
            try:
                sections = yaml.safe_load(f)
            except Exception as error:  # noqa: BLE001 - 解析失败不阻断
                return
        for section, values in (sections or {}).items():
            ns = LEGACY_SECTION_ENTRIES.get(section, section)
            if not isinstance(values, dict):
                continue
            try:
                self._write(ns, lambda current, inherited: _merge(current, values),
                            expected_revision=None)
            except Exception:  # noqa: BLE001 - 被组合拒绝的节留在改名文件里
                continue

    # ---------- 内部 ----------

    def _find_entry(self, ns: str) -> Any | None:
        for entry in self._config_editor.entries():
            if entry.options.get("id") == ns:
                return entry
        return None


def _raw_key(entry: Any, schema: Any) -> str:
    """revision 追踪的 raw 键：fiber uid + schema + config（对齐上游 raw）。"""
    import json
    fiber = getattr(entry, "fiber", None)
    uid = getattr(fiber, "uid", None) if fiber is not None else None
    schema_json = _schema_json(schema)
    return json.dumps([uid, schema_json, entry.options.get("config") or {}],
                      sort_keys=True, default=str)


def install_settings_forms(ctx: Context, *, config_editor: Any,
                           profile_home: str | None = None) -> SettingsForms:
    """幂等装配 `ctx.settings` 为 SettingsForms（rc.1 核心）。

    @param config_editor ctx 上已装配的 ConfigEditor（boot/profile 上下文）。
    @param profile_home profile 目录（legacy settings.yaml 导入的定位）。
    """
    existing = ctx.get("settings")
    if isinstance(existing, SettingsForms):
        return existing
    forms = SettingsForms(ctx, config_editor, profile_home=profile_home)
    return forms