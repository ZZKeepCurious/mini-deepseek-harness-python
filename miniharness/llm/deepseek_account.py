"""DeepSeek 账户路由的 `/models` 目录（listing + 覆盖 + 版本门控）。

对应 dsh 真实源码：packages/llm/llm-deepseek-account/src/（catalog.ts +
listing.ts + resolve-catalog.ts + config.ts + index.ts 的 refuse 段）。

上游账户路由不再接受可配 `models`：目录由 `GET /models`（经账户 token 认证）
供应，再按 `client_requirements[APP_IDENTITY.product].min_version` 做版本门控、
按 `modelOverrides.<id>` 做部署级叠加。缺凭证 → 空目录（`discoverModels` 返回
[]）。构建不支持的列出模型在 401/握手前即被拒绝，码为
`CLIENT_UPGRADE_REQUIRED` / `MODEL_UNSUPPORTED`。

mini 载体差异（登记 verified-diffs）：
  * 无原生 fetch/AbortController——listing 请求用 httpx，超时经 httpx.Timeout；
    上游 `ABORTED` 码在 mini 无对应常量，取消归入 TIMEOUT/TRANSPORT。
  * `APP_IDENTITY.product` 为 mini 自有（`mini-harness`），故
    `client_requirements` 按该键读取。
  * 目录缓存为 asyncio 载体（单飞 + 后台刷新 + 保留 last-known-good），
    对齐上游 AccountModelCatalog 的同步/异步拆分。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .protocol import APP_IDENTITY, INVALID_RESPONSE, TIMEOUT, TRANSPORT, LlmFailure

__all__ = [
    "AccountCatalog",
    "AccountModelCatalog",
    "AccountRoute",
    "CatalogRefusal",
    "DEFAULT_MODELS_MAX_AGE_MS",
    "DEFAULT_MODELS_TIMEOUT_MS",
    "LISTED_MODALITIES",
    "LISTING_TIMEOUT",
    "ListingEntry",
    "ListedCapabilities",
    "ListedFacts",
    "ClientRequirement",
    "ModelListing",
    "ModelOverride",
    "ResolvedAccountOptions",
    "TOKEN_HEADER",
    "CatalogSettings",
    "parse_model_listing",
    "refuse",
    "request_model_listing",
    "resolve_account_catalog",
    "resolve_account_options",
    "resolve_override",
]

#: 目录请求 deadline 的 timeout reason（上游 listing.ts:9）。
LISTING_TIMEOUT = "MODEL_LISTING_TIMEOUT"
#: 本构建发送的输入模态，按上游顺序（listing.ts:6）。
LISTED_MODALITIES = ("text", "image")
#: listing 请求与首次等待的缺省 deadline（config.ts:11）。
DEFAULT_MODELS_TIMEOUT_MS = 5_000
#: listing 缺省刷新年龄（config.ts:13）。
DEFAULT_MODELS_MAX_AGE_MS = 600_000
#: Messages / Files / 目录请求携带账户 token 的头（index.ts:24）。
TOKEN_HEADER = "x-dsh-auth-token"
#: 目录请求缺省无额外 header 时的 accept（listing.ts:158）。
_MAX_TIMER_DELAY_MS = 2_147_483_647


def _fields(value: Any) -> dict | None:
    return value if isinstance(value, dict) else None


def _known_modalities(value: Any) -> list[str] | None:
    """本构建发送的列出模态，按 listing 顺序去重；非数组 → None（listing.ts:61）。"""
    if not isinstance(value, list):
        return None
    listed = value
    return [m for m in LISTED_MODALITIES if m in listed]


def _capacity(value: Any) -> "int | None | bool":
    """声明的容量：缺失 → None，非正安全整数 → False（listing.ts:69）。"""
    if value is None:
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else False


@dataclass(frozen=True)
class ClientRequirement:
    """一条 listing 条目在本产品 token 下声明的要求（listing.ts:12）。"""

    kind: str  # 'none' | 'min-version' | 'invalid'
    version: str | None = None


@dataclass(frozen=True)
class ListedFacts:
    """一条可用 listing 条目声明的能力（listing.ts:18）。"""

    input_modalities: tuple[str, ...]
    context_window: int | None = None
    max_output_tokens: int | None = None
    system_prompt_update: str | None = None  # 'in-history' | 'leading-only'


@dataclass(frozen=True)
class ListedCapabilities:
    """声明的能力，或 Messages 传输无法驱动该模型的原因（listing.ts:30）。"""

    usable: bool
    facts: ListedFacts | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ListingEntry:
    """一条解码后的 listing 条目（listing.ts:35）。"""

    id: str
    requirement: ClientRequirement
    capabilities: ListedCapabilities
    name: str | None = None


@dataclass(frozen=True)
class ModelListing:
    """至少有一条目的解码 listing（listing.ts:47）。"""

    entries: tuple[ListingEntry, ...]
    skipped: int


def _requirement_of(entry: dict) -> ClientRequirement:
    if "client_requirements" not in entry or entry.get("client_requirements") is None:
        return ClientRequirement("none")
    requirements = _fields(entry.get("client_requirements"))
    if requirements is None:
        return ClientRequirement("invalid")
    declared = requirements.get(APP_IDENTITY.product)
    if declared is None:
        return ClientRequirement("none")
    own = _fields(declared)
    if own is None:
        return ClientRequirement("invalid")
    if own.get("min_version") is None:
        return ClientRequirement("none")
    value = own.get("min_version")
    if isinstance(value, str) and len(value) > 0:
        return ClientRequirement("min-version", value)
    return ClientRequirement("invalid")


def _capabilities_of(entry: dict) -> ListedCapabilities:
    api = _fields(entry.get("api_capabilities"))
    messages = _fields(api.get("anthropic_messages")) if api is not None else None
    if messages is None:
        return ListedCapabilities(False, reason="declares no api_capabilities.anthropic_messages")
    outputs = (["text"] if "output_modalities" not in entry or entry.get("output_modalities") is None
               else _known_modalities(entry.get("output_modalities")))
    if outputs is None or "text" not in outputs:
        return ListedCapabilities(False, reason="declares no text output")
    inputs = (["text"] if "input_modalities" not in entry or entry.get("input_modalities") is None
              else _known_modalities(entry.get("input_modalities")))
    if inputs is None or len(inputs) == 0:
        return ListedCapabilities(False, reason="declares no input modality DeepSeek Harness supports")
    context_window = _capacity(entry.get("context_window"))
    max_output_tokens = _capacity(entry.get("max_output_tokens"))
    if context_window is False or max_output_tokens is False:
        return ListedCapabilities(False, reason="declares an invalid context_window or max_output_tokens")
    update = messages.get("system_prompt_update")
    facts = ListedFacts(
        input_modalities=tuple(inputs),
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        system_prompt_update=(None if update is None
                              else ("in-history" if update == "in-history" else "leading-only")),
    )
    return ListedCapabilities(True, facts=facts)


def parse_model_listing(body: Any) -> ModelListing:
    """逐条解码一个 `/models` 响应体（listing.ts:118）。

    无 `data` 数组或无可用条目抛 `LlmFailure(INVALID_RESPONSE)`。
    """
    data = _fields(body).get("data") if _fields(body) is not None else None
    if not isinstance(data, list):
        raise LlmFailure(INVALID_RESPONSE, 'DeepSeek model listing has no "data" array')
    entries: list[ListingEntry] = []
    seen: set[str] = set()
    for row in data:
        entry = _fields(row)
        mid = entry.get("id") if entry is not None else None
        if entry is None or not isinstance(mid, str) or len(mid) == 0 or mid in seen:
            continue
        seen.add(mid)
        name = entry.get("name")
        entries.append(ListingEntry(
            id=mid,
            name=name if isinstance(name, str) and len(name) > 0 else None,
            requirement=_requirement_of(entry),
            capabilities=_capabilities_of(entry),
        ))
    if len(entries) == 0:
        raise LlmFailure(INVALID_RESPONSE, "DeepSeek model listing contains no model entry")
    return ModelListing(tuple(entries), len(data) - len(entries))


def request_model_listing(url: str, headers: dict | None = None, *,
                          timeout_ms: int = DEFAULT_MODELS_TIMEOUT_MS,
                          transport: Any = None,
                          attribution: Callable[[], dict] | None = None) -> ModelListing:
    """请求并解码一个模型 listing（listing.ts:150）。

    重定向失败（凭据停留在请求 origin）。错误映射：超时 → TIMEOUT、网络 →
    TRANSPORT、非 2xx → `HTTP_<status>`、非 JSON/缺条目 → INVALID_RESPONSE。
    """
    from .protocol import attribution_headers
    merge = attribution_headers if attribution is None else attribution
    request_headers = {**merge(), "accept": "application/json", **(headers or {})}
    kwargs: dict[str, Any] = {"timeout": httpx.Timeout(timeout_ms / 1000.0),
                              "follow_redirects": False}
    if transport is not None:
        kwargs["transport"] = transport
    try:
        with httpx.Client(**kwargs) as client:
            response = client.get(url, headers=request_headers)
            text = response.text
    except httpx.TimeoutException as error:
        raise LlmFailure(
            TIMEOUT,
            f"DeepSeek model listing request to {url} timed out after {timeout_ms} ms") from error
    except httpx.HTTPError as error:
        raise LlmFailure(TRANSPORT, f"could not reach {url}") from error
    if 300 <= response.status_code < 400:
        # 上游 fetch redirect:'error' 使重定向被拒（凭据不离开请求 origin）→ TRANSPORT。
        raise LlmFailure(TRANSPORT, f"could not reach {url}")
    if response.status_code >= 400:
        raise LlmFailure(f"HTTP_{response.status_code}",
                         f"{url} answered HTTP {response.status_code}",
                         status=response.status_code)
    try:
        import json
        body = json.loads(text)
    except ValueError as error:
        raise LlmFailure(INVALID_RESPONSE, f"{url} did not answer with JSON") from error
    return parse_model_listing(body)


# ---------- 配置与覆盖（config.ts） ----------

#: 一条模型覆盖；未设字段与空列表继承底层条目（config.ts:16）。
ModelOverride = dict


@dataclass(frozen=True)
class CatalogSettings:
    """一代配置的 listing 端点与缓存时序（config.ts:54）。"""

    url: str
    timeout_ms: int
    max_age_ms: int


@dataclass(frozen=True)
class ResolvedAccountOptions:
    """账户路由的一代已验证配置（config.ts:64）。"""

    protocol: dict
    catalog: CatalogSettings
    model_overrides: dict


def _listing_endpoint(base_url: str) -> str:
    """listing 端点：Messages origin 上的 `/models`，唯一接收账户 token 的 origin。"""
    parsed = httpx.URL(base_url)
    return str(parsed.copy_with(path="/models", query=None))


def resolve_override(mid: str, override: dict) -> ModelOverride:
    """保留覆盖声明的字段；未设字段为 None、空数组视为继承（config.ts:88）。"""
    if len(mid) == 0:
        raise ValueError("llm-deepseek-account: modelOverrides keys must be non-empty model ids")
    if "id" in override:
        raise ValueError(
            f'llm-deepseek-account: modelOverrides.{mid} sets "id", which is its key')
    declared: dict = {}
    for key, value in override.items():
        if value is None or (isinstance(value, list) and len(value) == 0):
            continue
        declared[key] = value
    _validate_override(mid, declared)
    return declared


def _validate_override(mid: str, declared: dict) -> None:
    """逐字段校验覆盖（config.ts:96）：非法值 fail loud。"""
    allowed = {"name", "description", "contextWindow", "maxTokens", "inputModalities",
               "imagePixelBudget", "imageMaxBytes", "systemPromptUpdate", "toolUpdate"}
    unknown = set(declared) - allowed
    if unknown:
        raise ValueError(
            f"llm-deepseek-account: modelOverrides.{mid} is invalid: unknown field(s) "
            f"{', '.join(sorted(unknown))}")
    if "inputModalities" in declared:
        modalities = declared["inputModalities"]
        if (not isinstance(modalities, list)
                or any(m not in LISTED_MODALITIES for m in modalities)):
            raise ValueError(
                f"llm-deepseek-account: modelOverrides.{mid} is invalid: inputModalities "
                f"must only contain {LISTED_MODALITIES}")
    for numeric in ("contextWindow", "maxTokens", "imageMaxBytes"):
        if numeric in declared:
            value = declared[numeric]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(
                    f"llm-deepseek-account: modelOverrides.{mid} is invalid: {numeric} "
                    f"must be a positive integer")
    if "systemPromptUpdate" in declared and declared["systemPromptUpdate"] not in ("in-history", "leading-only"):
        raise ValueError(
            f"llm-deepseek-account: modelOverrides.{mid} is invalid: systemPromptUpdate "
            f"must be 'in-history' or 'leading-only'")
    if "toolUpdate" in declared and declared["toolUpdate"] not in ("in-history", "addition-only"):
        raise ValueError(
            f"llm-deepseek-account: modelOverrides.{mid} is invalid: toolUpdate must be "
            f"'in-history' or 'addition-only'")


def resolve_account_options(options: dict,
                            environment: dict | None = None) -> ResolvedAccountOptions:
    """从原始账户设置到已验证事实的唯一显式 resolve 步（config.ts:113）。

    `models` 不再可配 → fail loud；`modelsTimeoutMs`/`modelsMaxAgeMs` 逐项判定；
    `modelOverrides` 逐条校验。`environment` 为 mini 无 oper 载体下的可选注入。
    """
    if "models" in options:
        raise ValueError(
            "llm-deepseek-account: models is no longer configurable; the account /models "
            "listing supplies the catalog. Remove models from this entry and adjust single "
            "models with modelOverrides.<model id>")
    opts = dict(options)
    models_timeout_ms = opts.pop("modelsTimeoutMs", DEFAULT_MODELS_TIMEOUT_MS)
    models_max_age_ms = opts.pop("modelsMaxAgeMs", DEFAULT_MODELS_MAX_AGE_MS)
    model_overrides = opts.pop("modelOverrides", {}) or {}
    if (not isinstance(models_timeout_ms, (int, float)) or isinstance(models_timeout_ms, bool)
            or not (models_timeout_ms == models_timeout_ms)  # NaN
            or models_timeout_ms <= 0 or models_timeout_ms > _MAX_TIMER_DELAY_MS):
        raise ValueError(
            f"llm-deepseek-account: modelsTimeoutMs must be a positive finite number no "
            f"greater than {_MAX_TIMER_DELAY_MS}")
    if (not isinstance(models_max_age_ms, int) or isinstance(models_max_age_ms, bool)
            or models_max_age_ms < 0):
        raise ValueError(
            "llm-deepseek-account: modelsMaxAgeMs must be a non-negative safe integer")
    base_url = opts.get("base_url") or opts.get("baseURL") or "https://api.deepseek.com/anthropic"
    protocol = dict(opts)
    overrides = {mid: resolve_override(mid, override)
                 for mid, override in model_overrides.items()}
    return ResolvedAccountOptions(
        protocol=protocol,
        catalog=CatalogSettings(url=_listing_endpoint(base_url),
                                timeout_ms=models_timeout_ms,
                                max_age_ms=models_max_age_ms),
        model_overrides=overrides,
    )


# ---------- 目录解析（resolve-catalog.ts） ----------

@dataclass(frozen=True)
class CatalogRefusal:
    """本构建在一次网络 I/O 之前拒绝某列出模型的原因（resolve-catalog.ts:10）。"""

    kind: str  # 'upgrade-required' | 'unsupported'
    required_version: str | None = None


@dataclass(frozen=True)
class AccountCatalog:
    """一份解析后的账户目录（resolve-catalog.ts:29）。"""

    offered: tuple[Any, ...]
    resolution: tuple[Any, ...]
    refusals: dict
    problems: tuple[str, ...]
    override_errors: tuple[str, ...]


def _model_with(model, layer: dict):
    """按覆盖层替换条目字段（resolve-catalog.ts:43，dataclass 载体）。"""
    from dataclasses import replace
    merged = replace(model, **{k: v for k, v in layer.items()})
    if getattr(merged, "inputModalities", None) is None or "image" not in merged.inputModalities:
        if "imagePixelBudget" not in layer:
            merged = replace(merged, imagePixelBudget=None)
        if "imageMaxBytes" not in layer:
            merged = replace(merged, imageMaxBytes=None)
    return merged


def _listed(base, entry: ListingEntry, facts: ListedFacts, route_max_tokens: int):
    """把一条可用 listing 条目叠在本构建同 id 条目之上（resolve-catalog.ts:53）。"""
    from dataclasses import replace
    base = base if facts.system_prompt_update != "leading-only" \
        else replace(base, systemPromptUpdate=None)
    layer: dict = {"inputModalities": tuple(facts.input_modalities)}
    if entry.name is not None:
        layer["name"] = entry.name
    if facts.context_window is not None:
        layer["contextWindow"] = facts.context_window
    if facts.max_output_tokens is not None:
        layer["maxTokens"] = min(route_max_tokens, facts.max_output_tokens)
    if facts.system_prompt_update == "in-history":
        layer["systemPromptUpdate"] = "in-history"
    return _model_with(base, layer)


def _semver_gte(client_version: str, required: str) -> bool | None:
    """客户端版本是否 ≥ 要求；版本串不可解析 → None（resolve-catalog.ts:70）。"""
    def parse(value: str):
        core = value.split("+", 1)[0].split("-", 1)[0]
        parts = core.split(".")
        result = []
        for part in parts:
            if not part.isdigit():
                return None
            result.append(int(part))
        while len(result) < 3:
            result.append(0)
        return tuple(result[:3])

    left = parse(client_version)
    right = parse(required)
    if right is None:
        return None
    if left is None:
        return None
    return left >= right


def resolve_account_catalog(*, listing, supported, overrides, route_max_tokens,
                            client_version: str) -> AccountCatalog:
    """解析账户目录（resolve-catalog.ts:91）。

    声明的最小客户端版本决定列出模型的可用性；无声明时只有本构建支持的模型被
    提供，其余列出模型隐藏。
    """
    supported_by_id = {model.id: model for model in supported}
    problems: list[str] = []
    override_errors: list[str] = []

    def with_override(model):
        override = overrides.get(model.id)
        if override is None:
            return model
        candidate = _model_with(model, override)
        try:
            _validate_override(model.id, override)
        except ValueError as error:
            override_errors.append(f"modelOverrides.{model.id} does not apply ({error})")
            return model
        return candidate

    if listing is None:
        resolution = tuple(with_override(model) for model in supported)
        return AccountCatalog(resolution, resolution, {}, tuple(problems),
                              tuple(override_errors))

    offered = []
    refusals: dict = {}
    for entry in listing:
        refusal = _gate_of(entry, entry.id in supported_by_id, client_version, problems)
        if refusal is not None:
            refusals[entry.id] = refusal
            continue
        if not entry.capabilities.usable:
            problems.append(f'model "{entry.id}" {entry.capabilities.reason}')
            refusals[entry.id] = CatalogRefusal("unsupported")
            continue
        base = supported_by_id.get(entry.id)
        if base is None:
            from .deepseek_files import DeepSeekCatalogModel
            base = DeepSeekCatalogModel(id=entry.id)
        offered.append(with_override(_listed(base, entry, entry.capabilities.facts,
                                             route_max_tokens)))
    offered_ids = {model.id for model in offered}
    listed_ids = {entry.id for entry in listing}
    for mid in overrides:
        if mid not in listed_ids and mid not in supported_by_id:
            override_errors.append(
                f'modelOverrides names "{mid}", which neither the account listing nor the '
                f'built-in catalog describes')
    resolution = tuple(offered) + tuple(
        with_override(model) for model in supported if model.id not in offered_ids)
    return AccountCatalog(tuple(offered)[:len(offered)], resolution, refusals,
                          tuple(problems), tuple(override_errors))


def _gate_of(entry: ListingEntry, known: bool, client_version: str,
             problems: list[str]) -> "CatalogRefusal | None":
    """列出模型的版本门控（resolve-catalog.ts:64）。"""
    requirement = entry.requirement
    if requirement.kind == "none":
        return None if known else CatalogRefusal("unsupported")
    if requirement.kind == "min-version":
        verdict = _semver_gte(client_version, requirement.version)
        if verdict is None:
            problems.append(f'model "{entry.id}" declares an unreadable client requirement')
            return CatalogRefusal("unsupported")
        return None if verdict else CatalogRefusal("upgrade-required", requirement.version)
    problems.append(f'model "{entry.id}" declares an unreadable client requirement')
    return CatalogRefusal("unsupported")


def refuse(model: str, refusal: "CatalogRefusal | None") -> None:
    """在 401/握手前拒绝一个模型（index.ts:33）。"""
    if refusal is None:
        return
    from .protocol import CLIENT_UPGRADE_REQUIRED, MODEL_UNSUPPORTED
    if refusal.kind == "upgrade-required":
        raise LlmFailure(
            CLIENT_UPGRADE_REQUIRED,
            f'DeepSeek model "{model}" requires DeepSeek Harness '
            f"{refusal.required_version} or later; current version is {APP_IDENTITY.version}")
    if refusal.kind == "unsupported":
        raise LlmFailure(
            MODEL_UNSUPPORTED,
            f'DeepSeek model "{model}" is not supported by DeepSeek Harness; current '
            f"version is {APP_IDENTITY.version}")
    raise ValueError(f"unknown catalog refusal {refusal.kind!r}")


# ---------- 目录缓存（catalog.ts） ----------

@dataclass
class _CacheState:
    scope: str | None = None
    listing: ModelListing | None = None
    settled_at: float | None = None
    failing: bool = False


class AccountModelCatalog:
    """账户路由的 listing 缓存：单飞加载 + 后台刷新 + 保留 last-known-good（catalog.ts:46）。"""

    def __init__(self, *, settings: Callable[[], CatalogSettings],
                 token: Callable[[str], Any], load: Callable[..., ModelListing],
                 now: Callable[[], float] = time.monotonic,
                 info: Callable[[str], None] | None = None,
                 warn: Callable[[str], None] | None = None):
        self._settings = settings
        self._token = token
        self._load = load
        self._now = now
        self._info = info or (lambda _message: None)
        self._warn = warn or (lambda _message: None)
        self._state = _CacheState()
        self._inflight: asyncio.Future | None = None
        self._closed = False

    def current(self) -> dict:
        return {"signed_in": self._state.scope is not None, "listing": self._state.listing}

    async def view(self) -> dict:
        import hashlib
        settings = self._settings()
        token = await _maybe_await(self._token(settings.url))
        if self._closed:
            return self.current()
        scope = (None if token is None
                 else f"{settings.url}\n{hashlib.sha256(token.encode('utf-8')).hexdigest()}")
        if scope != self._state.scope:
            self._rescope(scope)
        if token is not None:
            if self._state.settled_at is None:
                await self._start(settings, token)
            elif self._now() - self._state.settled_at >= settings.max_age_ms / 1000.0:
                asyncio.ensure_future(self._start(settings, token))
        return self.current()

    def dispose(self) -> None:
        self._closed = True

    def _rescope(self, scope: "str | None") -> None:
        self._state = _CacheState(scope=scope)
        self._inflight = None

    async def _start(self, settings: CatalogSettings, token: str):
        if self._inflight is not None and not self._inflight.done():
            return await self._inflight
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._inflight = future
        try:
            listing = await _maybe_await(self._load(settings.url, token, settings.timeout_ms))
        except Exception as error:  # noqa: BLE001 - 对齐上游：从不抛出、只告警
            if not self._state.failing:
                fallback = ("using the bundled catalog" if self._state.listing is None
                            else "keeping the previous catalog")
                self._warn(f"model listing from {settings.url} failed ({error}); {fallback}")
            self._state.settled_at = self._now()
            self._state.failing = True
            return None
        finally:
            if not future.done():
                future.set_result(None)
            if self._inflight is future:
                self._inflight = None
        self._state.listing = listing
        self._state.settled_at = self._now()
        self._state.failing = False
        self._info(f"model listing from {settings.url} loaded {len(listing.entries)} "
                   f"model(s), skipped {listing.skipped} row(s)")
        return None


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


class AccountRoute:
    """账户路由的连接事实提供者（上游 index.ts apply 的 mini 载体）。

    持有一份 `AccountModelCatalog`、本构建支持模型、`modelOverrides` 与
    `routeMaxTokens`；`connection()` 在每次请求解析时按当前 listing 重算目录，
    `admit(model)` 在网络 I/O 之前按版本门控拒绝，`discover_models()` 未登录
    时返回空目录（对齐上游 `discoverModels`）。
    """

    def __init__(self, *, catalog: AccountModelCatalog, supported, overrides=None,
                 route_max_tokens: int, client_version: str | None = None):
        self._catalog = catalog
        self._supported = tuple(supported)
        self._overrides = overrides or {}
        self._route_max_tokens = route_max_tokens
        self._client_version = client_version or APP_IDENTITY.version

    def resolve(self) -> AccountCatalog:
        view = self._catalog.current()
        listing = view["listing"]
        result = resolve_account_catalog(
            listing=(None if listing is None else listing.entries),
            supported=self._supported, overrides=self._overrides,
            route_max_tokens=self._route_max_tokens,
            client_version=self._client_version)
        return result

    def connection(self) -> dict:
        """一次连接的有效事实：已解析目录替换 `models`（index.ts connectionOf）。"""
        result = self.resolve()
        return {"models": result.resolution, "catalog": result}

    def admit(self, model: str) -> None:
        refuse(model, self.resolve().refusals.get(model))

    def discover_models(self) -> list:
        """未登录 → 空目录；否则给出可提供的模型（index.ts discoverModels）。"""
        from .deepseek_files import catalog_model_info
        if not self._catalog.current()["signed_in"]:
            return []
        return [catalog_model_info("deepseek-account", model)
                for model in self.resolve().offered]

