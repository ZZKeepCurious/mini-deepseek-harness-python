"""迷你 StandardSchema（~standard）子集：插件配置校验。

对应 dsh 真实源码：vendor/cordis/src/fiber.ts 的 `resolveConfig` +
@standard-schema/spec 协议（上游插件 Config 用 schemastery 构建 schema）。
mini 以 stdlib 实现协议的读取面：任何实现 `schema["~standard"].validate(value)`
的对象都可用；本模块提供极简 `S` 构建器（教学子集，schemastery 全量不承载）。

协议契约（标准-schema v1）：
  validate(value) -> {"value": ...} 校验通过（含归一化结果）
  validate(value) -> {"issues": [...]} 校验失败，issue 形如
                     {"message": str, "path": [str|int, ...]}
"""
from __future__ import annotations

from typing import Any, Callable

__all__ = ["S", "ValidationError", "resolve_config", "validate_schema_value"]


class ValidationError(TypeError):
    """配置校验失败：聚合 issue 消息（对齐 upstream fiber.ts ValidationError）。

    消息形如 `invalid config:\\n  - <message> (at <path>)`。
    """

    name = "ValidationError"

    def __init__(self, issues: list[dict]):
        lines = []
        for issue in issues:
            message = issue.get("message", "invalid")
            path = issue.get("path")
            if path:
                lines.append(f"  - {message} (at {'.'.join(str(p) for p in path)})")
            else:
                lines.append(f"  - {message}")
        super().__init__("invalid config:\n" + "\n".join(lines))
        self.issues = issues


def _validate(schema: Any, value: Any) -> dict:
    """按 ~standard 协议校验。"""
    standard = schema["~standard"]
    return standard.validate(value)


def resolve_config(schema: Any, config: Any) -> Any:
    """校验并归一化插件配置；无 schema 原样返回（对齐 fiber.ts resolveConfig）。

    异步校验不受支持（与上游一致，抛 TypeError）。
    """
    if schema is None:
        return config
    result = _validate(schema, config)
    if result.get("issues"):
        raise ValidationError(result["issues"])
    return result.get("value", config)


def validate_schema_value(schema: Any, value: Any) -> Any:
    """低层校验入口：返回归一化值，失败抛 ValidationError。"""
    return resolve_config(schema, value)


class _Schema:
    """极简 schema 构建器（教学子集；实现 ~standard 协议读取面）。"""

    __slots__ = ("_validate", "_label", "_default")

    def __init__(self, validate: Callable[[Any], dict], label: str,
                 default: Any = None, has_default: bool = False):
        self._validate = validate
        self._label = label
        self._default = default
        self._has_default = has_default

    def __getitem__(self, key: str) -> "_Schema":
        if key != "~standard":
            raise KeyError(key)
        return self

    def validate(self, value: Any) -> dict:
        if self._has_default and value is None:
            value = self._default
        return self._validate(value)

    # ----- 组合 -----

    def optional(self) -> "_Schema":
        """可缺省：None 通过（无默认值语义）。"""
        return _Schema(
            lambda v: self._validate(v) if v is not None else {"value": None},
            f"{self._label}?",
        )

    def default(self, value: Any) -> "_Schema":
        """None → 默认值（schemastery 的 .default 语义）。"""
        return _Schema(self._validate, f"{self._label}={value!r}",
                       default=value, has_default=True)

    def array(self, item: "_Schema | None" = None) -> "_Schema":
        item = item or S.any()
        return _Schema(
            lambda v: _validate_list(v, item, self._label),
            f"{self._label}[]",
        )

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return f"<S.{self._label}>"


def _issue(message: str, path: list = None) -> dict:
    return {"message": message, "path": path or []}


def _validate_list(value: Any, item: _Schema, label: str) -> dict:
    if not isinstance(value, list):
        return {"issues": [_issue(f"{label}: expected an array", )]}
    for idx, entry in enumerate(value):
        result = item.validate(entry)
        if result.get("issues"):
            issues = [dict(i, path=[idx, *i.get("path", [])]) for i in result["issues"]]
            return {"issues": issues}
    return {"value": value}


def _object_schema(fields: dict[str, _Schema], allow_extra: bool,
                   label: str) -> _Schema:
    def validate(value: Any) -> dict:
        if not isinstance(value, dict):
            return {"issues": [_issue(f"{label}: expected an object")]}
        issues = []
        normalized: dict = {}
        if not allow_extra:
            unknown = sorted(set(value) - set(fields))
            if unknown:
                issues.append(_issue(f"{label}: unknown keys {', '.join(unknown)}"))
        for name, field in fields.items():
            result = field.validate(value.get(name))
            if result.get("issues"):
                for i in result["issues"]:
                    issues.append(dict(i, path=[name, *i.get("path", [])]))
                continue
            normalized[name] = result.get("value", value.get(name))
        if issues:
            return {"issues": issues}
        return {"value": normalized}

    return _Schema(validate, label)


def _record_schema(value_schema: _Schema, label: str) -> _Schema:
    def validate(value: Any) -> dict:
        if not isinstance(value, dict):
            return {"issues": [_issue(f"{label}: expected a record")]}
        issues = []
        normalized: dict = {}
        for key, entry in value.items():
            result = value_schema.validate(entry)
            if result.get("issues"):
                for i in result["issues"]:
                    issues.append(dict(i, path=[key, *i.get("path", [])]))
                continue
            normalized[key] = result.get("value", entry)
        if issues:
            return {"issues": issues}
        return {"value": normalized}

    return _Schema(validate, label)


def _enum_schema(choices: tuple, label: str) -> _Schema:
    def validate(value: Any) -> dict:
        if value in choices:
            return {"value": value}
        return {"issues": [_issue(f"{label}: expected one of {choices!r}")]}

    return _Schema(validate, label)


class S:
    """命名空间：构建极简 schema（教学子集）。"""

    @staticmethod
    def any() -> _Schema:
        return _Schema(lambda v: {"value": v}, "any")

    @staticmethod
    def string() -> _Schema:
        return _Schema(
            lambda v: {"value": v} if isinstance(v, str)
            else {"issues": [_issue("expected a string")]},
            "string",
        )

    @staticmethod
    def number() -> _Schema:
        return _Schema(
            lambda v: {"value": v} if isinstance(v, (int, float)) and not isinstance(v, bool)
            else {"issues": [_issue("expected a number")]},
            "number",
        )

    @staticmethod
    def boolean() -> _Schema:
        return _Schema(
            lambda v: {"value": v} if isinstance(v, bool)
            else {"issues": [_issue("expected a boolean")]},
            "boolean",
        )

    @staticmethod
    def integer() -> _Schema:
        return _Schema(
            lambda v: {"value": v} if isinstance(v, int) and not isinstance(v, bool)
            else {"issues": [_issue("expected an integer")]},
            "integer",
        )

    @staticmethod
    def array(item: _Schema | None = None) -> _Schema:
        return S.any().array(item)

    @staticmethod
    def object(fields: dict[str, _Schema], extra: bool = False) -> _Schema:
        return _object_schema(fields, extra, "object")

    @staticmethod
    def record(value: _Schema) -> _Schema:
        return _record_schema(value, "record")

    @staticmethod
    def enum(*choices: Any) -> _Schema:
        return _enum_schema(tuple(choices), "enum")