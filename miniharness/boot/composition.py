"""第 5/7 章补充：YAML 配置载体 + !!js 表达式子集 + 组合 dump 渲染。

对应 dsh 真实源码：
- packages/boot/app-boot/src/index.ts（loadEnv / loadOverlayPatches）
- packages/boot/app-boot/src/config-dump.ts（renderConfigDump）
- apps/cli/src/args.ts（--patch / --dump-config / --dump-default-config 语义）

上游语义（已核实）：
- !!js tag → {__jsExpr: source} 节点，激活时求值（process.env.X 等）
- 读失败 / 解析失败 / !!js 无体 / 非顶层数组 / 条目非对象 → fail loud
- dump 是 boot-free 的只读组合；层注释 '# == <label>' 分隔来源；!!js 原样打印；
  skipped patch → warn 不失败；输出单文档可再加载
- loadEnv：.env 缺失（ENOENT）静默，其它错误 warn

mini 简化（须标注）：
- !!js 求值仅支持 process.env.<NAME> 完整匹配；其它表达式 fail loud
  （上游是 JS eval 全量表达式，mini 不求值 JS）
- 求值时机为读取时（上游为激活时）
- dump 无 pyyaml 时退化为 JSON（无行注释，仍可再加载）
- .env 解析复用凭据层 parse_dotenv（宽松跳过坏行）
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from .dotenv import BootstrapEnvNameError, is_bootstrap_only, parse_dotenv

__all__ = [
    "apply_patch",
    "compose_with_origins",
    "evaluate_js_expr",
    "load_composition",
    "load_document",
    "load_dotenv_file",
    "load_patch_list",
    "render_composition_dump",
    "resolve_js_exprs",
]

JS_ENV_EXPR = re.compile(r"^process\.env\.([A-Za-z_][A-Za-z0-9_]*)$")
_JS_TAG = "tag:yaml.org,2002:js"

try:  # pyyaml 为可选依赖（stdlib 优先的例外之一，httpx 之外，文档已标注）
    import yaml
except ImportError:  # pragma: no cover - 依赖缺失路径
    yaml = None


def _js_constructor(loader: Any, node: Any) -> dict[str, str]:
    """!!js <expr> → {'__jsExpr': <expr>}；无体/非标量 → 解析失败（对齐上游）。"""
    if not isinstance(node, yaml.nodes.ScalarNode):  # type: ignore[union-attr]
        raise ValueError("!!js 表达式必须为标量")
    text = node.value
    if not text or not text.strip():
        raise ValueError("!!js 表达式缺少内容")
    return {"__jsExpr": text}


if yaml is not None:
    yaml.SafeLoader.add_constructor(_JS_TAG, _js_constructor)


def _load_yaml_text(text: str) -> Any:
    if yaml is None:
        raise RuntimeError("需要可选依赖 PyYAML：pip install pyyaml")
    return yaml.safe_load(text)


def evaluate_js_expr(expr: str, environ: dict[str, str] | None = None) -> str:
    """!!js 表达式求值：仅支持 process.env.<NAME> 完整匹配，其它 fail loud。"""
    m = JS_ENV_EXPR.match(expr.strip())
    if not m:
        raise ValueError(
            f"不支持的 !!js 表达式: {expr!r}（mini 仅支持 process.env.<NAME>）"
        )
    return (environ if environ is not None else os.environ).get(m.group(1), "")


def resolve_js_exprs(value: Any, environ: dict[str, str] | None = None) -> Any:
    """递归求值 __jsExpr 节点（读取时求值，上游为激活时 —— 简化标注）。"""
    if isinstance(value, dict):
        if set(value) == {"__jsExpr"} and isinstance(value["__jsExpr"], str):
            return evaluate_js_expr(value["__jsExpr"], environ)
        return {k: resolve_js_exprs(v, environ) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_js_exprs(v, environ) for v in value]
    return value


def load_document(
    path: str | Path,
    bin_name: str,
    what: str,
) -> Any:
    """按扩展名加载 JSON / YAML 文档；读或解析失败 → fail loud（带前缀）。"""
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"{bin_name}: failed to read {what} {path}: {e}") from e
    suffix = p.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            return _load_yaml_text(raw)
        if suffix in (".json", ""):
            return json.loads(raw)
        raise RuntimeError(f"未知配置扩展名: {p.suffix!r}（支持 .json/.yaml/.yml）")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"{bin_name}: failed to parse {what} {path}: {e}") from e


def load_composition(
    config_path: str | Path,
    bin_name: str = "miniharness",
) -> list[dict]:
    """加载组合配置文件：顶层对象（plugins）或顶层条目数组（dump 输出形态）。"""
    data = load_document(config_path, bin_name, "config")
    if isinstance(data, list):
        plugins = data
    elif isinstance(data, dict):
        plugins = data.get("plugins", [])
    else:
        raise RuntimeError(f"{bin_name}: failed to parse config {config_path}: 顶层必须是对象或数组")
    if not isinstance(plugins, list):
        raise RuntimeError(f"{bin_name}: failed to parse config {config_path}: plugins 必须是数组")
    entries = []
    for i, e in enumerate(plugins):
        if not isinstance(e, dict):
            raise RuntimeError(f"{bin_name}: failed to parse config {config_path}: 条目 {i} 必须是对象")
        entries.append(e)
    return entries


def load_patch_list(
    patch_path: str | Path,
    bin_name: str = "miniharness",
    label: str = "patches",
) -> list[dict]:
    """加载补丁文件：顶层必须为数组、条目必须为对象（对齐 parsePatchList）。

    label 参数化进错误消息（上游 'patches' / 'overlay'，index.ts:315）；
    条目序号 1 起（上游 `entry ${index + 1}`，index.ts:332）。
    """
    data = load_document(patch_path, bin_name, label)
    if not isinstance(data, list):
        raise RuntimeError(f"{bin_name}: failed to parse {label} {patch_path}: 顶层必须是数组")
    for i, p in enumerate(data):
        if not isinstance(p, dict):
            raise RuntimeError(
                f"{bin_name}: failed to parse {label} {patch_path}: 条目 {i + 1} 必须是对象")
    return data


def load_dotenv_file(
    path: str | Path,
    warn: Callable[[str], None] | None = None,
    bin_name: str = "miniharness",
    environ: dict[str, str] | None = None,
) -> None:
    """.env 加载：缺失（ENOENT）静默；其它错误 warn；已存在的 key 不覆盖。

    bootstrap-only 名字在任何值物化前整体拒绝（上游 readEnvLayer
    index.ts:153-162：解析一次、校验与物化用同一份条目）。"""
    env = environ if environ is not None else os.environ
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except OSError as e:
        if warn:
            warn(f"{bin_name}: failed to load .env: {e}")
        return
    values = parse_dotenv(raw)
    for key in values:
        if is_bootstrap_only(key):
            raise BootstrapEnvNameError(
                f'{bin_name}: {path} sets "{key}", which only the launching '
                "environment may set (it decides how this process starts, where "
                "its code and instructions load from, or how it reaches the "
                f"network); export {key} instead of putting it in a .env file"
            )
    for key, value in values.items():
        env.setdefault(key, value)


def apply_patch(
    entries: list[dict],
    patches: list[dict],
    on_missing: Callable[[str], None] | None = None,
) -> list[dict]:
    """补丁算法（纯函数，禁止复制粘贴别处实现）。

    归属说明：原在 boot.py（上游 app-boot 的 applyPatch），步骤 3 消除
    boot.py ↔ composition.py 循环依赖时移入本模块（同属 boot 族）。

    on_missing 缺省 → KeyError（fail loud）；传入 warn 回调 → 跳过该条继续
    （对齐上游：目标缺失是 per-entry Loader warning，boot 继续，index.ts:309-311）。
    """
    out = [dict(e) for e in entries]
    for patch in patches:
        if "replace" in patch:
            target_id = patch["replace"]["id"]
            new_cfg = patch["replace"]["config"]
            for e in out:
                if e["id"] == target_id:
                    e["config"] = dict(new_cfg)
                    break
            else:
                if on_missing is not None:
                    on_missing(target_id)
                    continue
                raise KeyError(f"patch 目标 id={target_id} 不存在")
        elif "insert" in patch:
            out.extend(dict(e) for e in patch["insert"])
        else:
            raise ValueError(f"未知补丁操作: {patch}")
    return out


def compose_with_origins(
    base_entries: list[dict],
    layers: list[tuple[str, list[dict]]],
    warn: Callable[[str], None] | None = None,
) -> tuple[list[dict], list[dict]]:
    """层叠补丁 + 逐行 provenance 追踪；目标缺失的补丁层 → warn 并跳过（对齐上游）。

    provenance 为 {origin, patchedBy}（上游 index.ts:422-436）：origin 恒为
    最初来源层 label（base 为 "base"），patchedBy 累积每个改写过该行的层 label。
    """
    combined = [dict(e) for e in base_entries]
    records: list[dict] = [{"origin": "base", "patchedBy": []} for _ in combined]

    for label, patches in layers:
        def _warn_missing(target_id: str, _label: str = label) -> None:
            if warn:
                warn(f"[{_label}] 补丁被跳过: patch 目标 id={target_id} 不存在")
        applied = apply_patch(combined, patches, on_missing=_warn_missing)
        prev_by_id = {e.get("id"): (i, rec) for i, (e, rec) in enumerate(zip(combined, records))}
        new_records: list[dict] = []
        for entry in applied:
            prev = prev_by_id.get(entry.get("id"))
            if prev is not None and entry == combined[prev[0]]:
                new_records.append(prev[1])  # 未变：provenance 原样保留
            elif prev is not None:
                # 同 id 被改写：origin 保留，层 label 累积进 patchedBy
                new_records.append({"origin": prev[1]["origin"],
                                    "patchedBy": [*prev[1]["patchedBy"], label]})
            else:
                new_records.append({"origin": label, "patchedBy": []})
        combined, records = applied, new_records
    return combined, records


def _represent_dict(dumper: Any, data: dict) -> Any:
    """__jsExpr 单键节点原样输出为 !!js 标量；其它 dict 走默认代表器。"""
    if set(data) == {"__jsExpr"} and isinstance(data.get("__jsExpr"), str):
        return dumper.represent_scalar(_JS_TAG, data["__jsExpr"], style="")
    return dumper.represent_dict(data)


if yaml is not None:
    class _Dumper(yaml.SafeDumper):
        pass

    _Dumper.add_representer(dict, _represent_dict)


def render_composition_dump(
    bin_name: str,
    base_label: str,
    base_entries: list[dict],
    layers: list[tuple[str, list[dict]]],
    warn: Callable[[str], None] | None = None,
) -> str:
    """只读组合渲染（boot-free）：行级 '# == <origin>[, patched by X, Y]' provenance
    注释（上游 groupedDump index.ts:460-462），!!js 原样，单文档可再加载。

    对齐 renderConfigDump（config-dump.spec.ts）：skipped patch → warn 不失败。
    无 pyyaml 时退化为 JSON 数组（无注释，简化标注）。
    """
    combined, records = compose_with_origins(base_entries, layers, warn)
    if yaml is None:
        return json.dumps(combined, ensure_ascii=False, indent=2)

    blocks: list[str] = []
    current_label: str | None = None
    for entry, record in zip(combined, records):
        origin = base_label if record["origin"] == "base" else record["origin"]
        label = (f"{origin}, patched by {', '.join(record['patchedBy'])}"
                 if record["patchedBy"] else origin)
        if label != current_label:
            blocks.append(f"# == {label}")
            current_label = label
        text = yaml.dump(
            [entry],
            Dumper=_Dumper,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        blocks.append(text.rstrip())
    return "\n".join(blocks) + "\n"