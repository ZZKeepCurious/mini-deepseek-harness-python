"""第 7 章：活动插件包清单扩展 —— 官方 DeepSeek 请求的 `dsh_plugin_packages`
（上游 packages/llm/plugin-package-inventory-deepseek）。

每次官方请求携带当前活动插件的包身份清单：`{version: 1, packages: [{name, version?}]}`。
宿主条目与本请求所属 agent 的常驻 preset 在请求时解析；已安装依赖与无包身份的
插件 fiber 被排除。

mini 载体：活动条目来自 `ctx.registry` 中至少有一个 ACTIVE fiber 的注册项；包身份
经 `importlib.metadata.packages_distributions()` 从模块顶层名映射到发行版，版本取自
发行版元数据——**版本可选**：缺失/无法解析时只给 `name`（上游 identityFromManifest
同款）。mini 没有 Loader entry tree 与 agentPresets 组合检查（preset live 挂载见
P0-4a），故请求所属 agent 的 preset 条目暂不并入（载体差异，见 verified-diffs）。
"""

from __future__ import annotations

import logging
from importlib import metadata

from ..core.scope import FiberState

logger = logging.getLogger(__name__)


def _distribution_of(module_name: str, distributions: dict) -> str | None:
    """模块顶层名 → 拥有它的发行版名（无法映射 → None，视为无包身份的松散模块）。"""
    if not module_name:
        return None
    top_level = module_name.split(".", 1)[0]
    owners = distributions.get(top_level)
    if not owners:
        return None
    return owners[0]


def _identity_from_distribution(distribution: str) -> dict | None:
    """发行版身份：name 必填，version 仅当非空白字符串时带上（可选 + 容错）。"""
    try:
        package_metadata = metadata.metadata(distribution)
    except metadata.PackageNotFoundError:
        return None
    name = package_metadata.get("Name")
    if not isinstance(name, str) or name.strip() == "":
        return None
    try:
        version = metadata.version(distribution)
    except metadata.PackageNotFoundError:
        version = None
    if isinstance(version, str) and version.strip() != "":
        return {"name": name, "version": version}
    return {"name": name}


def collect_active_plugin_packages(ctx) -> list[dict]:
    """收集一次请求可用的活动包身份（上游 collectActivePluginPackages）。

    去重键为 (name, version)；按 (name, version) 文本序稳定排序（与宿主 ICU/locale
    无关）。
    """
    distributions = metadata.packages_distributions()
    unreadable: set[str] = set()
    unique: dict[tuple, dict] = {}
    for _key, runtime in ctx.registry.entries():
        fibers = runtime.get("fibers") or []
        if not any(getattr(fiber, "state", None) == FiberState.ACTIVE for fiber in fibers):
            continue
        callback = runtime.get("callback")
        module_name = getattr(callback, "__module__", "") or ""
        try:
            distribution = _distribution_of(module_name, distributions)
            identity = (None if distribution is None
                        else _identity_from_distribution(distribution))
        except Exception as error:
            if module_name not in unreadable:
                unreadable.add(module_name)
                logger.warning(
                    "plugin-package-inventory-deepseek: omitting unreadable package "
                    "identity for %s: %r", module_name, error)
            continue
        if identity is None:
            continue
        unique[(identity["name"], identity.get("version", ""))] = identity
    return sorted(unique.values(),
                  key=lambda item: (item["name"], item.get("version", "")))


def install_plugin_package_inventory(ctx, registry, *, enabled: bool = True):
    """把 `dsh_plugin_packages` 贡献注册到 DeepSeek 请求扩展注册表（上游 apply）。

    @param ctx - 携带活动插件注册表的上下文（经 `ctx.registry` 读取）。
    @param registry - `ExtensionRegistry`。
    @param enabled - False 时不注册（上游 config.enabled 缺省 true）。
    @returns disposer；enabled=False 时为 no-op。
    """
    if not enabled:
        return lambda: None

    def prepare(_request: dict) -> dict:
        return {"value": {
            "version": 1,
            "packages": collect_active_plugin_packages(ctx),
        }}

    return registry.register("dsh_plugin_packages", {"prepare": prepare})


__all__ = [
    "collect_active_plugin_packages",
    "install_plugin_package_inventory",
]
