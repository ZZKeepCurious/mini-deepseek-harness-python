"""schemastery 全量移植（对齐 vendor/schemastery/src/index.ts，902 行单文件引擎）。

本模块同时承载两层契约：

1. **schemastery 引擎**（本文件主体）：可调用 Schema 节点 + `resolve` 分发 +
   17 类 resolver + meta 构建器克隆语义 + `~standard` 协议面 + toString/to_json
   序列化 + i18n/simplify。
2. **cordis fiber 适配层**（文件尾部 `resolve_config` / `ValidationError`）：对齐
   `vendor/cordis/src/fiber.ts` 的 `resolveConfig` 读取面，被 `core/scope.py` 消费。

## 命名映射（pythonic snake_case，沿 dsh_scope.py 先例）

| 上游 | mini |
|------|------|
| `Schema.is` | `S.is_` |
| `Schema.regExp(flag)` | `S.reg_exp(flag)` |
| `Schema.arrayBuffer(enc)` | `S.array_buffer(enc)` |
| `Schema.from` | `S.from_` |
| `schema.sKey` / `schema.toJSON()` / `schema.toString()` | `schema.s_key` / `schema.to_json()` / `schema.to_string()` |
| `schemastery.ValidationError` | `SchemaValidationError`（亦 `S.ValidationError`） |

## 载体差异（标注，非简化删减）

- `callback` 字符串求值（上游 `new Function('return '+str)` 用于 JSON 反序列化路径）
  不做 eval；Python 载体仅收 callable，`to_json` 存 `inspect.getsource` 最佳努力。
- `date`/`reg_exp`/`array_buffer` 的 JS 类型锚定到 Python 对应物：
  `datetime.datetime` / `re.Pattern` / `bytes|bytearray|memoryview`。
- 消息中 `${data}` 用 Python `str()`，bool/None 拼写为 `True`/`None`（上游为
  `true`/`null`）；仅影响人类可读消息文案，不影响结构契约。
- 分层约束：`core.schema` 为 L0 叶（test_dependencies LAYER_UNITS = 0），零内部依赖；
  cosmokit 助手（deepEqual/isNullable/isPlainObject/clone/valueMap/pick/Binary）
  全部内联为模块私有实现。
"""

from __future__ import annotations

import base64
import copy as _copy_mod
import inspect
import json
import re as _re
import datetime as _dt
from typing import Any, Callable

__all__ = [
    "S",
    "Schema",
    "SchemaValidationError",
    "ValidationError",
    "resolve_config",
    "validate_schema_value",
]

# ---------------------------------------------------------------------------
# cosmokit 内联助手（vendor/cosmokit/src/types.ts 逐字移植）
# ---------------------------------------------------------------------------


def _is_nullable(value: Any) -> bool:
    return value is None


def _is_plain_object(value: Any) -> bool:
    return isinstance(value, dict)


def _clone(value: Any) -> Any:
    """结构化克隆（对齐 cosmokit clone：标量/不可变共享，dict/list 深拷贝）。

    上游对 class 实例也做原型保留拷贝；mini 仅插件配置 JSON 安全值参与 clone，
    其余（re.Pattern/datetime 等不可变）共享引用。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, list):
        return [_clone(v) for v in value]
    if isinstance(value, dict):
        return {k: _clone(v) for k, v in value.items()}
    return value  # 不可变对象共享


def _js_typeof(value: Any) -> str:
    """对齐 JS typeof 的分桶（用于 intersect 类型一致性判定）。"""
    if value is None:
        return "object"  # JS: typeof null === 'object'
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, dict)):
        return "object"
    if isinstance(value, _re.Pattern) or isinstance(value, _dt.datetime) \
            or isinstance(value, (bytes, bytearray, memoryview)):
        return "object"
    return "object"


def _deep_equal(a: Any, b: Any, strict: bool = False) -> bool:
    """对齐 cosmokit deepEqual（含 strict 第三参，simplify 用于 dict 分支）。"""
    if a is b:
        return True
    if not strict and _is_nullable(a) and _is_nullable(b):
        return True
    if _js_typeof(a) != _js_typeof(b):
        return False
    if _js_typeof(a) != "object":
        return False
    if not _is_plain_object(a) and not isinstance(a, list):
        if b is None:
            return False
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_deep_equal(x, y, strict) for x, y in zip(a, b))
    if isinstance(a, _dt.datetime) and isinstance(b, _dt.datetime):
        return a == b
    if isinstance(a, _re.Pattern) and isinstance(b, _re.Pattern):
        return a.pattern == b.pattern and a.flags == b.flags
    if isinstance(a, (bytes, bytearray, memoryview)) and isinstance(b, (bytes, bytearray, memoryview)):
        return bytes(a) == bytes(b)
    if _is_plain_object(a) and _is_plain_object(b):
        merged = set(a) | set(b)
        return all(_deep_equal(a.get(k), b.get(k), strict) for k in merged)
    return False


# ---------------------------------------------------------------------------
# 全局 uid / refs（对齐 vendor/schemastery 的 __schemastery_index__ /
# __schemastery_refs__ 全局单例）
# ---------------------------------------------------------------------------

_INDEX = 0


def _next_uid() -> int:
    global _INDEX
    uid = _INDEX
    _INDEX += 1
    return uid


_REFS: dict | None = None


# ---------------------------------------------------------------------------
# schemastery.ValidationError（与 cordis 侧同名类区分：本类 name='ValidationError'
# 且携带 options.path；$prefix 前缀格式化逐字对齐上游）
# ---------------------------------------------------------------------------


class SchemaValidationError(TypeError):
    """对齐 schemastery ValidationError：消息带 `$path` 前缀格式化。"""

    name = "ValidationError"

    def __init__(self, message: str, options: dict | None = None):
        options = options or {}
        prefix = "$"
        for segment in options.get("path") or []:
            if isinstance(segment, str):
                prefix += "." + segment
            elif isinstance(segment, int):
                prefix += "[" + str(segment) + "]"
            else:
                prefix += "[Symbol(" + str(segment) + ")]"
        if prefix.startswith("."):
            prefix = prefix[1:]
        super().__init__((prefix + " " + message) if prefix != "$" else message)
        self.options = options

    @staticmethod
    def is_(error: Any) -> bool:
        return isinstance(error, SchemaValidationError)


_ABSENT = object()


# ---------------------------------------------------------------------------
# Schema 节点
# ---------------------------------------------------------------------------


class _StandardProps:
    """对齐 `schema['~standard']` 协议视图（@standard-schema/spec v1）。"""

    vendor = "schemastery"
    version = 1

    def __init__(self, schema: "Schema"):
        self._schema = schema

    def validate(self, value: Any) -> dict:
        try:
            return {"value": Schema.resolve(value, self._schema, {})[0]}
        except SchemaValidationError as error:
            return {
                "issues": [
                    {
                        "message": str(error),
                        "path": list(error.options.get("path") or []),
                    }
                ]
            }


class Schema:
    """可调用 schema 节点（对齐 vendor/schemastery/src/index.ts 的 Schema 接口）。"""

    # 反序列化标记：上游用 Symbol.for('schemastery')；mini 单类用 isinstance 等价判定。

    def __init__(self, options: dict | None = None):
        options = options or {}
        self.type = options.get("type")
        self.meta = options.get("meta") if options.get("meta") is not None else {}
        self.inner: Any = options.get("inner")
        self.list: list | None = options.get("list")
        self.dict: dict | None = options.get("dict")
        self.s_key: Any = options.get("s_key")
        self.bits: dict | None = options.get("bits")
        self.callback: Any = options.get("callback")
        self.constructor: Any = options.get("constructor")
        self.builder: Any = options.get("builder")
        self.value: Any = options.get("value")
        self.preserve: bool = options.get("preserve") or False
        self.uid = _next_uid()

    # ----- 调用 / 协议 -----

    def __call__(self, data: Any = None, options: dict | None = None) -> Any:
        return Schema.resolve(data, self, options or {})[0]

    def __getitem__(self, key: str) -> Any:
        if key != "~standard":
            raise KeyError(key)
        return _StandardProps(self)

    # ----- 克隆 / meta 改写 -----

    def _copy(self) -> "Schema":
        data = dict(self.__dict__)
        data.pop("uid", None)
        return Schema(data)

    def _with_meta(self, **kwargs: Any) -> "Schema":
        node = self._copy()
        node.meta = {**node.meta, **kwargs}
        return node

    # ----- builder 方法（克隆语义） -----

    def required(self, value: bool = True) -> "Schema":
        return self._with_meta(required=value)

    def hidden(self, value: bool = True) -> "Schema":
        return self._with_meta(hidden=True) if value else self._with_meta(hidden=False)

    def loose(self, value: bool = True) -> "Schema":
        return self._with_meta(loose=True) if value else self._with_meta(loose=False)

    def disabled(self, value: bool = True) -> "Schema":
        return self._with_meta(disabled=True) if value else self._with_meta(disabled=False)

    def collapse(self, value: bool = True) -> "Schema":
        return self._with_meta(collapse=True) if value else self._with_meta(collapse=False)

    def comment(self, text: str) -> "Schema":
        return self._with_meta(comment=text)

    def description(self, text: str) -> "Schema":
        return self._with_meta(description=text)

    def link(self, link: str) -> "Schema":
        return self._with_meta(link=link)

    def default(self, value: Any) -> "Schema":
        return self._with_meta(default=value)

    def max(self, value: int | float) -> "Schema":
        return self._with_meta(max=value)

    def min(self, value: int | float) -> "Schema":
        return self._with_meta(min=value)

    def step(self, value: int | float) -> "Schema":
        return self._with_meta(step=value)

    def pattern(self, regexp: _re.Pattern) -> "Schema":
        return self._with_meta(pattern={"source": regexp.pattern, "flags": _flags_str(regexp)})

    def role(self, text: str, extra: Any = None) -> "Schema":
        return self._with_meta(role=text, extra=extra)

    def extra(self, key: str, value: Any) -> "Schema":
        return self._with_meta(**{key: value})

    def deprecated(self) -> "Schema":
        # 上游行为：Schema(this) 浅拷贝后原地 push badges，badges 列表与原节点共享（逐字复刻）。
        node = self._copy()
        node.meta.setdefault("badges", []).append({"text": "deprecated", "type": "danger"})
        return node

    def experimental(self) -> "Schema":
        node = self._copy()
        node.meta.setdefault("badges", []).append({"text": "experimental", "type": "warning"})
        return node

    def set(self, key: str, value: "Schema") -> "Schema":
        self.dict[key] = value
        return self

    def push(self, value: "Schema") -> "Schema":
        self.list.append(value)
        return self

    def simplify(self, value: Any) -> Any:
        if _deep_equal(value, self.meta.get("default"), self.type == "dict"):
            return None
        if _is_nullable(value):
            return value
        if self.type == "object" or self.type == "dict":
            result = {}
            for key in value:
                sub = self.dict[key] if self.type == "object" else self.inner
                item = sub.simplify(value[key]) if sub else None
                if self.type == "dict" or not _is_nullable(item):
                    result[key] = item
            if _deep_equal(result, self.meta.get("default"), self.type == "dict"):
                return None
            return result
        if self.type == "array" or self.type == "tuple":
            result = []
            subs = self.list if self.type == "tuple" else None
            for index, item in enumerate(value):
                sub = (subs[index] if subs else self.inner) if subs or self.inner else None
                result.append(sub.simplify(item) if sub else item)
            return result
        if self.type == "intersect":
            result = {}
            for sub in self.list or []:
                result.update(sub.simplify(value))
            return result
        if self.type == "union":
            for sub in self.list or []:
                try:
                    Schema.resolve(value, sub, {})
                    return sub.simplify(value)
                except SchemaValidationError:
                    pass
        return value

    # ----- 序列化 / 格式化 -----

    def to_string(self, inline: bool = False) -> str:
        formatter = _FORMATTERS.get(self.type)
        if formatter is None:
            return f"Schema<{self.type}>"
        return formatter(self, inline)

    def to_json(self) -> Any:
        global _REFS
        if _REFS is not None:
            if self.uid not in _REFS:
                _REFS[self.uid] = _plain(self)
            return self.uid
        _REFS = {}
        try:
            root_uid = self.to_json()
            return {"uid": root_uid, "refs": _REFS}
        finally:
            _REFS = None



    def i18n(self, messages: dict) -> "Schema":
        node = self._copy()
        desc = _merge_desc(node.meta.get("description"), messages)
        if desc:
            node.meta["description"] = desc
        if node.dict:
            node.dict = {
                key: inner.i18n(_drill(messages, key))
                for key, inner in node.dict.items()
            }
        if node.list:
            node.list = [
                inner.i18n(_drill_array(messages, index))
                for index, inner in enumerate(node.list)
            ]
        if node.inner:
            node.inner = node.inner.i18n(_drill(messages, None))
        if node.s_key:
            node.s_key = node.s_key.i18n(_drill_key(messages))
        return node

    # ----- 解析分发 -----

    @staticmethod
    def resolve(data: Any, schema: "Schema | None", options: dict | None = None, strict: bool = False) -> list:
        options = options or {}
        if schema is None:
            return [data]
        if callable(options.get("ignore")) and options["ignore"](data, schema):
            return [data]

        if _is_nullable(data) and schema.type != "lazy":
            if schema.meta.get("required"):
                raise SchemaValidationError("missing required value", options)
            current = schema
            fallback = schema.meta.get("default")
            while current is not None and current.type == "intersect" and _is_nullable(fallback):
                current = current.list[0] if current.list else None
                fallback = current.meta.get("default") if current else None
            if _is_nullable(fallback):
                return [data]
            data = _clone(fallback)

        callback = _RESOLVERS.get(schema.type)
        if callback is None:
            raise SchemaValidationError(f'unsupported type "{schema.type}"', options)

        try:
            return callback(data, schema, options, strict)
        except SchemaValidationError:
            if not schema.meta.get("loose"):
                raise
            return [schema.meta.get("default")]

    @staticmethod
    def from_(source: Any) -> "Schema":
        if _is_nullable(source):
            return S.any()
        if isinstance(source, (str, int, float, bool)):
            return S.const(source).required()
        if isinstance(source, Schema):
            return source
        if source is str:
            return S.string().required()
        if source is int or source is float:
            return S.number().required()
        if source is bool:
            return S.boolean().required()
        if callable(source):
            return S.is_(source).required()
        raise TypeError(f"cannot infer schema from {source}")

    @staticmethod
    def extend(type_: str, resolve_fn: Callable) -> None:
        _RESOLVERS[type_] = resolve_fn

def _plain(node: Schema) -> dict:
    """序列化单节点（refs 活动期由子节点 to_json 递归登记 uid）。"""
    out: dict = {"type": node.type, "meta": _copy_mod.deepcopy(node.meta)}
    if node.inner is not None:
        out["inner"] = node.inner.to_json()
    if node.list is not None:
        out["list"] = [x.to_json() for x in node.list]
    if node.dict is not None:
        out["dict"] = {k: v.to_json() for k, v in node.dict.items()}
    if node.s_key is not None:
        out["s_key"] = node.s_key.to_json()
    if node.bits is not None:
        out["bits"] = node.bits
    if node.callback is not None:
        try:
            out["callback"] = inspect.getsource(node.callback)
        except (OSError, TypeError):
            out["callback"] = repr(node.callback)
    if node.constructor is not None:
        out["constructor"] = node.constructor.__name__ if callable(node.constructor) else node.constructor
    if node.value is not None:
        out["value"] = node.value
    if node.preserve:
        out["preserve"] = True
    if node.builder is not None:
        out["builder"] = getattr(node.builder, "__name__", repr(node.builder))
    return out



# ---------------------------------------------------------------------------
# 格式化辅助
# ---------------------------------------------------------------------------


def _flags_str(regexp: _re.Pattern) -> str:
    flags = regexp.flags
    out = ""
    mapping = [(_re.I, "i"), (_re.M, "m"), (_re.S, "s"), (_re.X, "x"), (_re.A, "a")]
    for bit, letter in mapping:
        if flags & bit:
            out += letter
    return out


def _merge_desc(original: Any, messages: dict) -> dict:
    result = {}
    if isinstance(original, str):
        result[""] = original
    elif isinstance(original, dict):
        result.update(original)
    for locale, value in messages.items():
        if isinstance(value, dict) and (value.get("$description") or value.get("$desc")):
            result[locale] = value.get("$description") or value.get("$desc")
        elif isinstance(value, str):
            result[locale] = value
    return result


def _drill(messages: dict, key) -> dict:
    out = {}
    for locale, value in messages.items():
        if not isinstance(value, dict):
            continue
        inner = value.get("$value", value.get("$inner"))
        if isinstance(inner, dict) and key in inner:
            out[locale] = inner[key]
        elif isinstance(value, dict) and key in value:
            out[locale] = value[key]
    return out


def _drill_array(messages: dict, index: int) -> dict:
    out = {}
    for locale, value in messages.items():
        if not isinstance(value, dict):
            continue
        inner = value.get("$value", value.get("$inner"))
        if isinstance(inner, (list, tuple)) and len(inner) > index:
            out[locale] = inner[index]
        elif isinstance(value, (list, tuple)) and len(value) > index:
            out[locale] = value[index]
        else:
            out[locale] = {k: v for k, v in (value.items() if isinstance(value, dict) else [])}
    return out


def _drill_key(messages: dict) -> dict:
    out = {}
    for locale, value in messages.items():
        if isinstance(value, dict) and "$key" in value:
            out[locale] = value["$key"]
    return out


# ---------------------------------------------------------------------------
# resolver 辅助
# ---------------------------------------------------------------------------


def _check_within_range(data: int | float, meta: dict, description: str, options: dict, skip_min: bool = False) -> None:
    max_value = meta.get("max", float("inf"))
    min_value = meta.get("min", -float("inf"))
    if data > max_value:
        raise SchemaValidationError(f"expected {description} <= {max_value} but got {data}", options)
    if data < min_value and not skip_min:
        raise SchemaValidationError(f"expected {description} >= {min_value} but got {data}", options)


def _decimal_shift(data: float, digits: int) -> float:
    s = repr(data)
    if "e" in s or "E" in s:
        return data * (10 ** digits)
    index = s.find(".")
    if index == -1:
        return data * (10 ** digits)
    frac = s[index + 1:]
    integer = s[:index]
    if len(frac) <= digits:
        return float(integer + frac.ljust(digits, "0"))
    return float(integer + frac[:digits] + "." + frac[digits:])


def _is_multiple_of(data: float, minimum: float, step: float) -> bool:
    step = abs(step)
    if not _re.match(r"^\d+\.\d+$", str(step)):
        return (data - minimum) % step == 0
    index = str(step).find(".")
    digits = len(str(step)[index + 1:])
    return abs(_decimal_shift(data, digits) - _decimal_shift(minimum, digits)) % _decimal_shift(step, digits) == 0


def _property(data: Any, key: Any, schema: Schema, options: dict) -> Any:
    try:
        child = dict(options)
        child["path"] = list(options.get("path") or []) + [key]
        value_in = data[key] if isinstance(data, list) else data.get(key)
        result = Schema.resolve(value_in, schema, child)
        value = result[0]
        adapted = result[1] if len(result) > 1 else _ABSENT
        if adapted is not _ABSENT:
            data[key] = adapted
        return value
    except SchemaValidationError:
        if not options.get("autofix"):
            raise
        data.pop(key, None)
        return schema.meta.get("default")


def _merge(result: dict, data: dict) -> None:
    for key in data:
        if key in result:
            continue
        result[key] = data[key]


# ---------------------------------------------------------------------------
# resolver 注册表 + 17 类实现
# ---------------------------------------------------------------------------

_RESOLVERS: dict[str, Callable] = {}


def _register(type_: str) -> Callable:
    def deco(fn: Callable) -> Callable:
        _RESOLVERS[type_] = fn
        return fn
    return deco


@_register("any")
def _resolve_any(data, schema, options, strict):
    return [data]


@_register("never")
def _resolve_never(data, schema, options, strict):
    raise SchemaValidationError(f"expected nullable but got {data}", options)


@_register("const")
def _resolve_const(data, schema, options, strict):
    if _deep_equal(data, schema.value):
        return [schema.value]
    raise SchemaValidationError(f"expected {schema.value} but got {data}", options)


@_register("string")
def _resolve_string(data, schema, options, strict):
    if not isinstance(data, str):
        raise SchemaValidationError(f"expected string but got {data}", options)
    meta = schema.meta
    if meta.get("pattern"):
        regexp = _re.compile(meta["pattern"]["source"], _re_flag_int(meta["pattern"].get("flags", "")))
        if not regexp.search(data):
            raise SchemaValidationError(f"expect string to match regexp {regexp}", options)
    _check_within_range(len(data), meta, "string length", options)
    return [data]


@_register("number")
def _resolve_number(data, schema, options, strict):
    if not isinstance(data, (int, float)) or isinstance(data, bool):
        raise SchemaValidationError(f"expected number but got {data}", options)
    meta = schema.meta
    _check_within_range(data, meta, "number", options)
    step = meta.get("step")
    if step and not _is_multiple_of(data, meta.get("min", 0), step):
        raise SchemaValidationError(f"expected number multiple of {step} but got {data}", options)
    return [data]


@_register("boolean")
def _resolve_boolean(data, schema, options, strict):
    if isinstance(data, bool):
        return [data]
    raise SchemaValidationError(f"expected boolean but got {data}", options)


@_register("function")
def _resolve_function(data, schema, options, strict):
    if callable(data):
        return [data]
    raise SchemaValidationError(f"expected function but got {data}", options)


@_register("is")
def _resolve_is(data, schema, options, strict):
    constructor = schema.constructor
    if callable(constructor):
        if isinstance(data, constructor):
            return [data]
        raise SchemaValidationError(f"expected {constructor.__name__} but got {data}", options)
    if _is_nullable(data):
        raise SchemaValidationError(f"expected {constructor} but got {data}", options)
    for cls in type(data).__mro__:
        if cls.__name__ == constructor:
            return [data]
    raise SchemaValidationError(f"expected {constructor} but got {data}", options)


@_register("bitset")
def _resolve_bitset(data, schema, options, strict):
    bits = schema.bits or {}
    meta = schema.meta
    value = 0
    keys: list[str] = []
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        value = int(data)
        for key, bit in bits.items():
            if value & bit:
                keys.append(key)
    elif isinstance(data, (list, tuple)):
        keys = list(data)
        for key in keys:
            if not isinstance(key, str):
                raise SchemaValidationError(f"expected string but got {key}", options)
            if key in bits:
                value |= bits[key]
    else:
        raise SchemaValidationError(f"expected number or array but got {data}", options)
    if value == meta.get("default"):
        return [value]
    return [value, keys]


@_register("array")
def _resolve_array(data, schema, options, strict):
    if not isinstance(data, list):
        raise SchemaValidationError(f"expected array but got {data}", options)
    inner = schema.inner
    skip_min = not _is_nullable(inner.meta.get("default")) if inner is not None else False
    _check_within_range(len(data), schema.meta, "array length", options, skip_min)
    return [[_property(data, index, inner, options) for index in range(len(data))]]


@_register("dict")
def _resolve_dict(data, schema, options, strict):
    if not _is_plain_object(data):
        raise SchemaValidationError(f"expected object but got {data}", options)
    result = {}
    for key in list(data.keys()):
        try:
            r_key = Schema.resolve(key, schema.s_key, options)[0]
        except SchemaValidationError:
            if strict:
                continue
            raise
        result[r_key] = _property(data, key, schema.inner, options)
        data[r_key] = data[key]
        if key != r_key:
            data.pop(key, None)
    return [result]


@_register("tuple")
def _resolve_tuple(data, schema, options, strict):
    if not isinstance(data, list):
        raise SchemaValidationError(f"expected array but got {data}", options)
    members = schema.list or []
    result = [_property(data, index, members[index], options) for index in range(len(members))]
    if strict:
        return [result]
    result.extend(data[len(members):])
    return [result]


@_register("object")
def _resolve_object(data, schema, options, strict):
    if not _is_plain_object(data):
        raise SchemaValidationError(f"expected object but got {data}", options)
    result = {}
    fields = schema.dict or {}
    for key in fields:
        value = _property(data, key, fields[key], options)
        if not _is_nullable(value) or key in data:
            result[key] = value
    if not strict:
        _merge(result, data)
    return [result]


@_register("union")
def _resolve_union(data, schema, options, strict):
    messages = []
    for inner in schema.list or []:
        try:
            return Schema.resolve(data, inner, options, strict)
        except SchemaValidationError as error:
            messages.append(error)
    raise SchemaValidationError(f"expected {schema.to_string()} but got {json.dumps(data, default=str)}", options)


@_register("intersect")
def _resolve_intersect(data, schema, options, strict):
    members = schema.list or []
    if not members:
        return [data]
    result = None
    for inner in members:
        value = Schema.resolve(data, inner, options, True)[0]
        if _is_nullable(value):
            continue
        if _is_nullable(result):
            result = value
        elif _js_typeof(result) != _js_typeof(value):
            raise SchemaValidationError(f"expected {schema.to_string()} but got {json.dumps(data, default=str)}", options)
        elif _is_plain_object(value) or isinstance(value, list):
            _merge(result if isinstance(result, dict) else {}, value)
        elif result != value:
            raise SchemaValidationError(f"expected {schema.to_string()} but got {json.dumps(data, default=str)}", options)
    if not strict and _is_plain_object(data):
        _merge(result if isinstance(result, dict) else {}, data)
    return [result]


@_register("transform")
def _resolve_transform(data, schema, options, strict):
    inner_result = Schema.resolve(data, schema.inner, options, True)
    result = inner_result[0]
    adapted = inner_result[1] if len(inner_result) > 1 else data
    if schema.preserve:
        return [schema.callback(result)]
    return [schema.callback(result), schema.callback(adapted)]


@_register("lazy")
def _resolve_lazy(data, schema, options, strict):
    if not isinstance(schema.inner, Schema):
        schema.inner = schema.builder()
        schema.inner.meta = {**schema.meta, **schema.inner.meta}
    return Schema.resolve(data, schema.inner, options, strict)


# ---------------------------------------------------------------------------
# 工厂（defineMethod 移植）
# ---------------------------------------------------------------------------

_FORMATTERS: dict[str, Callable] = {}


def _define(name: str, keys: tuple, formatter: Callable) -> None:
    """仅登记 toString formatter（工厂函数由 _SNamespace 直接提供）。"""
    _FORMATTERS[name] = formatter


def _re_flag_int(flags: str) -> int:
    table = {"i": _re.I, "m": _re.M, "s": _re.S, "x": _re.X, "a": _re.A}
    result = 0
    for ch in flags:
        result |= table.get(ch, 0)
    return result


# 原生/基础工厂
_define("is", ("constructor",), lambda schema, inline=False: schema.constructor.__name__ if callable(schema.constructor) else schema.constructor)
_define("any", (), lambda schema, inline=False: "any")
_define("never", (), lambda schema, inline=False: "never")
_define("const", ("value",), lambda schema, inline=False: json.dumps(schema.value) if isinstance(schema.value, str) else schema.value)
_define("string", (), lambda schema, inline=False: "string")
_define("number", (), lambda schema, inline=False: "number")
_define("boolean", (), lambda schema, inline=False: "boolean")
_define("bitset", ("bits",), lambda schema, inline=False: "bitset")
_define("function", (), lambda schema, inline=False: "function")
_define("array", ("inner",), lambda schema, inline=False: f"{schema.inner.to_string(True)}[]")
_define("dict", ("inner", "sKey"), lambda schema, inline=False: f"{{ [key: {schema.s_key.to_string()}]: {schema.inner.to_string()} }}")
_define("tuple", ("list",), lambda schema, inline=False: f"[{', '.join(m.to_string() for m in schema.list)}]")
_define("object", ("dict",), lambda schema, inline=False: "{}" if not schema.dict else
        "{ " + ", ".join(f"{k}{'' if schema.dict[k].meta.get('required') else '?'}: {schema.dict[k].to_string()}" for k in schema.dict) + " }")
_define("union", ("list",), lambda schema, inline=False: ("(" if inline else "") + " | ".join(m.to_string() for m in schema.list) + (")" if inline else ""))
_define("intersect", ("list",), lambda schema, inline=False: " & ".join(m.to_string(True) for m in schema.list))
_define("transform", ("inner", "callback", "preserve"), lambda schema, inline=False: schema.inner.to_string(inline))


# 复合/派生工厂
def _lazy(builder: Callable) -> Schema:
    return Schema({"type": "lazy", "builder": builder, "inner": None})


def _natural() -> Schema:
    return S.number().step(1).min(0)


def _percent() -> Schema:
    return S.number().step(0.01).min(0).max(1).role("slider")


def _date() -> Schema:
    def parse(value):
        if isinstance(value, _dt.datetime):
            return value
        if isinstance(value, _dt.date):
            return _dt.datetime(value.year, value.month, value.day)
        text = str(value)
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return _dt.datetime.fromisoformat(text)
        except ValueError:
            raise SchemaValidationError(f'invalid date "{value}"', {})
    return S.union([
        S.is_(_dt.datetime),
        S.transform(S.string().role("datetime"), parse, True),
    ])


def _reg_exp(flag: str = "") -> Schema:
    def compile_fn(value):
        try:
            return _re.compile(str(value), _re_flag_int(flag))
        except _re.error as error:
            raise SchemaValidationError(str(error), {})
    return S.union([
        S.is_(_re.Pattern),
        S.transform(S.string().role("regexp", {"flag": flag}), compile_fn, True),
    ])


def _array_buffer(encoding: str | None = None) -> Schema:
    def accept(value):
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
        raise SchemaValidationError(f"expected ArrayBufferSource but got {value}", {})

    members = [
        S.is_(bytes),
        S.is_(bytearray),
        S.is_(memoryview),
        S.transform(S.any(), accept, True),
    ]
    if encoding == "hex":
        def from_hex(value):
            try:
                return base64.b16decode(str(value).upper())
            except (ValueError, TypeError) as error:
                raise SchemaValidationError(str(error), {})
        members.append(S.transform(S.string(), from_hex, True))
    elif encoding == "base64":
        def from_b64(value):
            try:
                return base64.b64decode(str(value), validate=True)
            except (ValueError, TypeError) as error:
                raise SchemaValidationError(str(error), {})
        members.append(S.transform(S.string(), from_b64, True))
    return S.union(members)


class _SNamespace:
    """S 命名空间（对齐上游 `Schema` 默认导出 = 静态构造器 + 工厂）。"""

    any = staticmethod(lambda: Schema({"type": "any"}))
    never = staticmethod(lambda: Schema({"type": "never"}))
    const = staticmethod(lambda value: _build_const(value))
    string = staticmethod(lambda: Schema({"type": "string"}))
    number = staticmethod(lambda: Schema({"type": "number"}))
    boolean = staticmethod(lambda: Schema({"type": "boolean"}))
    natural = staticmethod(_natural)
    percent = staticmethod(_percent)
    date = staticmethod(_date)
    reg_exp = staticmethod(_reg_exp)
    array_buffer = staticmethod(_array_buffer)
    bitset = staticmethod(lambda bits: _build_bitset(bits))
    function = staticmethod(lambda: Schema({"type": "function"}))
    is_ = staticmethod(lambda constructor: Schema({"type": "is", "constructor": constructor}))
    array = staticmethod(lambda inner=None: Schema({"type": "array", "inner": S.from_(inner) if inner is not None else S.any()}))
    dict = staticmethod(lambda inner=None, s_key=None: Schema({"type": "dict", "inner": S.from_(inner) if inner is not None else S.any(), "s_key": (s_key if isinstance(s_key, Schema) else (S.from_(s_key) if s_key is not None else S.string()))}))
    tuple = staticmethod(lambda members=(): Schema({"type": "tuple", "list": [x if isinstance(x, Schema) else S.from_(x) for x in members]}))
    object = staticmethod(lambda fields=None: Schema({"type": "object", "dict": {k: (v if isinstance(v, Schema) else S.from_(v)) for k, v in (fields or {}).items()}, "meta": {"default": {}}}))
    union = staticmethod(lambda members=(): Schema({"type": "union", "list": [x if isinstance(x, Schema) else S.from_(x) for x in members]}))
    intersect = staticmethod(lambda members=(): Schema({"type": "intersect", "list": [x if isinstance(x, Schema) else S.from_(x) for x in members]}))
    transform = staticmethod(lambda inner, callback, preserve=False: Schema({"type": "transform", "inner": S.from_(inner), "callback": callback, "preserve": preserve}))
    lazy = staticmethod(_lazy)
    from_ = staticmethod(Schema.from_)
    resolve = staticmethod(Schema.resolve)
    extend = staticmethod(Schema.extend)
    ValidationError = SchemaValidationError

def _build_const(value):
    return Schema({"type": "const", "value": value})


def _build_bitset(bits):
    return Schema({"type": "bitset", "bits": {k: v for k, v in bits.items() if isinstance(v, (int, float))}, "meta": {"default": 0}})


S = _SNamespace()


# ---------------------------------------------------------------------------
# 协议读取面（对齐 vendor/cordis/src/fiber.ts resolveConfig）
# ---------------------------------------------------------------------------


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


def resolve_config(schema: Any, config: Any) -> Any:
    """校验并归一化插件配置；无 schema 原样返回（对齐 fiber.ts resolveConfig）。"""
    if schema is None:
        return config
    standard = schema["~standard"]
    result = standard.validate(config)
    if result.get("issues"):
        raise ValidationError(result["issues"])
    return result.get("value", config)


def validate_schema_value(schema: Any, value: Any) -> Any:
    """低层校验入口：返回归一化值，失败抛 ValidationError。"""
    return resolve_config(schema, value)
