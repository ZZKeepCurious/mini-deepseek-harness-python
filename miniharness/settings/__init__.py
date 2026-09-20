"""用户设置（对齐 packages/settings/settings + settings-file）。

- `SettingsProvider`（`ctx.settings`）：插件注册命名空间 schema，读取解析值（schema 默认 →
  注册者 composition `base` → 用户文档 section）；`SettingsScope` 提供 get/watch/update/replace；
  写路径带 monotonic revision，陈旧写报 `SettingsConflictError`（`SETTINGS_CONFLICT`）；
  提交后发 `settings/updated`（深度相等门控）与 `settings/document-updated`。
- `redact_secrets`：按 schema 声明的 secret 位置剥离值、只报 set 状态。
- `SettingsFileProvider`：文件承载原始文档，原子写 + watchdog 监听外部改动重载。

载体差异（登记）：上游 schema 用 schemastery（`role('secret')` + `toJSON` 线视图），mini 以
纯 dict 默认值 + 显式 `secrets` 路径集承载；typert RPC 视图与 sessionProjections 无关面未承载。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.scope import Context, Service

__all__ = [
    "SettingsConflictError",
    "SettingsFileProvider",
    "SettingsProvider",
    "SettingsScope",
    "install_settings",
    "parse_settings_namespace",
    "redact_secrets",
]

_NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")


def parse_settings_namespace(value: str) -> str:
    if not isinstance(value, str) or not _NAMESPACE_PATTERN.match(value):
        raise TypeError(f'settings namespace "{value}" must match {_NAMESPACE_PATTERN.pattern}')
    return value


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _deep_freeze(v) for k, v in value.items()}
    if isinstance(value, list):
        return tuple(_deep_freeze(v) for v in value)
    return value


def deep_equal_json(a: Any, b: Any) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(deep_equal_json(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(deep_equal_json(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def _merge(base: dict, patch: dict, prefix: tuple = ()) -> dict:
    out = _deep_copy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value, prefix + (key,))
        else:
            out[key] = _deep_copy(value)
    return out


def _at_path(section: dict, path: list) -> Any:
    node: Any = section
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _set_path(section: dict, path: list, value: Any) -> None:
    if not path:
        section.clear()
        section.update(_deep_copy(value))
        return
    node = section
    for part in path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[path[-1]] = _deep_copy(value)


def _unset_path(section: dict, path: list) -> None:
    if not path:
        section.clear()
        return
    node = section
    for part in path[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return
    if isinstance(node, dict):
        node.pop(path[-1], None)


class _Missing:
    pass


_MISSING = _Missing()


def redact_secrets(value: Any, secrets: list) -> tuple:
    """剥离 secret 位置的值，返回 (脱敏值, [{'path': [...], 'set': bool}])。"""
    redacted = _deep_copy(value)
    views = []
    for spec in secrets:
        path = list(spec) if isinstance(spec, (list, tuple)) else [spec]
        current = _at_path(redacted, path) if isinstance(redacted, dict) else _MISSING
        if current is not _MISSING:
            _unset_path(redacted, path)
        views.append({"path": path, "set": current is not _MISSING and current is not None})
    return redacted, views


_redact = redact_secrets


class SettingsConflictError(Exception):
    code = "SETTINGS_CONFLICT"

    def __init__(self, namespace: str, expected: int, actual: int):
        super().__init__(
            f"settings namespace '{namespace}' moved: expected revision {expected}, actual {actual}")
        self.namespace = namespace
        self.expected = expected
        self.actual = actual


@dataclass
class _Descriptor:
    ns: str
    defaults: dict
    base: dict | None = None
    applies: str = "live"
    validate: Callable | None = None
    secrets: tuple = ()
    revision: int = 0


@dataclass
class _Listener:
    callback: Callable
    queue: list = field(default_factory=list)


class SettingsScope:
    """某个命名空间的属主句柄（对齐上游 SettingsScope）。"""

    def __init__(self, provider: "SettingsProvider", ns: str):
        self._provider = provider
        self._ns = ns

    def get(self) -> Any:
        return self._provider._resolve(self._ns)

    def watch(self, callback: Callable) -> Callable:
        return self._provider._watch(self._ns, callback)

    async def update(self, patch: dict, *, expected_revision: int | None = None) -> None:
        await self._provider.update(self._ns, patch, expected_revision=expected_revision)

    async def replace(self, section: dict, *, expected_revision: int | None = None) -> None:
        await self._provider.replace(self._ns, section, expected_revision=expected_revision)

    async def mutate(self, ops: list, *, expected_revision: int | None = None) -> None:
        await self._provider.mutate(self._ns, ops, expected_revision=expected_revision)


class SettingsProvider(Service):
    """用户设置能力 seams（`ctx.settings`）。"""

    provide = "settings"

    def __init__(self, ctx: Context):
        super().__init__(ctx, "settings")
        self._descriptors: dict = {}
        self._document: dict = {}
        self._listeners: dict = {}
        self._writable = True
        self._has_document = False

    # ---------- 注册 ----------

    def register(self, ns: str, *, defaults: dict, base: dict | None = None,
                 applies: str = "live", validate: Callable | None = None,
                 secrets=()) -> SettingsScope:
        ns = parse_settings_namespace(ns)
        if ns in self._descriptors:
            raise ValueError(f'settings namespace "{ns}" is already registered')
        descriptor = _Descriptor(ns=ns, defaults=_deep_copy(defaults),
                                 base=_deep_copy(base) if base is not None else None,
                                 applies=applies, validate=validate, secrets=tuple(secrets))
        user = self._document.get(ns)
        if user is not None and validate is not None:
            validate(self._resolve_with(descriptor, user))
        self._descriptors[ns] = descriptor
        self._listeners.setdefault(ns, [])
        return SettingsScope(self, ns)

    # ---------- 解析 ----------

    def _user_section(self, ns: str) -> dict | None:
        section = self._document.get(ns)
        return section if isinstance(section, dict) else None

    def _resolve_with(self, descriptor: _Descriptor, user: dict | None) -> Any:
        value = _deep_copy(descriptor.defaults)
        if descriptor.base is not None:
            value = _merge(value, descriptor.base)
        if user is not None:
            value = _merge(value, user)
        return value

    def _resolve(self, ns: str) -> Any:
        descriptor = self._descriptors.get(ns)
        if descriptor is None:
            raise KeyError(ns)
        return self._resolve_with(descriptor, self._user_section(ns))

    def get(self, ns: str) -> Any:
        return self._resolve(ns)

    # ---------- 观察 ----------

    def _watch(self, ns: str, callback: Callable) -> Callable:
        listener = _Listener(callback=callback)
        self._listeners.setdefault(ns, []).append(listener)
        active = True

        def disposer() -> None:
            nonlocal active
            if not active:
                return
            active = False
            try:
                self._listeners[ns].remove(listener)
            except (KeyError, ValueError):
                pass
        return disposer

    def _emit(self, ns: str, next_value: Any, prev_value: Any, source: str) -> None:
        payload = {"ns": ns, "next": next_value, "prev": prev_value, "source": source}
        if not deep_equal_json(next_value, prev_value):
            for listener in list(self._listeners.get(ns, [])):
                try:
                    listener.callback(next_value, prev_value)
                except Exception:
                    pass
        self.ctx.emit("settings/updated", payload)

    # ---------- 写路径 ----------

    async def _commit(self, ns: str, next_section: dict | None, source: str,
                      expected_revision: int | None) -> None:
        descriptor = self._descriptors[ns]
        if expected_revision is not None and expected_revision != descriptor.revision:
            raise SettingsConflictError(ns, expected_revision, descriptor.revision)
        prev_value = self._resolve(ns)
        next_value = self._resolve_with(descriptor, next_section)
        if descriptor.validate is not None:
            descriptor.validate(next_value)
        if next_section is None:
            self._document.pop(ns, None)
        else:
            self._document[ns] = next_section
        descriptor.revision += 1
        self._persist()
        self.ctx.emit("settings/document-updated", {"ns": ns, "revision": descriptor.revision})
        self._emit(ns, next_value, prev_value, source)

    async def update(self, ns: str, patch: dict, *, expected_revision: int | None = None) -> None:
        if ns not in self._descriptors:
            raise KeyError(ns)
        current = self._user_section(ns) or {}
        await self._commit(ns, _merge(current, patch), "update", expected_revision)

    async def replace(self, ns: str, section: dict, *, expected_revision: int | None = None) -> None:
        if ns not in self._descriptors:
            raise KeyError(ns)
        await self._commit(ns, _deep_copy(section) if section else {}, "update", expected_revision)

    async def mutate(self, ns: str, ops: list, *, expected_revision: int | None = None) -> None:
        if ns not in self._descriptors:
            raise KeyError(ns)
        section = self._user_section(ns) or {}
        for op in ops:
            if op.get("op") == "set":
                _set_path(section, list(op.get("path", [])), op.get("value"))
            elif op.get("op") == "unset":
                _unset_path(section, list(op.get("path", [])))
            else:
                raise ValueError(f"unknown settings op: {op.get('op')}")
        await self._commit(ns, section, "update", expected_revision)

    # ---------- 描述 ----------

    def describe(self, *, redact_secrets: bool = False) -> dict:
        namespaces = []
        for ns, descriptor in self._descriptors.items():
            value = self._resolve(ns)
            base = _deep_copy(descriptor.base) if descriptor.base is not None else None
            user = self._user_section(ns)
            entry = {
                "ns": ns,
                "value": value,
                "applies": descriptor.applies,
                "revision": descriptor.revision,
            }
            if base is not None:
                entry["base"] = base
            if user is not None:
                entry["user"] = _deep_copy(user)
            if redact_secrets:
                entry["value"], secrets = _redact(entry["value"], list(descriptor.secrets))
                if base is not None:
                    entry["base"], _ = _redact(base, list(descriptor.secrets))
                if user is not None:
                    entry["user"], _ = _redact(user, list(descriptor.secrets))
                entry["secrets"] = secrets
            namespaces.append(entry)
        return {"writable": self._writable, "hasDocument": self._has_document,
                "namespaces": namespaces}

    # ---------- 持久化钩子 ----------

    def _persist(self) -> None:
        pass

    def _load_document(self, document: dict) -> None:
        self._document = document if isinstance(document, dict) else {}
        self._has_document = True


class SettingsFileProvider(SettingsProvider):
    """文件承载的设置 provider：JSON 文档 + 原子写 + watchdog 外部改动重载。"""

    def __init__(self, ctx: Context, path: str):
        super().__init__(ctx)
        self.path = path
        self._watch_observer = None
        self._reload()

    def _reload(self) -> None:
        try:
            raw = pathlib.Path(self.path).read_text(encoding="utf-8")
        except FileNotFoundError:
            self._load_document({})
            return
        self._load_document(json.loads(raw) if raw.strip() else {})

    def _persist(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        pathlib.Path(tmp).write_text(
            json.dumps(self._document, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def watch_file(self) -> None:
        if self._watch_observer is not None:
            return
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        provider = self

        class _Handler(FileSystemEventHandler):
            def on_modified(self, event):
                if os.path.abspath(event.src_path) == os.path.abspath(provider.path):
                    provider.reload_from_provider()

        def setup():
            observer = Observer()
            observer.schedule(_Handler(), os.path.dirname(os.path.abspath(self.path)) or ".")
            observer.daemon = True
            observer.start()
            self._watch_observer = observer
            return observer.stop

        self.ctx.effect(setup, label="settings-file-watch")

    def reload_from_provider(self) -> None:
        prev = {ns: self._resolve(ns) for ns in self._descriptors}
        self._reload()
        for ns, descriptor in self._descriptors.items():
            descriptor.revision += 1
            next_value = self._resolve(ns)
            self.ctx.emit("settings/document-updated", {"ns": ns, "revision": descriptor.revision})
            self._emit(ns, next_value, prev.get(ns), "provider")


def install_settings(ctx: Context, *, path: str | None = None) -> SettingsProvider:
    """幂等装配 settings provider；给 path 则用文件承载并监听外部改动。"""
    existing = ctx.get("settings")
    if existing is not None:
        return existing
    if path is None:
        return SettingsProvider(ctx)
    provider = SettingsFileProvider(ctx, path)
    provider.watch_file()
    return provider
