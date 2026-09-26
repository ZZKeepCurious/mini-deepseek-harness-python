"""`load_workspace_dependencies` 工具（对齐 packages/skill/tool-workspace-dependencies）。

读载体运行时清单 `runtime.json`（构建期元数据）+ `dependencies/` 目录（捆绑
CPython、site-packages、Node、pnpm），回答"捆绑运行时在哪里"——不改 PATH 或
包管理器设置。

契约（对齐上游 src/index.ts）：
  * `Config`：`source` 必填绝对路径（payload 目录，含 runtime.json +
    dependencies/）；`root` 可选（Harness home 下安装目录，设置则首次调用复制、
    省略则原位使用）。
  * `runtime.json`（PrimaryRuntimeManifest）：desktopVersion / platform
    (win32|darwin|linux) / arch (x64|arm64) / payloadDigest? / python /
    node? / pnpm?(要求 node 在场) / pythonPackages（分布名→版本映射）。
  * `parsePrimaryRuntime` 校验：非法 metadata（含重复分布名、legacy components
    归一、混合格式拒绝）→ `primary runtime: invalid metadata`；平台/架构不匹配
    → `primary runtime: incompatible platform or architecture`；路径种类不符 →
    `primary runtime: expected file|directory at <path>`；安装路径是符号链接 →
    `primary runtime: installation path is a filesystem link`。
  * 工具 schema：无参数，返回 `{python, node?, pnpm?, pythonPackages,
    nodePackages?, pythonDistributions}`；render = JSON.stringify(value, 2)。
  * 惰性 memoize：首次 execute 决定 resolve/install，成功缓存整个插件生命周期。

mini 载体差异（登记）：
  * mini 无捆绑 CPython/Node 载体——工具经配置的 `source`（runtime.json +
    dependencies/）解析路径（契约面原样）；未配置 source 时回落**运行中的
    Python 解释器**（`sys.executable` + `site-packages` + 已安装分布版本），
    语义等价（"当前可用运行时"）。
  * `installPrimaryRuntime` 的 staging cp → 校验 → 原子 swap（`.previous`
    保留树）用 `shutil.copytree` + `os.rename` 等价落地。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import site
import sys
import sysconfig
import tempfile
from pathlib import Path
from typing import Any

from ..core.tools import Tool

__all__ = [
    "parse_primary_runtime",
    "register_workspace_dependencies_tool",
    "resolve_primary_runtime",
    "workspace_dependency_paths",
]

_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$")
_PLATFORMS = ("win32", "darwin", "linux")
_ARCHES = ("x64", "arm64")
_PYTHON_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PYTHON_VERSION = re.compile(r"^\d[\w.!+-]*$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _is_distribution_map(value: Any) -> bool:
    return _is_record(value) and all(
        _PYTHON_NAME.match(str(name)) and isinstance(version, str)
        and _PYTHON_VERSION.match(version)
        for name, version in value.items())


def parse_primary_runtime(value: Any) -> dict:
    """校验 payload JSON 并归一 legacy components 字段（对齐 parsePrimaryRuntime）。"""
    if not _is_record(value):
        raise RuntimeError("primary runtime: invalid metadata")
    legacy = value.get("components") is not None
    versions = value.get("components") if legacy else value
    if not _is_record(versions) or (
            legacy and any(key in value for key in ("python", "node", "pnpm"))):
        raise RuntimeError("primary runtime: invalid metadata")
    desktop_version = value.get("desktopVersion")
    platform = value.get("platform")
    arch = value.get("arch")
    payload_digest = value.get("payloadDigest")
    python = versions.get("python")
    node = versions.get("node")
    pnpm = versions.get("pnpm")
    python_packages = {} if (legacy and value.get("pythonPackages") is None) \
        else value.get("pythonPackages")
    if not isinstance(desktop_version, str) or not desktop_version \
            or not isinstance(platform, str) or platform not in _PLATFORMS \
            or not isinstance(arch, str) or arch not in _ARCHES \
            or not (isinstance(python, str) and _VERSION.match(python)) \
            or (node is not None and not (isinstance(node, str) and _VERSION.match(node))) \
            or (pnpm is not None and not (isinstance(pnpm, str) and _VERSION.match(pnpm)
                                          and node is not None)) \
            or (payload_digest is not None
                and not (isinstance(payload_digest, str) and _SHA256.match(payload_digest))) \
            or not _is_distribution_map(python_packages):
        raise RuntimeError("primary runtime: invalid metadata")
    distributions = {}
    for name, version in python_packages.items():
        key = re.sub(r"[-_.]+", "-", str(name)).lower()
        if key in distributions:
            raise RuntimeError("primary runtime: invalid metadata")
        distributions[key] = version
    # 归一化键用于 duplicate 检测（同上游 Map 语义）；返回原始 pythonPackages。
    if legacy:
        for name in ("numpy", "pandas"):
            legacy_version = versions.get(name)
            if not (isinstance(legacy_version, str) and _VERSION.match(legacy_version)):
                raise RuntimeError("primary runtime: invalid metadata")
            key = re.sub(r"[-_.]+", "-", name).lower()
            mapped = distributions.get(key)
            if mapped is not None and mapped != legacy_version:
                raise RuntimeError(f"primary runtime: conflicting {name} distribution version")
    out: dict[str, Any] = {
        "desktopVersion": desktop_version, "platform": platform, "arch": arch,
        "python": python,
    }
    if node is not None:
        out["node"] = node
    if pnpm is not None:
        out["pnpm"] = pnpm
    if payload_digest is not None:
        out["payloadDigest"] = payload_digest
    out["pythonPackages"] = python_packages
    return out


def _windows() -> bool:
    return os.name == "nt"


def _platform_tag() -> str:
    return "win32" if os.name == "nt" else "darwin" if sys.platform == "darwin" else "linux"


def _arch_tag() -> str:
    machine = os.uname().machine if hasattr(os, "uname") else os.environ.get(
        "PROCESSOR_ARCHITECTURE", "AMD64")
    return "arm64" if "arm64" in str(machine).lower() or "aarch64" in str(machine).lower() else "x64"


def workspace_dependency_paths(root: str, manifest: dict) -> dict:
    """平台相关的解释器与库目录解析（对齐 workspaceDependencyPaths）。"""
    dependencies = os.path.join(root, "dependencies")
    windows = manifest["platform"] == "win32"
    out: dict[str, Any] = {
        "python": os.path.join(
            dependencies, "python", "python.exe" if windows else "bin/python3"),
        "pythonPackages": os.path.join(
            dependencies, "python",
            "Lib" if windows else "lib/python"
            + ".".join(manifest["python"].split(".")[:2]),
            "site-packages"),
        "pythonDistributions": manifest["pythonPackages"],
    }
    if manifest.get("node") is not None:
        out["node"] = os.path.join(
            dependencies, "node", "bin", "node.exe" if windows else "node")
        out["nodePackages"] = os.path.join(dependencies, "node", "node_modules")
    if manifest.get("pnpm") is not None:
        out["pnpm"] = os.path.join(dependencies, "pnpm", "bin", "pnpm.mjs")
    return out


def _validate_payload_entries(paths: dict) -> None:
    for key, kind in (("python", "file"), ("node", "file"), ("pnpm", "file"),
                      ("pythonPackages", "directory"), ("nodePackages", "directory")):
        path = paths.get(key)
        if path is None:
            continue
        if kind == "file" and not os.path.isfile(path):
            raise RuntimeError(f"primary runtime: expected file at {path}")
        if kind == "directory" and not os.path.isdir(path):
            raise RuntimeError(f"primary runtime: expected directory at {path}")


def _compatible_manifest(source: str) -> dict:
    manifest_path = os.path.join(source, "runtime.json")
    if not os.path.isfile(manifest_path):
        raise RuntimeError(f"primary runtime: expected file at {manifest_path}")
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = parse_primary_runtime(json.load(handle))
    if manifest["platform"] != _platform_tag() or manifest["arch"] != _arch_tag():
        raise RuntimeError("primary runtime: incompatible platform or architecture")
    return manifest


def resolve_primary_runtime(source: str) -> dict:
    """原位校验 payload：读 metadata + 校验条目，不复制（对齐 resolvePrimaryRuntime）。"""
    paths = workspace_dependency_paths(source, _compatible_manifest(source))
    _validate_payload_entries(paths)
    return paths


def _python_native_runtime() -> dict:
    """未配置 payload 时的回落：运行中的 Python 解释器（mini 载体差异）。"""
    purelib = sysconfig.get_paths().get("purelib") or site.getsitepackages()[0]
    distributions = {}
    try:
        from importlib.metadata import distributions as _dists
        for dist in _dists():
            try:
                distributions[dist.metadata.get("Name", dist.metadata.get(
                    "name", ""))] = dist.version
            except Exception:  # noqa: BLE001 - 单分布读取失败跳过
                continue
    except Exception:  # noqa: BLE001 - 无 importlib.metadata 面（旧 Python）
        pass
    return {
        "python": sys.executable,
        "pythonPackages": purelib,
        "pythonDistributions": distributions,
    }


def _make_tool(resolve: Any, native: Any) -> Tool:
    def execute(_args: dict, _exec_: Any) -> dict:
        try:
            return resolve() if resolve is not None else native()
        except Exception as error:  # noqa: BLE001 - 工具错误结果
            raise RuntimeError(f"Error: {error}") from error

    def render(_args: dict, value: dict) -> list:
        return [{"type": "text", "text": json.dumps(value, indent=2)}]

    def present_call(_args: dict) -> dict:
        return {"card": "generic", "title": "Load workspace dependencies", "kind": "read"}

    return Tool(
        name="load_workspace_dependencies",
        description=(
            "Get absolute paths to bundled Python and library directories, plus "
            "bundled Python distribution versions. Node.js and pnpm paths are "
            "included when the payload provides them. Python includes numpy, "
            "pandas, python-docx, python-pptx, openpyxl, Pillow, lxml, and "
            "XlsxWriter. Use these libraries for Office files unless the user or "
            "workspace instructions select another environment. When Node.js and "
            "pnpm paths are returned, run pnpm with that Node executable and pnpm "
            "script path. This does not change PATH or package-manager settings."),
        parameters={},
        output={"schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "python": {"type": "string"},
                "node": {"type": "string"},
                "pnpm": {"type": "string"},
                "pythonPackages": {"type": "string"},
                "nodePackages": {"type": "string"},
                "pythonDistributions": {"type": "object"},
            },
        }},
        execute=execute,
        render=render,
        present_call=present_call,
    )


def register_workspace_dependencies_tool(tool_registry: Any, *,
                                         source: str | None = None,
                                         root: str | None = None) -> None:
    """把 `load_workspace_dependencies` 工具注册进 ToolRegistry。

    @param source payload 目录（含 runtime.json + dependencies/）。
    @param root 安装目录（设置则首次调用复制、省略则原位使用）。
    """
    if source is not None and not os.path.isabs(source):
        raise RuntimeError("workspace dependencies: source and root must be absolute paths")
    if root is not None and not os.path.isabs(root):
        raise RuntimeError("workspace dependencies: source and root must be absolute paths")

    cached: dict[str, Any] = {}

    def resolve() -> dict:
        if "value" in cached:
            return cached["value"]
        try:
            if source is not None and root is not None:
                value = _install_primary_runtime(source, root)
            elif source is not None:
                value = resolve_primary_runtime(source)
            else:
                value = _python_native_runtime()
            cached["value"] = value
            return value
        except Exception:
            cached.pop("value", None)
            raise

    tool_registry.register(_make_tool(resolve, None))


def _install_primary_runtime(source: str, root: str) -> dict:
    """把 payload 复制到安装目录，保留完整前树（对齐 installPrimaryRuntime）。"""
    manifest = _compatible_manifest(source)
    os.makedirs(os.path.dirname(root), exist_ok=True)
    previous = f"{root}.previous"
    if not os.path.lexists(root) and os.path.lexists(previous):
        os.rename(previous, root)
    if os.path.isfile(os.path.join(root, "runtime.json")):
        with open(os.path.join(root, "runtime.json"), encoding="utf-8") as handle:
            current = parse_primary_runtime(json.load(handle))
        if current == manifest:
            paths = workspace_dependency_paths(root, manifest)
            _validate_payload_entries(paths)
            return paths
    staging = tempfile.mkdtemp(prefix=".primary-runtime-",
                               dir=os.path.dirname(root))
    try:
        shutil.copytree(source, staging, dirs_exist_ok=True, symlinks=True)
        paths = workspace_dependency_paths(staging, manifest)
        _validate_payload_entries(paths)
        if os.path.lexists(previous):
            shutil.rmtree(previous)
        replacing = os.path.lexists(root)
        if replacing:
            os.rename(root, previous)
        try:
            os.rename(staging, root)
        except Exception:
            if replacing:
                os.rename(previous, root)
            raise
        if os.path.lexists(previous):
            shutil.rmtree(previous)
    finally:
        if os.path.lexists(staging):
            shutil.rmtree(staging)
    return workspace_dependency_paths(root, manifest)