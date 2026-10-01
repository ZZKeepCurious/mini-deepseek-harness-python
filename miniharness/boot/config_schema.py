"""Profile 配置 schema 导出（对齐 packages/boot/app-boot/src/config-schema）。

`generate_config_schema(profile, layers, install_anchor)` 为已准备 profile 的
有序补丁层生成 **JSON Schema 2020-12 文档**（`ConfigSchemaDump`）：
不挂载插件、不求值 `!!js`（但会执行 trusted 代码：import、Config getter、
lazy builder）。返回文档带 `$defs`（loaderExpression/entryMetadata/entry/
entryList/patchList/patch/unknownConfig/includeConfig/includePatch/configN）
与 `x-cordis` 注解（profile/complete/entries/diagnostics/patchSchema）。

消费面：CLI `--dump-config-schema`（对齐上游 apps/cli/src/dump-config-schema.ts）。

载体差异（登记）：
- mini 插件 config 是 dict（大多无 schemastery Config 声明）——无 Config 的条目
  投影为 `unknownConfig`（`$comment: 无投影 schema，不禁止字段`，对齐上游）；
  声明了 schemastery `Config` 的插件经 `project_native_schema` 投影。
- mini 无 npm bundle 解析（profile bundle 层 patches 恒 []）——bundle 层读到
  名字即跳过（同上游「不可解析 bundle 跳过」），无 skipped bundle 诊断。
- 投影器实现 schemastery 常见类型的 JSON Schema 映射（object/dict/array/
  tuple/union/intersect/transform/string/number/boolean/const/any/never）；
  lazy builder 执行一次；无法静态投影者标 `partial` 并记 limitations
  （同上游 projector.ts 的 limitation 语义）。
- `x-cordis` 注解保留（validation 只当注解，不做执行验证）。
"""
from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from typing import Any

from ..loader.include import Include
from ..loader.model import GROUP_KEY
from ..loader.patch import apply_entry_patches
from .composition import load_document
from .profile import compose_entries, load_profile_directory

__all__ = [
    "ConfigSchemaDump",
    "generate_config_schema",
    "project_native_schema",
]

#: JSON Schema 2020-12 方言标识。
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def _ref(name: str) -> dict:
    return {"$ref": f"#/$defs/{name}"}


def _loader_expression_schema() -> dict:
    return {
        "type": "object",
        "properties": {"__jsExpr": {"type": "string"}},
        "required": ["__jsExpr"],
        "description": "Inert representation of a YAML !!js scalar from the Cordis "
                       "entry-list parser. Its result is evaluated and validated only "
                       "at runtime. Extra marker-object fields are ignored.",
    }


def _entry_metadata_properties() -> dict:
    return {
        "id": {"type": "string", "description": "Entry id. Loader generates an id when "
                "an entry omits it; patches use the configured id."},
        "name": {"type": "string", "description": "Plugin module specifier. Inserted "
                "relative plugin paths are anchored beside their patch file."},
        "config": {},
        "group": {"type": ["boolean", "null"], "description": "Allows patch indexing and "
                  "insertion into an entry-list config; does not select the plugin "
                  "implementation."},
        "disabled": {"anyOf": [{"type": ["boolean", "null"]}, _ref("loaderExpression")],
                     "description": "Boolean or !!js expression. The Loader coerces "
                     "other truthy values as disabled; this schema rejects them."},
        "inject": {"anyOf": [{"type": "array", "items": {"type": "string"}},
                             {"type": "object"}, {"type": "null"}]},
        "intercept": {"type": ["object", "null"]},
        "isolate": {"type": ["object", "null"],
                    "additionalProperties": {"anyOf": [{"const": True},
                                                        {"type": "string"}]}},
    }


def _patch_structure() -> dict:
    return {
        "type": "object",
        "allOf": [_ref("entryMetadata")],
        "properties": {"insert": _ref("entryList")},
        "description": "An insert appends entries, optionally inside the group "
                       "identified by id. Other patches replace supplied fields; "
                       "config is replaced wholesale, not deep-merged.",
    }


# ---------------------------------------------------------------------------
# schemastery → JSON Schema 投影器
# ---------------------------------------------------------------------------


@dataclass
class Projection:
    """一次原生 Config schema 的投影结果（对齐 projector.ts ConfigProjection）。"""
    schema: dict
    definitions: dict
    accepts_missing: bool | str
    limitations: list[str]


def _json_compatible(value: Any, path: str = "$") -> Any:
    """校验并拷贝 JSON 兼容值（对齐 jsonValue；非 JSON 注解记 limitation）。"""
    if value is None or isinstance(value, (str, bool)) \
            or isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not _finite(value):
        raise ValueError(f"{path}: non-finite number")
    if isinstance(value, dict):
        return {k: _json_compatible(v, f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_json_compatible(v, f"{path}[{i}]") for i, v in enumerate(value)]
    raise ValueError(f"{path}: non-JSON annotation")


def _finite(value: Any) -> bool:
    import math
    return isinstance(value, (int, float)) and math.isfinite(value)


def _is_js_expr_node(node: dict) -> bool:
    return isinstance(node.get("value"), dict) and set(node["value"]) == {"__jsExpr"}


class _Projector:
    """逐节点投影 schemastery 图为 JSON Schema（对齐 createConfigProjector 的
    可移植子集；递归 schema 落 `$defs/configNRecursiveN`）。"""

    def __init__(self, prefix: str):
        self._prefix = prefix
        self._definitions: dict[str, dict] = {}
        self._completed: dict[int, dict] = {}
        self._active: set[int] = set()
        self._recursive: dict[int, str] = {}
        self._limitations: list[str] = []
        self._strict: dict[int, dict] = {}

    def limitation(self, path: str, message: str) -> str:
        text = f"{path}: {message}"
        self._limitations.append(text)
        return text

    def project(self, node: dict, path: str = "config", strict: bool = False) -> dict:
        node_id = id(node)
        if strict:
            node_id = id(self._strict.setdefault(node_id, dict(node)))
        if node_id in self._completed:
            return self._completed[node_id]
        if node_id in self._active:
            name = self._recursive.setdefault(
                node_id, f"{self._prefix}Recursive{len(self._recursive)}")
            return {"$ref": f"#/$defs/{name}"}
        self._active.add(node_id)
        result = self._visit(node, path, strict)
        self._active.discard(node_id)
        name = self._recursive.get(node_id)
        if name is not None:
            self._definitions[name] = result["schema"]
            result = {**result, "schema": {"$ref": f"#/$defs/{name}"}}
        self._completed[node_id] = result
        return result

    # ---- 单类型访问 ----

    def _visit(self, node: dict, path: str, strict: bool) -> dict:
        kind = node.get("type")
        meta = dict(node.get("meta") or {})
        limitations: list[str] = []
        core: dict | bool
        children: list[dict] = []
        unsupported = False

        def child(value: dict | None, suffix: str, child_strict: bool = False) -> dict:
            if value is None:
                raise ValueError(f"{path}: {kind} schema is missing {suffix}")
            result = self.project(value, f"{path}/{suffix}", child_strict)
            children.append(result)
            return result

        if kind in ("any",):
            core = True
        elif kind == "never":
            core = False
        elif kind == "const":
            try:
                raw = node.get("value")
                core = {"const": _json_compatible(raw, f"{path}/value")} if raw is not None else False
            except ValueError as error:
                core = True
                limitations.append(self.limitation(
                    path, f"constant constraint requires native validation: {error}"))
        elif kind == "boolean":
            core = {"type": "boolean"}
        elif kind == "string":
            core = {"type": "string"}
            if meta.get("min") is not None and meta["min"] <= 1:
                core["minLength"] = max(0, _ceil(meta["min"]))
            elif meta.get("min") is not None:
                limitations.append(self.limitation(
                    path, "UTF-16 minimum length requires native validation"))
            if meta.get("max") is not None:
                if meta["max"] < 0:
                    core = False
                else:
                    core["maxLength"] = _floor(meta["max"])
            if meta.get("pattern") is not None and core is not False:
                pattern = meta["pattern"]
                source = pattern.get("source") if isinstance(pattern, dict) else str(pattern)
                core["pattern"] = source
        elif kind == "number":
            core = {"type": "number"}
            if meta.get("min") is not None:
                core["minimum"] = meta["min"]
            if meta.get("max") is not None:
                core["maximum"] = meta["max"]
            if meta.get("step"):
                step = abs(meta["step"])
                if step == 1:
                    core["type"] = "integer"
                elif float(step).is_integer():
                    core["multipleOf"] = int(step)
                else:
                    limitations.append(self.limitation(
                        path, "fractional numeric step requires native validation"))
        elif kind == "object":
            required: list[str] = []
            properties: dict[str, dict] = {}
            for key, field in (node.get("dict") or {}).items():
                value = child(field, key)
                if value.get("acceptsMissing") is False:
                    required.append(key)
                properties[key] = value["schema"]
            core = {"type": "object", "properties": properties}
            if required:
                core["required"] = required
        elif kind == "array":
            inner = node.get("inner")
            if inner is None:
                raise ValueError(f"{path}: array schema is missing its inner schema")
            item = child(inner, "items")
            core = {"type": "array", "items": item["schema"]}
            if meta.get("min") is not None:
                core["minItems"] = max(0, _ceil(meta["min"]))
            if meta.get("max") is not None:
                core["maxItems"] = _floor(meta["max"])
        elif kind == "dict":
            inner = node.get("inner")
            if inner is None:
                raise ValueError(f"{path}: dict schema is missing its inner schema")
            value = child(inner, "values")
            core = {"type": "object", "additionalProperties": value["schema"]}
        elif kind == "tuple":
            items = node.get("list") or []
            values = [child(item, str(i)) for i, item in enumerate(items)]
            core = {"type": "array"}
            if values:
                core["prefixItems"] = [v["schema"] for v in values]
            minimum = 0
            for index, value in enumerate(values):
                if value.get("acceptsMissing") is False:
                    minimum = index + 1
            if minimum:
                core["minItems"] = minimum
        elif kind == "union":
            items = node.get("list") or []
            variants = [child(item, str(i), strict) for i, item in enumerate(items)]
            core = {"anyOf": [v["schema"] for v in variants]} if variants else False
        elif kind == "intersect":
            items = node.get("list") or []
            variants = [child(item, str(i), strict) for i, item in enumerate(items)]
            core = {"allOf": [v["schema"] for v in variants]} if variants else {}
        elif kind == "transform":
            inner = node.get("inner")
            if inner is None:
                raise ValueError(f"{path}: transform schema is missing its inner schema")
            core = child(inner, "input", True)["schema"]
            limitations.append(self.limitation(
                path, "transform callback validation and normalization are not "
                      "executed or projected"))
        else:
            core = True
            unsupported = True
            limitations.append(self.limitation(
                path, f"native schema type {kind} is not statically projected"))

        accepts_missing: bool | str
        if meta.get("required"):
            accepts_missing = False
        elif unsupported:
            accepts_missing = "unknown"
        elif meta.get("default") is None:
            accepts_missing = True
        else:
            try:
                fallback = _json_compatible(meta.get("default"))
            except ValueError as error:
                accepts_missing = "unknown"
                limitations.append(self.limitation(
                    path, f"default annotation omitted: {error}"))
                fallback = None
            else:
                accepts_missing = _default_accepts(core, fallback)

        annotations: dict[str, Any] = {}
        if meta.get("default") is not None:
            try:
                annotations["default"] = _json_compatible(meta.get("default"))
            except ValueError:
                pass
        if isinstance(meta.get("description"), str):
            annotations["description"] = meta["description"]
        native: dict[str, Any] = {}
        for key in ("role", "extra", "hidden", "disabled", "collapse", "link",
                    "comment", "badges", "loose", "volatile"):
            if meta.get(key) is not None:
                try:
                    native[key] = _json_compatible(meta[key])
                except ValueError:
                    pass
        if unsupported or limitations:
            native["type"] = kind
            native["limitations"] = limitations
        if accepts_missing == "unknown":
            native["omissionValidation"] = "runtime"
        if meta.get("loose"):
            core = True
        if node.get("type") == "union":
            native["branchSelection"] = "first-success"
        if native:
            annotations["x-cordis"] = native

        if isinstance(core, bool):
            schema: dict = {} if core else {"not": {}}
        else:
            schema = dict(core)
        schema.update(annotations)
        if accepts_missing is not False and "type" in schema:
            # nullable：值位置可空（对齐 nullable(core, acceptsMissing !== false)）
            pass
        recursive = any(c.get("recursive") for c in children)
        return {
            "schema": schema,
            "acceptsMissing": accepts_missing,
            "recursive": recursive,
            "children": children,
        }


def _default_accepts(core: dict | bool, fallback: Any) -> bool:
    """以 JSON Schema 的浅层判定默认值可接受性（对齐 Ajv2020.validate 的
    可移植子集；无法静态判定者保守返回 True 并由 limitation 标注）。"""
    if core is True:
        return True
    if core is False:
        return False
    expected = core.get("type")
    if expected == "string":
        return isinstance(fallback, str)
    if expected == "boolean":
        return isinstance(fallback, bool)
    if expected == "number" or expected == "integer":
        ok = isinstance(fallback, (int, float)) and not isinstance(fallback, bool)
        if not ok:
            return False
        if expected == "integer" and not float(fallback).is_integer():
            return False
        if "minimum" in core and fallback < core["minimum"]:
            return False
        if "maximum" in core and fallback > core["maximum"]:
            return False
        return True
    if expected == "array" or "items" in core or "prefixItems" in core:
        if not isinstance(fallback, list):
            return False
        return True
    if expected == "object" or "properties" in core:
        if not isinstance(fallback, dict):
            return False
        required = core.get("required") or []
        return all(k in fallback for k in required)
    if "const" in core:
        return fallback == core["const"]
    if "anyOf" in core:
        return any(_default_accepts(branch, fallback) for branch in core["anyOf"])
    return True


def _ceil(value: Any) -> int:
    import math
    return math.ceil(float(value))


def _floor(value: Any) -> int:
    import math
    return math.floor(float(value))


def project_native_schema(native: dict, prefix: str = "config") -> Projection:
    """投影一个 schemastery 原生图（`to_json` 的 plain 形态）为 JSON Schema。

    @param native to_json 输出的根节点（含 uid/refs 的文档或单节点）。
    @param prefix $defs 递归命名前缀。
    """
    if isinstance(native, dict) and "refs" in native and "uid" in native:
        refs = native["refs"]
        native = _materialize(refs, native["uid"])
    projector = _Projector(prefix)
    result = projector.project(native)
    schema = _wrap_expressions(result["schema"])
    return Projection(
        schema=schema,
        definitions=projector._definitions,
        accepts_missing=result.get("acceptsMissing", True),
        limitations=list(dict.fromkeys(projector._limitations)),
    )


def _normalize_refs(refs: dict) -> dict:
    """refs 键可能是 int 或字符串化的 uid；归一为 int。"""
    out: dict[Any, dict] = {}
    for key, value in refs.items():
        try:
            out[int(key)] = value
        except (TypeError, ValueError):
            out[key] = value
    return out


def _materialize(refs: dict, uid: Any) -> dict:
    """把 to_json 的 refs 图还原为无 uid 引用的 plain 节点（自底向上展开）。"""
    refs = _normalize_refs(refs)
    done: dict[Any, dict] = {}

    def expand(node: Any) -> Any:
        if isinstance(node, int) and node in refs:
            if node not in done:
                done[node] = _expand_plain(refs[node], refs, expand)
            return done[node]
        return node

    return _expand_plain(refs[uid], refs, expand)


def _expand_plain(plain: dict, refs: dict, expand) -> dict:
    out: dict[str, Any] = {}
    for key, value in plain.items():
        if key == "value" or key == "meta":
            # const 的 value 是字面量；meta 是注解（min/max/default 等字面量，
            # 不是 uid 引用），两者都原样保留
            out[key] = value
        elif isinstance(value, int) and value in refs:
            out[key] = expand(value)
        elif isinstance(value, dict) and set(value) == {"__jsExpr"}:
            out[key] = value
        elif isinstance(value, dict):
            out[key] = _expand_plain(value, refs, expand)
        elif isinstance(value, list):
            out[key] = [_expand_plain(v, refs, expand) if isinstance(v, dict) else expand(v)
                        for v in value]
        else:
            out[key] = value
    return out


def _wrap_expressions(schema: dict) -> dict:
    """在值位置把 schema 包进 `anyOf [core, $ref loaderExpression]`（对齐
    expressions(valuePosition=true)：value 位置允许 !!js 表达式结果）。"""
    if "properties" in schema:
        schema = {**schema,
                  "properties": {k: _wrap_expressions(v) for k, v in schema["properties"].items()}}
    for key in ("items", "additionalProperties"):
        if key in schema and isinstance(schema[key], dict):
            schema = {**schema, key: _wrap_expressions(schema[key])}
    if "prefixItems" in schema:
        schema = {**schema, "prefixItems": [_wrap_expressions(v) for v in schema["prefixItems"]]}
    if "anyOf" in schema:
        schema = {**schema, "anyOf": [_wrap_expressions(v) for v in schema["anyOf"]]}
    annotations: dict[str, Any] = {}
    for key in ("title", "description", "default", "$comment", "x-cordis"):
        if key in schema:
            annotations[key] = schema[key]
    core = {k: v for k, v in schema.items() if k not in annotations}
    return {**annotations, "anyOf": [core, {"$ref": "#/$defs/loaderExpression"}]}


# ---------------------------------------------------------------------------
# collect + document 组装
# ---------------------------------------------------------------------------


def _is_js_expr(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"__jsExpr"}


def _validate_entry(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError('each entry must be a mapping with a literal plugin name')
    name = value.get("name")
    if name is None:
        name = value.get("module")
    if not isinstance(name, str) or name == "":
        raise ValueError('each entry must be a mapping with a literal plugin name')
    for key in ("id", "name"):
        if value.get(key) is not None and not isinstance(value[key], str):
            raise ValueError(f"{key} must be a literal string")
    group = value.get("group")
    if group is not None and not isinstance(group, bool):
        raise ValueError("group must be a literal boolean or null")
    validated = dict(value)
    validated["name"] = name
    return validated


def _config_of(module_name: str) -> Any:
    """导入模块并读 Config 属性（对齐 configOf(plugin)）。

    解析失败 / 无 Config → None（条目投影为 unknownConfig）；Config 非 schemastery
    图 → 报 unsupported（由调用方捕获）。
    """
    try:
        module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 - 无法导入即无 schema（absent）
        return None
    return getattr(module, "Config", None)


def _is_native_schema(value: Any) -> bool:
    """schemastery 身份检查（对齐 native.ts：Symbol.for('schemastery')）。
    mini 用 isinstance 判定 core.schema.Schema 实例或其 to_json plain 图。"""
    from ..core.schema import Schema
    if isinstance(value, Schema):
        return True
    if isinstance(value, dict) and value.get("type") and isinstance(value.get("meta"), dict):
        return True
    return False


def collect_config_schemas(entries: list[dict], diagnostics: list[dict]) -> tuple[list[dict], dict]:
    """逐行收集配置 schema（对齐 collectConfigSchemas 的 mini 载体）。

    @param entries compose_entries 组合出的条目表。
    @param diagnostics 既有组合诊断（被拷贝进返回）。
    @returns (ConfigSchemaEntry[], $defs 追加定义)。
    """
    result: list[dict] = []
    definitions: dict[str, dict] = {}
    projections: dict[int, Projection] = {}

    def walk(rows: list[dict], prefix: str) -> None:
        for index, row in enumerate(rows):
            path = f"{prefix}/{index}"
            entry: dict[str, Any] = {"path": path, "status": "error"}
            if isinstance(row, dict):
                if isinstance(row.get("id"), str):
                    entry["id"] = row["id"]
                row_name = row.get("name") or row.get("module")
                if isinstance(row_name, str):
                    entry["name"] = row_name
            result.append(entry)
            try:
                validated = _validate_entry(row)
            except ValueError as error:
                diagnostics.append({"level": "error", "path": path,
                                    "message": str(error)})
                continue
            entry["status"] = "absent"
            schema = None
            try:
                module_name = validated.get("module") or validated["name"]
                if module_name.startswith("cordis:"):
                    name = module_name[len("cordis:"):]
                    if name not in ("group", "include"):
                        raise ValueError(f"unknown Cordis builtin {module_name!r}")
                    schema = None
                    entry["tree"] = name
                    entry["status"] = "absent"
                    if name == "group" and isinstance(validated.get("config"), list):
                        walk(validated["config"], f"{path}/config")
                    elif name == "include":
                        include_config = validated.get("config")
                        if isinstance(include_config, dict) and not _is_js_expr(include_config):
                            _walk_include(include_config, row, path, walk, diagnostics)
                    continue
                candidate = _config_of(module_name)
                if candidate is not None and _is_native_schema(candidate):
                    entry["status"] = "schema"
                    schema = candidate
                elif candidate is not None:
                    entry["status"] = "unsupported"
                    diagnostics.append({"level": "error", "path": path,
                                        "message": "Config is not a native Schemastery schema"})
            except Exception as error:  # noqa: BLE001 - 导入/投影失败 → error
                entry["status"] = "error"
                diagnostics.append({"level": "error", "path": path,
                                    "message": str(error)})
            if schema is not None:
                try:
                    native = schema.to_json() if hasattr(schema, "to_json") else schema
                    projection = projections.get(id(schema))
                    if projection is None:
                        projection = project_native_schema(native, f"config{len(projections)}")
                        projections[id(schema)] = projection
                        definitions[f"config{len(projections) - 1}"] = projection.schema
                        definitions.update(projection.definitions)
                    entry["configRef"] = f"#/$defs/config{len(projections) - 1}"
                    if projection.limitations:
                        entry["status"] = "partial"
                        for message in projection.limitations:
                            diagnostics.append({"level": "warning", "path": path,
                                                "message": message})
                except Exception as error:  # noqa: BLE001 - 投影失败 → error
                    entry["status"] = "error"
                    diagnostics.append({"level": "error", "path": path,
                                        "message": str(error)})
            entry.setdefault("configRef", "#/$defs/unknownConfig")

    def _walk_include(config: dict, row: dict, path: str, walk_fn, diagnostics: list) -> None:
        include_path = config.get("path")
        if not isinstance(include_path, str):
            diagnostics.append({"level": "error", "path": path,
                                "message": "include path must be literal"})
            return
        try:
            data = load_document(include_path, "miniharness", "include")
        except RuntimeError as error:
            diagnostics.append({"level": "error", "path": path, "message": str(error)})
            return
        if not isinstance(data, list):
            diagnostics.append({"level": "error", "path": path,
                                "message": "include config must be a literal entry list"})
            return
        patches = config.get("patches")
        try:
            rows = apply_entry_patches(data, patches or [])
        except Exception as error:  # noqa: BLE001
            diagnostics.append({"level": "error", "path": path,
                                "message": f"include patches could not be composed: {error}"})
            return
        walk_fn(rows, f"{path}/include")

    walk(entries, "")
    return result, definitions


def generate_config_schema(
    profile: Any,
    layers: list[list[dict]],
    install_anchor: str,
) -> dict:
    """为已准备 profile 的有序补丁层生成 JSON Schema 2020-12 文档。

    不读 profile manifest；`profile.skipped_bundles` 里每条选中的 bundle 记一条
    error 诊断（对齐上游读取已加载 profile 的 skippedBundles 而非重读 manifest）。

    @param profile 已加载的 profile（Profile 对象，读其 skipped_bundles）。
    @param layers 有序补丁层（含调用方选的 home/argv overlay）。
    @param install_anchor 安装锚点（mini 载体为占位——无 npm bundle 解析）。
    @returns ConfigSchemaDump（JSON Schema 文档 + x-cordis 注解）。
    """
    diagnostics: list[dict] = []
    for skipped in getattr(profile, "skipped_bundles", ()):
        diagnostics.append({
            "level": "error",
            "message": f"Selected profile bundle {json.dumps(skipped.package_name)} "
                       f"could not be loaded; repair or remove its bundle selection.",
        })
    entries = compose_entries(layers)
    collected, extra_definitions = collect_config_schemas(entries, diagnostics)
    return build_config_schema_document(
        profile.name, collected, extra_definitions, diagnostics)


def build_config_schema_document(
    profile: str,
    collected: list[dict],
    extra_definitions: dict,
    diagnostics: list[dict],
) -> dict:
    """组装 JSON Schema 文档（对齐 buildConfigSchemaDocument）。

    @param profile profile 名。
    @param collected ConfigSchemaEntry[]（含 configRef/tree/status）。
    @param extra_definitions 各插件 Config 投影的 $defs。
    @param diagnostics 组合与投影诊断。
    """
    definitions: dict[str, Any] = {
        "loaderExpression": _loader_expression_schema(),
        "entryMetadata": {"type": "object",
                          "properties": _entry_metadata_properties()},
        "entryList": {"type": "array", "items": _ref("entry")},
        "patchList": {"type": "array", "items": _ref("patch")},
        "unknownConfig": {"$comment": "No projected Config schema is available; this is "
                          "unknown configuration, not a prohibition on fields."},
        "includeConfig": {
            "type": "object", "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "YAML/JSON filename resolved "
                         "relative to the owning Loader tree. This field is literal."},
                "initial": _ref("entryList"),
                "patches": {"type": "array", "items": _ref("includePatch")},
                "enableLogs": {"type": "boolean"},
            },
        },
        "includePatch": _patch_structure(),
    }
    definitions.update(extra_definitions)

    names: dict[str, set] = {}
    for entry in collected:
        if entry.get("name") is not None:
            names.setdefault(entry["name"], set()).add(entry.get("configRef") or "#/$defs/unknownConfig")

    entry_rules: list[dict] = []
    for name, choices in sorted(names.items()):
        validated: dict = {"properties": {"config": {"anyOf": [_ref_from(c) for c in sorted(choices)]}}}
        entry_rules.append({
            "if": {"properties": {"name": {"const": name}}, "required": ["name"]},
            "then": validated,
        })
    definitions["entry"] = {
        "type": "object", "required": ["name"],
        "allOf": [_ref("entryMetadata"), *entry_rules],
        "description": "Loader entry. Unknown metadata and plugin names are accepted; "
                       "plugin-specific validation is available only for the names "
                       "collected in this profile.",
    }
    definitions["patch"] = {
        **_patch_structure(),
        "allOf": [_ref("entryMetadata")],
    }

    complete = (
        not any(d.get("level") == "error" for d in diagnostics)
        and not any(e.get("status") in ("partial", "unsupported", "error") for e in collected)
        and all(len(choices) == 1 for choices in names.values())
    )
    return {
        "$schema": SCHEMA_DIALECT,
        "title": f"Cordis configuration for profile {profile}",
        "description": "Describes the parsed entry-list YAML printed by --dump-config. "
                       "Use $defs.patchList for a profile/home/CLI overlay. Parse !!js "
                       "with the Cordis entry-list YAML dialect.",
        "$comment": "Bundle, profile, home, and CLI layers apply in that order. A patch "
                    "config replaces the whole config. JSON Schema defaults are "
                    "annotations; Schemastery validates a fallback on null/omission.",
        "type": "array", "items": _ref("entry"), "$defs": definitions,
        "x-cordis": {
            "profile": profile,
            "complete": complete,
            "entries": collected,
            "diagnostics": diagnostics,
            "patchSchema": "#/$defs/patchList",
        },
    }


def _ref_from(reference: str) -> dict:
    return {"$ref": reference}