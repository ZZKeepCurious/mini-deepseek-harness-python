"""DeepSeek 账户 `/models` 目录（dsh-v0.2.1-rc.2 增量 P0-6a）。

对齐上游 `packages/llm/llm-deepseek-account/src/{listing,config,resolve-catalog,
catalog,index}.ts`：listing 解码、`modelOverrides` 的显式 resolve、版本门控
（`CLIENT_UPGRADE_REQUIRED`/`MODEL_UNSUPPORTED`）、单飞 + 后台刷新 +
last-known-good 缓存。
"""
import asyncio
import unittest

import httpx

from miniharness.llm.deepseek_account import (
    AccountModelCatalog,
    LISTING_TIMEOUT,
    CatalogSettings,
    ModelListing,
    parse_model_listing,
    refuse,
    request_model_listing,
    resolve_account_catalog,
    resolve_account_options,
    resolve_override,
)
from miniharness.llm.deepseek_files import DeepSeekCatalogModel
from miniharness.llm.protocol import (
    CLIENT_UPGRADE_REQUIRED,
    INVALID_RESPONSE,
    MODEL_UNSUPPORTED,
    TIMEOUT,
    TRANSPORT,
    LlmFailure,
)


def _entry(mid, **fields):
    entry = {"id": mid}
    entry.update(fields)
    return entry


def _usable(mid, **fields):
    row = _entry(mid, api_capabilities={"anthropic_messages": {}})
    row.update(fields)
    return row


class ParseListingTest(unittest.TestCase):
    def test_reads_entries_in_order_and_counts_skipped(self):
        listing = parse_model_listing({"data": [
            _usable("a"), {"name": "no id"}, _usable("a"), _usable("b"), "junk",
        ]})
        self.assertEqual([entry.id for entry in listing.entries], ["a", "b"])
        self.assertEqual(listing.skipped, 3)

    def test_missing_data_array_is_invalid(self):
        with self.assertRaises(LlmFailure) as caught:
            parse_model_listing({})
        self.assertEqual(caught.exception.code, INVALID_RESPONSE)

    def test_no_usable_entry_is_invalid(self):
        with self.assertRaises(LlmFailure) as caught:
            parse_model_listing({"data": [{"name": "x"}]})
        self.assertEqual(caught.exception.code, INVALID_RESPONSE)

    def test_requirement_variants(self):
        none = parse_model_listing({"data": [_usable("a")]}).entries[0]
        self.assertEqual(none.requirement.kind, "none")
        missing = parse_model_listing({"data": [_usable("a", client_requirements={})]}).entries[0]
        self.assertEqual(missing.requirement.kind, "none")
        declared = parse_model_listing({"data": [_usable(
            "a", client_requirements={"mini-harness": {"min_version": "1.2.3"}})]}).entries[0]
        self.assertEqual((declared.requirement.kind, declared.requirement.version),
                         ("min-version", "1.2.3"))
        invalid = parse_model_listing({"data": [_usable("a", client_requirements="x")]}).entries[0]
        self.assertEqual(invalid.requirement.kind, "invalid")

    def test_capabilities_require_messages_block_and_text(self):
        no_messages = parse_model_listing({"data": [_entry("a")]}).entries[0]
        self.assertFalse(no_messages.capabilities.usable)
        self.assertIn("anthropic_messages", no_messages.capabilities.reason)
        no_text = parse_model_listing({"data": [
            _usable("a", output_modalities=["image"])]}).entries[0]
        self.assertFalse(no_text.capabilities.usable)

    def test_capabilities_reads_gaps_and_clamps_modalities(self):
        entry = parse_model_listing({"data": [_usable(
            "a", input_modalities=["audio", "image", "text"],
            context_window=1000, max_output_tokens=500,
            api_capabilities={"anthropic_messages": {"system_prompt_update": "other"}},
        )]}).entries[0]
        self.assertTrue(entry.capabilities.usable)
        facts = entry.capabilities.facts
        self.assertEqual(facts.input_modalities, ("text", "image"))
        self.assertEqual(facts.context_window, 1000)
        self.assertEqual(facts.max_output_tokens, 500)
        # 任何非 'in-history' 的值读作 'leading-only'。
        self.assertEqual(facts.system_prompt_update, "leading-only")

    def test_capabilities_rejects_invalid_capacity(self):
        entry = parse_model_listing({"data": [_usable("a", context_window=0)]}).entries[0]
        self.assertFalse(entry.capabilities.usable)


class RequestListingTest(unittest.TestCase):
    def _fetch(self, handler):
        return request_model_listing(
            "https://api.deepseek.com/models", {"x-dsh-auth-token": "t"},
            timeout_ms=1000, transport=httpx.MockTransport(handler),
            attribution=lambda: {})

    def test_success(self):
        listing = self._fetch(lambda request: httpx.Response(
            200, json={"data": [_usable("a")]}))
        self.assertEqual([entry.id for entry in listing.entries], ["a"])

    def test_non_2xx_maps_to_http_code(self):
        with self.assertRaises(LlmFailure) as caught:
            self._fetch(lambda request: httpx.Response(503, text="down"))
        self.assertEqual(caught.exception.code, "HTTP_503")
        self.assertEqual(caught.exception.status, 503)

    def test_redirect_is_rejected_as_transport(self):
        with self.assertRaises(LlmFailure) as caught:
            self._fetch(lambda request: httpx.Response(302, headers={"location": "https://evil"}))
        self.assertEqual(caught.exception.code, TRANSPORT)

    def test_non_json_is_invalid_response(self):
        with self.assertRaises(LlmFailure) as caught:
            self._fetch(lambda request: httpx.Response(200, text="<html>"))
        self.assertEqual(caught.exception.code, INVALID_RESPONSE)

    def test_timeout_maps_to_timeout(self):
        def handler(request):
            raise httpx.ConnectTimeout("slow")
        with self.assertRaises(LlmFailure) as caught:
            self._fetch(handler)
        self.assertEqual(caught.exception.code, TIMEOUT)

    def test_network_error_maps_to_transport(self):
        def handler(request):
            raise httpx.ConnectError("no route")
        with self.assertRaises(LlmFailure) as caught:
            self._fetch(handler)
        self.assertEqual(caught.exception.code, TRANSPORT)

    def test_listing_timeout_reason_constant(self):
        self.assertEqual(LISTING_TIMEOUT, "MODEL_LISTING_TIMEOUT")


class ResolveOptionsTest(unittest.TestCase):
    def test_models_is_no_longer_configurable(self):
        with self.assertRaises(ValueError) as caught:
            resolve_account_options({"models": []})
        self.assertIn("no longer configurable", str(caught.exception))

    def test_timeouts_and_endpoint(self):
        resolved = resolve_account_options(
            {"base_url": "https://api.deepseek.com/anthropic",
             "modelsTimeoutMs": 2000, "modelsMaxAgeMs": 1000})
        self.assertEqual(resolved.catalog.url, "https://api.deepseek.com/models")
        self.assertEqual(resolved.catalog.timeout_ms, 2000)
        self.assertEqual(resolved.catalog.max_age_ms, 1000)

    def test_invalid_timeouts_fail_loud(self):
        with self.assertRaises(ValueError):
            resolve_account_options({"modelsTimeoutMs": 0})
        with self.assertRaises(ValueError):
            resolve_account_options({"modelsMaxAgeMs": -1})

    def test_override_drops_unset_and_empty(self):
        self.assertEqual(resolve_override("a", {"name": None, "inputModalities": []}), {})
        self.assertEqual(resolve_override("a", {"contextWindow": 4096}),
                         {"contextWindow": 4096})

    def test_override_rejects_id_key_and_unknown_fields(self):
        with self.assertRaises(ValueError):
            resolve_override("a", {"id": "b"})
        with self.assertRaises(ValueError):
            resolve_override("a", {"nope": 1})


class ResolveCatalogTest(unittest.TestCase):
    _SUPPORTED = (
        DeepSeekCatalogModel(id="deepseek-flash", name="Flash", contextWindow=100),
        DeepSeekCatalogModel(id="deepseek-v4-pro", name="Pro"),
    )

    def _resolve(self, data, **kwargs):
        listing = parse_model_listing({"data": data}).entries if data is not None else None
        return resolve_account_catalog(
            listing=listing, supported=self._SUPPORTED,
            overrides=kwargs.get("overrides", {}),
            route_max_tokens=kwargs.get("route_max_tokens", 8192),
            client_version=kwargs.get("client_version", "0.2.1"))

    def test_no_listing_offers_bundled(self):
        catalog = self._resolve(None)
        self.assertEqual([m.id for m in catalog.offered],
                         ["deepseek-flash", "deepseek-v4-pro"])

    def test_unknown_without_requirement_is_refused_unsupported(self):
        catalog = self._resolve([_usable("mystery")])
        self.assertEqual(catalog.refusals["mystery"].kind, "unsupported")
        self.assertEqual([m.id for m in catalog.offered], [])

    def test_min_version_gate(self):
        required = _usable("mystery", client_requirements={"mini-harness": {"min_version": "9.9.9"}})
        catalog = self._resolve([required])
        refusal = catalog.refusals["mystery"]
        self.assertEqual((refusal.kind, refusal.required_version), ("upgrade-required", "9.9.9"))

    def test_known_model_layers_listed_facts(self):
        catalog = self._resolve([_usable(
            "deepseek-flash", name="Flash 2", context_window=200000,
            max_output_tokens=100000)])
        offered = catalog.offered[0]
        self.assertEqual(offered.name, "Flash 2")
        self.assertEqual(offered.contextWindow, 200000)
        self.assertEqual(offered.maxTokens, 8192)  # 受 routeMaxTokens 约束

    def test_unusable_capabilities_become_problem_and_refusal(self):
        catalog = self._resolve([_entry("deepseek-flash")])
        self.assertTrue(any("declares no api_capabilities" in p for p in catalog.problems))
        self.assertEqual(catalog.refusals["deepseek-flash"].kind, "unsupported")

    def test_override_error_for_unknown_id(self):
        catalog = self._resolve([_usable("deepseek-flash")],
                                overrides={"ghost": {"name": "G"}})
        self.assertTrue(any("neither the account listing" in e for e in catalog.override_errors))

    def test_override_applies_to_bundled_when_no_listing(self):
        catalog = self._resolve(None, overrides={"deepseek-flash": {"name": "Custom"}})
        offered = next(m for m in catalog.offered if m.id == "deepseek-flash")
        self.assertEqual(offered.name, "Custom")

    def test_resolution_appends_supported_not_offered(self):
        catalog = self._resolve([_usable("deepseek-flash")])
        self.assertEqual([m.id for m in catalog.offered], ["deepseek-flash"])
        self.assertEqual([m.id for m in catalog.resolution],
                         ["deepseek-flash", "deepseek-v4-pro"])


class RefuseTest(unittest.TestCase):
    def test_upgrade_required(self):
        from miniharness.llm.deepseek_account import CatalogRefusal
        with self.assertRaises(LlmFailure) as caught:
            refuse("m", CatalogRefusal("upgrade-required", "9.9.9"))
        self.assertEqual(caught.exception.code, CLIENT_UPGRADE_REQUIRED)

    def test_unsupported(self):
        from miniharness.llm.deepseek_account import CatalogRefusal
        with self.assertRaises(LlmFailure) as caught:
            refuse("m", CatalogRefusal("unsupported"))
        self.assertEqual(caught.exception.code, MODEL_UNSUPPORTED)

    def test_none_is_noop(self):
        refuse("m", None)


class CatalogCacheTest(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_first_access_loads_then_caches_within_max_age(self):
        calls = []
        settings = CatalogSettings("https://x/models", 1000, 600)
        listing = ModelListing((), 0)

        def load(url, token, timeout_ms):
            calls.append(token)
            return listing

        catalog = AccountModelCatalog(
            settings=lambda: settings, token=lambda _url: "t", load=load,
            now=lambda: 0.0)
        view = self._run(catalog.view())
        self.assertIs(view["listing"], listing)
        self.assertTrue(view["signed_in"])
        self._run(catalog.view())
        self.assertEqual(calls, ["t"])

    def test_stale_age_triggers_background_reload(self):
        calls = []
        clock = {"now": 0.0}
        settings = CatalogSettings("https://x/models", 1000, 600)
        listing = ModelListing((), 0)

        def load(url, token, timeout_ms):
            calls.append(token)
            return listing

        catalog = AccountModelCatalog(
            settings=lambda: settings, token=lambda _url: "t", load=load,
            now=lambda: clock["now"])

        async def scenario():
            await catalog.view()
            clock["now"] = 601.0
            await catalog.view()
            for _ in range(5):  # 让后台刷新任务起步并完成
                await asyncio.sleep(0)

        self._run(scenario())
        self.assertEqual(calls, ["t", "t"])

    def test_failure_keeps_last_known_good_and_warns_once(self):
        warnings = []
        settings = CatalogSettings("https://x/models", 1000, 600)
        good = ModelListing((), 0)
        state = {"fail": False, "now": 0.0}

        def load(url, token, timeout_ms):
            if state["fail"]:
                raise RuntimeError("boom")
            return good

        catalog = AccountModelCatalog(
            settings=lambda: settings, token=lambda _url: "t", load=load,
            now=lambda: state["now"], warn=warnings.append)

        async def scenario():
            await catalog.view()
            state["fail"] = True
            state["now"] = 601.0
            await catalog.view()
            for _ in range(5):
                await asyncio.sleep(0)
            state["now"] = 1202.0
            await catalog.view()
            for _ in range(5):
                await asyncio.sleep(0)

        self._run(scenario())
        self.assertIsNotNone(catalog.current()["listing"])
        self.assertEqual(len(warnings), 1)

    def test_signed_out_has_no_listing(self):
        settings = CatalogSettings("https://x/models", 1000, 600)
        catalog = AccountModelCatalog(
            settings=lambda: settings, token=lambda _url: None,
            load=lambda *a: ModelListing((), 0))
        view = self._run(catalog.view())
        self.assertFalse(view["signed_in"])
        self.assertIsNone(view["listing"])


class AccountRouteTest(unittest.TestCase):
    def _catalog(self, listing):
        class _Cat:
            def current(_self):
                return {"signed_in": listing is not None, "listing": listing}
        return _Cat()

    def _route(self, listing, *, overrides=None, route_max_tokens=8192):
        from miniharness.llm.deepseek_account import AccountRoute
        return AccountRoute(
            catalog=self._catalog(listing),
            supported=(DeepSeekCatalogModel(id="deepseek-flash", name="Flash"),),
            overrides=overrides or {}, route_max_tokens=route_max_tokens,
            client_version="0.2.1")

    def test_connection_replaces_models_with_resolution(self):
        listing = parse_model_listing({"data": [_usable("deepseek-flash")]})
        route = self._route(listing)
        connection = route.connection()
        self.assertEqual([m.id for m in connection["models"]], ["deepseek-flash"])

    def test_admit_rejects_unsupported_listed_model(self):
        listing = parse_model_listing({"data": [_usable("mystery")]})
        with self.assertRaises(LlmFailure) as caught:
            self._route(listing).admit("mystery")
        self.assertEqual(caught.exception.code, MODEL_UNSUPPORTED)

    def test_admit_rejects_upgrade_required(self):
        listing = parse_model_listing({"data": [_usable(
            "mystery", client_requirements={"mini-harness": {"min_version": "9.9.9"}})]})
        with self.assertRaises(LlmFailure) as caught:
            self._route(listing).admit("mystery")
        self.assertEqual(caught.exception.code, CLIENT_UPGRADE_REQUIRED)

    def test_discover_empty_when_signed_out(self):
        self.assertEqual(self._route(None).discover_models(), [])

    def test_discover_lists_offered_when_signed_in(self):
        listing = parse_model_listing({"data": [_usable("deepseek-flash")]})
        names = [model["model"] for model in self._route(listing).discover_models()]
        self.assertEqual(names, ["deepseek-flash"])


class AdapterAccountGateTest(unittest.TestCase):
    def _route(self, listing):
        from miniharness.llm.deepseek_account import AccountRoute

        class _Cat:
            def current(_self):
                return {"signed_in": True, "listing": listing}
        return AccountRoute(catalog=_Cat(),
                            supported=(DeepSeekCatalogModel(id="deepseek-flash"),),
                            overrides={}, route_max_tokens=8192,
                            client_version="0.2.1")

    def test_adapter_admit_raises_for_account_route(self):
        from miniharness.llm.deepseek import DeepSeekAdapter
        listing = parse_model_listing({"data": [_usable("mystery")]})
        adapter = DeepSeekAdapter(account_token="t", account_route=self._route(listing))
        with self.assertRaises(LlmFailure) as caught:
            adapter.admit("mystery")
        self.assertEqual(caught.exception.code, MODEL_UNSUPPORTED)

    def test_official_missing_credential_has_empty_catalog(self):
        from miniharness.llm.deepseek import DeepSeekAdapter
        adapter = DeepSeekAdapter(api_key="", auth="official")
        self.assertEqual(adapter.available_models(), [])

    def test_official_credential_lists_models(self):
        from miniharness.llm.deepseek import DeepSeekAdapter
        adapter = DeepSeekAdapter(api_key="key", auth="official")
        self.assertTrue(adapter.available_models())

    def test_account_without_route_has_empty_catalog(self):
        from miniharness.llm.deepseek import DeepSeekAdapter
        adapter = DeepSeekAdapter(account_token="t", auth="account")
        self.assertEqual(adapter.available_models(), [])


if __name__ == "__main__":
    unittest.main()
