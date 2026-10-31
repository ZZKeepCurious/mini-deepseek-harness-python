"""Listed catalog 解析（对齐 packages/llm/llm-deepseek/src/listed-catalog.ts）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .deepseek_account import (
    CatalogRefusal,
    _gate_of,
    _listed,
    _model_with,
    refuse,
)

__all__ = ["ListedCatalog", "refuse_listed_model", "resolve_listed_catalog"]


@dataclass(frozen=True)
class ListedCatalog:
    offered: tuple[Any, ...]
    resolution: tuple[Any, ...]
    refusals: dict[str, CatalogRefusal]
    badges: dict[str, dict]
    problems: tuple[str, ...]
    override_errors: tuple[str, ...]


def resolve_listed_catalog(
        *,
        listing,
        supported,
        overrides,
        route_max_tokens: int,
        client_version: str) -> ListedCatalog:
    supported_by_id = {model.id: model for model in supported}
    problems: list[str] = []
    override_errors: list[str] = []

    def with_override(model):
        override = overrides.get(model.id)
        if override is None:
            return model
        candidate = _model_with(model, override)
        try:
            from .deepseek_account import _validate_override
            _validate_override(model.id, override)
        except ValueError as error:
            override_errors.append(f"modelOverrides.{model.id} does not apply ({error})")
            return model
        return candidate

    if listing is None:
        resolution = tuple(with_override(model) for model in supported)
        return ListedCatalog(resolution, resolution, {}, {}, tuple(problems),
                             tuple(override_errors))

    offered: list[Any] = []
    refusals: dict[str, CatalogRefusal] = {}
    badges: dict[str, dict] = {}
    for entry in listing:
        refusal = _gate_of(entry, entry.id in supported_by_id, client_version, problems)
        if refusal is not None:
            refusals[entry.id] = refusal
            continue
        if not entry.capabilities.usable:
            problems.append(f'model "{entry.id}" {entry.capabilities.reason}')
            refusals[entry.id] = CatalogRefusal("unsupported")
            continue
        from .deepseek_files import DeepSeekCatalogModel
        base = supported_by_id.get(entry.id) or DeepSeekCatalogModel(id=entry.id)
        offered.append(with_override(_listed(base, entry, entry.capabilities.facts,
                                             route_max_tokens)))
        badge = entry.badge
        if isinstance(badge, dict):
            kind = badge.get("kind")
            if kind == "badge":
                badges[entry.id] = badge["badge"]
            elif kind == "invalid":
                problems.append(f'model "{entry.id}" shows no badge: {badge.get("reason")}')
    offered_ids = {model.id for model in offered}
    listed_ids = {entry.id for entry in listing}
    for mid in overrides:
        if mid not in listed_ids and mid not in supported_by_id:
            override_errors.append(
                f'modelOverrides names "{mid}", which neither the model listing nor the '
                f"built-in catalog describes")
    resolution = tuple(offered) + tuple(
        with_override(model) for model in supported if model.id not in offered_ids)
    return ListedCatalog(tuple(offered), resolution, refusals, badges,
                         tuple(problems), tuple(override_errors))


def refuse_listed_model(model: str, refusal: CatalogRefusal | None) -> None:
    refuse(model, refusal)
