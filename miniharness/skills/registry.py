"""skill 能力缝的服务端：分层注册表 + 模型侧渲染与校验。

上游对照：packages/skill/skill/src/index.ts（SkillRegistry + SkillProvider
接口 + renderSkillContent / escapeText / escapeAttr）。

契约（与上游逐条一致）：
  * 名字语法 SKILL_NAME = ^[a-z0-9]+(?:-[a-z0-9]+)*$（kebab-case）
  * 层语义：全局层 + 调用方 scope 层（近层同名直接遮蔽远层；rank 只在一层内
    决胜负）。runtime 注册秩 RUNTIME_RANK=250；provider 秩由 provider 自报
  * collect 缓存按 (cwd, scope 链, revision) 键控；incomplete 观察永不缓存；
    revision 抖动最多重试 MAX_COLLECT_ATTEMPTS=2 次
  * provider.list() 可返回候选数组（complete 简写）或 {candidates, complete}
    观察；抛错 → 跳过该 provider 且整体 cacheable=False（保留 last-good）
  * 候选/定义/运行时注册在入注册表时全量校验，坏条目 fail loud
  * invalidate → revision++ + 清缓存 + emit `skills/change`（监听器异常被
    容错，不能否决注册表变更）

mini 简化（有意保留，须在文档标注）：
  * 同步实现：list/get/snapshot 无 await，provider 直接同步返回（上游 async）
  * 无真实 AbortSignal：lookup 的 signal 字段不被消费（同步模型无等待点）
  * scope 键用 Context（AgentLoop 非 Context，tool_skill 传 agent.ctx）；
    无 per-agent 组合层，默认装配全部落在全局层
  * ctx.logger 缺失时退化为标准 logging
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from types import MappingProxyType
from typing import Any, Callable

from ..core.scope import Context

__all__ = [
    "BUNDLED_SKILL_RANK",
    "RUNTIME_PROVIDER",
    "RUNTIME_RANK",
    "SkillRegistry",
    "digest_catalog_entries",
    "escape_attr",
    "escape_text",
    "is_model_invocable",
    "is_skill_name",
    "is_user_invocable",
    "render_resource_hint",
    "render_skill_content",
]

logger = logging.getLogger("miniharness.skills")

SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RUNTIME_PROVIDER = "runtime"
RUNTIME_RANK = 250
BUNDLED_SKILL_RANK = 600
DEFAULT_COLLECT_CACHE_ENTRIES = 128
MAX_COLLECT_ATTEMPTS = 2


def is_skill_name(name: str) -> bool:
    """名字是否匹配公开 kebab-case skill 语法。"""
    return bool(SKILL_NAME.match(name))


def is_model_invocable(skill: dict) -> bool:
    """是否可对模型侧目录与加载器开放。"""
    return skill["invocation"]["modelInvocable"]


def is_user_invocable(skill: dict) -> bool:
    """是否可对人侧命令目录与加载器开放。"""
    return skill["invocation"]["userInvocable"]


# ---------- 模型侧渲染（逐字符对齐上游） ----------

def escape_text(value: str) -> str:
    """转义嵌入 skill 标记的散文（防 provider 文本开合框架标签）。"""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(value: str) -> str:
    """转义 skill 属性值（name 属性）。"""
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def render_resource_hint(skill: dict) -> list[str]:
    """resourceBase 的模型可见指引行（provider-managed / directory / url / opaque）。"""
    base = skill.get("resourceBase")
    if base is None:
        return [
            f'Resources for this skill are managed by provider "{escape_text(skill["provider"])}".',
            "Load referenced resources only as needed.",
        ]
    kind = base.get("kind")
    if kind == "directory":
        return [
            f"Base directory for this skill: {escape_text(base['path'])}",
            "Resolve relative paths mentioned by this skill against the base directory before using them. Load referenced resources only as needed.",
        ]
    if kind == "url":
        return [
            f"Base URL for this skill: {escape_text(base['url'])}",
            "Resolve relative URLs mentioned by this skill against the base URL before using them. Load referenced resources only as needed.",
        ]
    if kind == "opaque":
        return [
            f"Resources for this skill: {escape_text(base['description'])}",
            "Load referenced resources only as needed.",
        ]
    raise ValueError(f"SkillResourceBase.kind 未知: {kind!r}")


def render_skill_content(skill: dict) -> str:
    """渲染加载后的完整 skill（skill 工具结果与手势注入共用同一形状）。"""
    return "\n".join([
        f'<skill_content name="{escape_attr(skill["name"])}">',
        "<skill_resources>",
        *render_resource_hint(skill),
        "</skill_resources>",
        "",
        "<skill_instructions>",
        skill["content"],
        "</skill_instructions>",
        "</skill_content>",
    ])


# ---------- catalog digest（目录身份 = durable 条目，非渲染散文） ----------

def digest_catalog_entries(entries: list[dict]) -> str:
    """对目录条目列表取 sha256（每条 JSON.stringify([name, description]) 逐行）。

    与上游 digestCatalogEntries 一致：JSON 转义使边界精确（描述里任何分隔符
    都是合法字符，只有引号能让边界唯一）。
    """
    canonical = "\n".join(
        json.dumps([entry["name"], entry["description"]], ensure_ascii=False, separators=(",", ":"))
        for entry in entries
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------- 校验 ----------

def _validate_invocation(invocation: Any, subject: str) -> None:
    if invocation is None:
        return
    if not isinstance(invocation, dict):
        raise TypeError(f"{subject} 的 invocation 策略非对象")
    for key in ("modelInvocable", "userInvocable"):
        if not isinstance(invocation.get(key), bool):
            raise TypeError(f"{subject} 的 invocation.{key} 非布尔")


def _validate_name(name: Any, subject: str) -> None:
    if not isinstance(name, str):
        raise TypeError(f"{subject} 返回非字符串 skill 名")
    if not is_skill_name(name):
        raise ValueError(f"{subject} 返回非法 skill 名 {name!r}")


def validate_candidate(candidate: dict, provider_name: str) -> None:
    """候选入注册表前全量校验（坏条目 fail loud）。"""
    _validate_name(candidate.get("name"), f'skill provider "{provider_name}"')
    name = candidate["name"]
    description = candidate.get("description")
    if not isinstance(description, str):
        raise TypeError(f'skill provider "{provider_name}" 返回 skill "{name}" 的 description 非字符串')
    if description == "":
        raise ValueError(f'skill provider "{provider_name}" 返回 skill "{name}" 缺少 description')
    _validate_invocation(candidate.get("invocation"), f'skill provider "{provider_name}" 返回 skill "{name}"')
    when_to_use = candidate.get("whenToUse")
    if when_to_use is not None and not isinstance(when_to_use, str):
        raise TypeError(f'skill provider "{provider_name}" 返回 skill "{name}" 的 whenToUse 非字符串')
    if not isinstance(candidate.get("source"), str):
        raise TypeError(f'skill provider "{provider_name}" 返回 skill "{name}" 的 source 非字符串')
    if not isinstance(candidate.get("rank"), (int, float)) or not _is_finite(candidate["rank"]):
        raise ValueError(f'skill provider "{provider_name}" 返回 skill "{name}" 的 rank 非法')
    if not isinstance(candidate.get("provider"), str):
        raise TypeError(f'skill provider "{provider_name}" 返回 skill "{name}" 的 provider 非字符串')
    if candidate["provider"] != provider_name:
        raise ValueError(
            f'skill provider "{provider_name}" 返回 skill "{name}" 但 provider 为 "{candidate["provider"]}"'
        )
    path = candidate.get("path")
    if path is not None and not isinstance(path, str):
        raise TypeError(f'skill provider "{provider_name}" 返回 skill "{name}" 的 path 非字符串')


def validate_definition(skill: dict) -> None:
    """校验 provider 加载出的完整定义（对齐上游 validateDefinition）。"""
    _validate_name(skill.get("name"), "加载的 skill")
    name = skill["name"]
    if not isinstance(skill.get("description"), str) or skill["description"] == "":
        raise TypeError(f'loaded skill "{name}" requires a description')
    _validate_invocation(skill.get("invocation"), f'loaded skill "{name}"')
    when_to_use = skill.get("whenToUse")
    if when_to_use is not None and not isinstance(when_to_use, str):
        raise TypeError(f'loaded skill "{name}" whenToUse must be a string')
    if not isinstance(skill.get("source"), str):
        raise TypeError(f'loaded skill "{name}" source must be a string')
    if not isinstance(skill.get("provider"), str):
        raise TypeError(f'loaded skill "{name}" provider must be a string')
    if not isinstance(skill.get("content"), str):
        raise TypeError(f'loaded skill "{name}" content must be a string')
    path = skill.get("path")
    if path is not None and not isinstance(path, str):
        raise TypeError(f'loaded skill "{name}" path must be a string')


def _is_finite(value: Any) -> bool:
    try:
        import math
        return math.isfinite(value)
    except (TypeError, ValueError):
        return False


def validate_runtime_skill(skill: dict) -> None:
    """运行时注册校验（对齐上游 validateRuntimeSkill）。"""
    _validate_name(skill.get("name"), "runtime skill")
    if not isinstance(skill.get("description"), str) or skill["description"] == "":
        raise ValueError(f'skill "{skill["name"]}" requires a description')
    _validate_invocation(skill.get("invocation"), f'runtime skill "{skill["name"]}"')


# ---------- 分层注册表 ----------

class _SkillLayer:
    """一个 scope 的完整 skill 贡献（providers 按注册序 + runtime map）。"""

    def __init__(self, scope: Context | None):
        self.scope = scope
        self.providers: dict[str, dict] = {}      # name -> {provider, order}
        self.runtime: dict[str, dict] = {}        # name -> SkillDefinition


class SkillRegistry:
    """分层 skill 注册表：merge 全局 + scope 链，近层同名直接遮蔽。

    服务键 `skills`（ctx.provide("skills", self)）；提供 list / snapshot / get /
    register / register_provider。invalidate 发出 `skills/change`（emit）。
    """

    def __init__(self, ctx: Context, config: dict | None = None):
        self.ctx = ctx
        cfg = config or {}
        max_entries = cfg.get("collectCacheMaxEntries", DEFAULT_COLLECT_CACHE_ENTRIES)
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 1:
            raise ValueError(f"skill: collectCacheMaxEntries must be a positive integer, got {max_entries!r}")
        self.collect_cache_max_entries = max_entries
        self._layers: dict[int, _SkillLayer] = {}
        self._global = _SkillLayer(None)
        self._collect_cache: dict[str, dict[str, dict]] = {}
        self._revision = 0
        self._next_provider_order = 0
        self._scope_ids: dict[int, int] = {}
        self._next_scope_id = 1
        ctx.provide("skills", self)

    # ---------- 层与 scope ----------

    def _scope_id(self, scope: Context | None) -> int | None:
        if scope is None:
            return None
        key = id(scope)
        if key not in self._scope_ids:
            self._scope_ids[key] = self._next_scope_id
            self._next_scope_id += 1
        return self._scope_ids[key]

    def _chain(self, scope: Context | None) -> list[Context]:
        """scope 的父链（近→远）；None → 空。"""
        chain: list[Context] = []
        node = scope
        while node is not None:
            chain.append(node)
            node = node.parent
        return chain

    def _layer_for(self, scope: Context | None) -> _SkillLayer:
        if scope is None:
            return self._global
        key = id(scope)
        if key not in self._layers:
            self._layers[key] = _SkillLayer(scope)
        return self._layers[key]

    def _chain_layers(self, scope: Context | None) -> list[_SkillLayer]:
        """scope 链上已有的层（远祖→近），用于 collect 合并。"""
        layers: list[_SkillLayer] = []
        for node in reversed(self._chain(scope)):
            layer = self._layers.get(id(node))
            if layer is not None:
                layers.append(layer)
        return layers

    # ---------- 注册 ----------

    def register_provider(self, create: Callable[[Any], Any], scope: Context | None = None) -> Callable[[], None]:
        """注册一个 provider（同步工厂，返回对象或 {name,list,get} dict）。

        重复名 / 保留名 'runtime' fail loud。返回 disposer：注销 provider 并
        invalidate。mini 无 Cordis effect，直接维护层内表 + revision 递增
        （文档标注生命周期简化）。
        """
        raw = create({"invalidate": self.invalidate_cache})
        if isinstance(raw, dict):
            name, list_fn, get_fn = raw["name"], raw["list"], raw["get"]
        else:
            name, list_fn, get_fn = raw.name, raw.list, raw.get
        if name == RUNTIME_PROVIDER:
            raise ValueError(f'"{RUNTIME_PROVIDER}" is reserved for runtime skill registrations')
        layer = self._layer_for(scope)
        if name in layer.providers:
            raise ValueError(f'a skill provider named "{name}" is already registered')
        order = self._next_provider_order
        self._next_provider_order += 1
        provider = {"name": name, "list": list_fn, "get": get_fn}
        layer.providers[name] = {"provider": provider, "order": order}

        def dispose() -> None:
            if layer.providers.get(name) is not None:
                del layer.providers[name]
                # 调用 provider 自身的 dispose（如 watcher 清理）
                if hasattr(raw, "dispose"):
                    try:
                        raw.dispose()
                    except Exception:
                        logger.debug("provider %s dispose failed", name, exc_info=True)
                self.invalidate_cache()

        self.invalidate_cache()
        return dispose

    def register(self, skill: dict, scope: Context | None = None) -> Callable[[], None]:
        """运行时注册一个 skill（provider 缺省 'runtime'，invocation 缺省双开）。

        同层同名 first-wins：重复注册 warn + 返回 no-op disposer（不能移除胜者）。
        """
        validate_runtime_skill(skill)
        layer = self._layer_for(scope)
        if skill["name"] in layer.runtime:
            logger.warning('runtime skill "%s" ignored because it is already registered', skill["name"])
            return lambda: None
        definition = {
            **skill,
            "invocation": skill.get("invocation") or {"modelInvocable": True, "userInvocable": True},
            "provider": skill.get("provider") or RUNTIME_PROVIDER,
        }
        layer.runtime[definition["name"]] = definition

        def dispose() -> None:
            if layer.runtime.get(definition["name"]) is not None:
                del layer.runtime[definition["name"]]
                self.invalidate_cache()

        self.invalidate_cache()
        return dispose

    # ---------- 读取 ----------

    def list(self, options: dict | None = None) -> list[dict]:
        """排序的 invocation-neutral 摘要（模型/用户可见性由消费边界决定）。"""
        return self.snapshot(options)["skills"]

    def snapshot(self, options: dict | None = None) -> dict:
        """当前目录观察：{skills: 排序摘要, complete: 是否在稳定 revision 内完成}。"""
        options = options or {}
        collected = self._collect(options)
        skills = sorted(
            (self._to_summary(entry["candidate"]) for entry in collected["entries"].values()),
            key=lambda s: s["name"],
        )
        return {"skills": skills, "complete": collected["cacheable"]}

    def get(self, name: str, options: dict | None = None) -> dict | None:
        """加载并校验指定名字的胜出候选；找不到 / 名字非法返回 None。

        定义名与候选名不一致 → 失效该条目并返回 None（对齐上游 invalidateEntry）。
        """
        if not is_skill_name(name):
            return None
        options = options or {}
        collected = self._collect(options)
        entry = collected["entries"].get(name)
        if entry is None:
            return None
        provider = entry["provider"]
        definition = provider["get"](entry["candidate"], options)
        if definition is None:
            return None
        validate_definition(definition)
        if definition["name"] != entry["candidate"]["name"]:
            self._invalidate_entry(entry)
            return None
        return definition

    # ---------- collect ----------

    def _collect(self, options: dict) -> dict:
        attempt = 1
        while True:
            revision = self._revision
            key = self._collect_cache_key(options.get("cwd"), self._chain(options.get("scope")), revision)
            cached = self._collect_cache.get(key)
            if cached is not None:
                return {"entries": cached, "cacheable": True}
            result = self._collect_fresh(options)
            if revision != self._revision:
                if attempt < MAX_COLLECT_ATTEMPTS:
                    attempt += 1
                    continue
                return {"entries": result["entries"], "cacheable": False}
            if result["cacheable"]:
                self._collect_cache[key] = result["entries"]
                if len(self._collect_cache) > self.collect_cache_max_entries:
                    oldest = next(iter(self._collect_cache))
                    del self._collect_cache[oldest]
            return result

    def _collect_cache_key(self, cwd: str | None, chain: list[Context], revision: int) -> str:
        return json.dumps({
            "cwd": cwd,
            "scopes": [self._scope_id(scope) for scope in chain],
            "revision": revision,
        })

    def _collect_fresh(self, options: dict) -> dict:
        # 全局先，再远祖→近 scope 逐层覆盖：近层同名直接替换远层
        layers = [self._global, *self._chain_layers(options.get("scope"))]
        merged: dict[str, dict] = {}
        cacheable = True
        for layer in layers:
            collected = self._collect_layer(layer, options)
            if not collected["cacheable"]:
                cacheable = False
            for entry in collected["entries"]:
                merged[entry["candidate"]["name"]] = entry
        return {"entries": merged, "cacheable": cacheable}

    def _collect_layer(self, layer: _SkillLayer, options: dict) -> dict:
        collected = self._list_layer_candidates(layer, options)
        collected["entries"].sort(key=lambda e: (
            e["candidate"]["rank"], e["providerOrder"], e["localOrder"],
        ))
        seen: set[str] = set()
        result: list[dict] = []
        for entry in collected["entries"]:
            skill = entry["candidate"]
            if skill["name"] in seen:
                logger.warning(
                    'skill "%s" from %s ignored because a higher-priority skill already exists',
                    skill["name"], skill["source"],
                )
                continue
            seen.add(skill["name"])
            result.append(entry)
        return {"entries": result, "cacheable": collected["cacheable"]}

    def _list_layer_candidates(self, layer: _SkillLayer, options: dict) -> dict:
        candidates: list[dict] = []
        cacheable = True
        runtime_order = 0
        for name in sorted(layer.runtime, key=lambda s: s):
            skill = layer.runtime[name]
            candidates.append({
                "candidate": self._runtime_candidate(skill),
                "provider": _RUNTIME_SKILL_PROVIDER,
                "providerOrder": -1,
                "localOrder": runtime_order,
                "layer": layer,
            })
            runtime_order += 1
        for registered in layer.providers.values():
            provider = registered["provider"]
            local_order = 0
            try:
                output = provider["list"](options)
            except Exception as error:
                cacheable = False
                logger.warning('skill provider "%s" skipped: %s', provider["name"], error)
                continue
            if output is None:
                continue
            observation = self._normalize_provider_observation(output, provider["name"])
            if not observation["complete"]:
                cacheable = False
            for candidate in observation["candidates"]:
                validate_candidate(candidate, provider["name"])
                candidates.append({
                    "candidate": candidate,
                    "provider": provider,
                    "providerOrder": registered["order"],
                    "localOrder": local_order,
                    "layer": layer,
                })
                local_order += 1
        return {"entries": candidates, "cacheable": cacheable}

    def _runtime_candidate(self, skill: dict) -> dict:
        candidate: dict = {
            "name": skill["name"],
            "description": skill["description"],
            "invocation": skill["invocation"],
            "source": skill["source"],
            "provider": skill["provider"],
            "rank": RUNTIME_RANK,
            "locator": skill,
        }
        if "whenToUse" in skill:
            candidate["whenToUse"] = skill["whenToUse"]
        if "resourceBase" in skill:
            candidate["resourceBase"] = skill["resourceBase"]
        if "path" in skill:
            candidate["path"] = skill["path"]
        if "metadata" in skill:
            candidate["metadata"] = skill["metadata"]
        return candidate

    def _normalize_provider_observation(self, output: Any, provider_name: str) -> dict:
        if isinstance(output, (list, tuple)):
            return {"candidates": list(output), "complete": True}
        if not isinstance(output, (dict, MappingProxyType)):
            raise TypeError(
                f'skill provider "{provider_name}" list() must return an array or {{ candidates, complete }} observation'
            )
        candidates = output.get("candidates")
        complete = output.get("complete")
        if not isinstance(candidates, (list, tuple)) or not isinstance(complete, bool):
            raise TypeError(
                f'skill provider "{provider_name}" list() must return an array or {{ candidates, complete }} observation'
            )
        return {"candidates": list(candidates), "complete": complete}

    def _to_summary(self, skill: dict) -> dict:
        summary: dict = {
            "name": skill["name"],
            "description": skill["description"],
            "invocation": skill["invocation"],
            "source": skill["source"],
            "provider": skill["provider"],
        }
        if "whenToUse" in skill:
            summary["whenToUse"] = skill["whenToUse"]
        if "resourceBase" in skill:
            summary["resourceBase"] = skill["resourceBase"]
        return summary

    # ---------- 失效 ----------

    def invalidate_cache(self) -> None:
        """revision++ + 清缓存 + emit `skills/change`（监听器异常被容错）。"""
        self._revision += 1
        self._collect_cache.clear()
        try:
            self.ctx.emit("skills/change")
        except Exception as error:
            logger.warning("skills/change listener threw: %s", error)

    def _invalidate_entry(self, entry: dict) -> None:
        layer = entry["layer"]
        provider = layer.providers.get(entry["provider"]["name"])
        if provider is not None and provider["provider"] is entry["provider"]:
            self.invalidate_cache()


#: 运行时 skill 的载体 provider：只持有 get（list 永不产出候选）。
_RUNTIME_SKILL_PROVIDER: dict = {
    "name": RUNTIME_PROVIDER,
    "list": lambda options: [],
    "get": lambda candidate, options: candidate["locator"],
}
