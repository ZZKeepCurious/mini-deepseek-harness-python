"""web_tools.network / web_tools.runtime 补充覆盖。

对齐上游 packages/web/web-fetch-http/src/network.ts 与 packages/web/web/src/index.ts。
网络解析走真实 ``resolver`` 注入与本地 ``localhost`` 系统解析；不做外网请求。
"""
from __future__ import annotations

import asyncio
import ipaddress
import threading
import unittest

from miniharness.web_tools.network import (
    IPV4ONLY_DISCOVERY_HOST,
    IPV4ONLY_SENTINELS,
    PublicAddress,
    _embedded_ipv4_address,
    _literal_family,
    _race_resolution,
    _resolve_hostname,
    discover_nat64_prefixes,
    is_non_public_ip_literal,
    is_public_ip_address,
    resolve_public_addresses,
    strip_ipv6_brackets,
    translated_ipv4_address,
)
from miniharness.web_tools.runtime import WebRuntime, WebRuntimeConfig
from miniharness.web_tools.types import WebError


def run(coro):
    return asyncio.run(coro)


class PublicAddressTest(unittest.TestCase):
    def test_eq_and_repr(self):
        a = PublicAddress("1.1.1.1", 4)
        self.assertEqual(a, PublicAddress("1.1.1.1", 4))
        self.assertNotEqual(a, PublicAddress("1.1.1.1", 6))
        self.assertEqual(a.__eq__("not-an-address"), NotImplemented)
        self.assertEqual(repr(a), "PublicAddress('1.1.1.1', 4)")


class LiteralHelpersTest(unittest.TestCase):
    def test_strip_brackets(self):
        self.assertEqual(strip_ipv6_brackets("[::1]"), "::1")
        self.assertEqual(strip_ipv6_brackets("::1"), "::1")
        self.assertEqual(strip_ipv6_brackets("example.com"), "example.com")

    def test_literal_family(self):
        self.assertEqual(_literal_family("8.8.8.8"), 4)
        self.assertEqual(_literal_family("2606:4700::1"), 6)
        self.assertEqual(_literal_family("not-an-ip"), 0)

    def test_is_non_public_literal(self):
        self.assertTrue(is_non_public_ip_literal("10.0.0.1"))
        self.assertTrue(is_non_public_ip_literal("[::1]"))
        self.assertFalse(is_non_public_ip_literal("8.8.8.8"))
        self.assertFalse(is_non_public_ip_literal("example.com"))

    def test_invalid_and_multicast(self):
        self.assertFalse(is_public_ip_address("definitely not an ip"))
        self.assertFalse(is_public_ip_address("224.0.0.1"))
        self.assertFalse(is_public_ip_address("ff02::1"))

    def test_ipv4_mapped_uses_embedded_v4(self):
        self.assertTrue(is_public_ip_address("::ffff:8.8.8.8"))
        self.assertFalse(is_public_ip_address("::ffff:10.0.0.1"))
        self.assertFalse(is_public_ip_address("::ffff:224.0.0.1"))

    def test_translation_prefixes_rejected(self):
        self.assertFalse(is_public_ip_address("64:ff9b::808:808"))
        self.assertFalse(is_public_ip_address("5f00::1"))


class EmbeddedIPv4Test(unittest.TestCase):
    def test_length_96_reads_tail(self):
        raw = ipaddress.ip_address("2606:4700::c000:aa").packed
        self.assertEqual(_embedded_ipv4_address(raw, 96), "192.0.0.170")

    def test_length_32_reads_middle(self):
        raw = ipaddress.ip_address("2606:4700:c000:aa::").packed
        self.assertEqual(_embedded_ipv4_address(raw, 32), "192.0.0.170")

    def test_length_64_requires_zero_byte_8(self):
        raw = ipaddress.ip_address("2001:db8:abcd:ef01:00c0:0000:aa11:1111").packed
        self.assertEqual(_embedded_ipv4_address(raw, 64), "192.0.0.170")

    def test_length_64_rejects_nonzero_byte_8(self):
        raw = ipaddress.ip_address("2001:db8:abcd:ef01:01c0:0000:00aa:1111").packed
        self.assertIsNone(_embedded_ipv4_address(raw, 64))

    def test_negative_prefix_span_returns_none(self):
        raw = ipaddress.ip_address("2606:4700::1").packed
        self.assertIsNone(_embedded_ipv4_address(raw, 72))


class DiscoverNat64Test(unittest.TestCase):
    def test_prefix_detected_and_deduped(self):
        async def resolver(_host):
            return [("2606:4700:c000:aa::", 6), ("2606:4700:c000:aa::", 6)]
        prefixes = run(discover_nat64_prefixes(resolver=resolver))
        self.assertEqual(prefixes, [(bytes.fromhex("26064700"), 32)])

    def test_non_sentinel_and_non_ipv6_skipped(self):
        async def resolver(_host):
            return [("2001:db8::1", 6), ("192.0.0.170", 4)]
        self.assertEqual(run(discover_nat64_prefixes(resolver=resolver)), [])

    def test_length_64_prefix_detected(self):
        async def resolver(_host):
            return [("2001:db8:abcd:ef01:00c0:0000:aa11:1111", 6)]
        prefixes = run(discover_nat64_prefixes(resolver=resolver))
        self.assertEqual(prefixes, [(bytes.fromhex("20010db8abcdef01"), 64)])

    def test_length_96_prefix_detected(self):
        async def resolver(_host):
            return [("2606:4700::c000:aa", 6)]
        prefixes = run(discover_nat64_prefixes(resolver=resolver))
        self.assertEqual(prefixes, [(bytes.fromhex("260647000000000000000000"), 96)])

    def test_sentinels_constant(self):
        self.assertEqual(IPV4ONLY_SENTINELS, frozenset({"192.0.0.170", "192.0.0.171"}))
        self.assertEqual(IPV4ONLY_DISCOVERY_HOST, "ipv4only.arpa")


class TranslatedIPv4Test(unittest.TestCase):
    def test_non_ipv6_returns_none(self):
        self.assertIsNone(translated_ipv4_address("8.8.8.8", [(bytes.fromhex("26064700"), 32)]))

    def test_no_matching_prefix(self):
        self.assertIsNone(
            translated_ipv4_address("2606:4700:0808:0808::", [(bytes.fromhex("00000000"), 32)]))

    def test_matching_prefix_returns_embedded(self):
        value = translated_ipv4_address(
            "2606:4700:c000:aa::", [(bytes.fromhex("26064700"), 32)])
        self.assertEqual(value, "192.0.0.170")


class ResolvePublicAddressesTest(unittest.TestCase):
    def _resolver(self, answers):
        async def resolver(_host):
            return answers
        return resolver

    def test_empty_answers_rejected(self):
        with self.assertRaises(WebError) as raised:
            run(resolve_public_addresses("example.com", resolver=self._resolver([])))
        self.assertEqual(raised.exception.code, "WEB_PROVIDER_ERROR")
        self.assertEqual(str(raised.exception),
                         'hostname "example.com" resolved to no addresses')

    def test_family_mismatch_is_invalid(self):
        with self.assertRaises(WebError) as raised:
            run(resolve_public_addresses("example.com",
                                         resolver=self._resolver([("1.2.3.4", 6)])))
        self.assertEqual(raised.exception.code, "WEB_PROVIDER_ERROR")
        self.assertEqual(str(raised.exception),
                         'hostname "example.com" resolved to an invalid IP address')

    def test_private_answer_blocked(self):
        with self.assertRaises(WebError) as raised:
            run(resolve_public_addresses("example.com",
                                         resolver=self._resolver([("10.0.0.1", 4)])))
        self.assertEqual(raised.exception.code, "WEB_BLOCKED_URL")

    def test_public_ipv4_answer_accepted(self):
        addresses = run(resolve_public_addresses(
            "example.com", resolver=self._resolver([("8.8.8.8", 4)])))
        self.assertEqual(addresses, [PublicAddress("8.8.8.8", 4)])

    def test_nat64_private_translation_blocked(self):
        async def resolver(host):
            # 发现域名与主查询都返回同一带哨兵内嵌的 v6 地址
            return [("2606:4700:c000:aa::", 6)]
        with self.assertRaises(WebError) as raised:
            run(resolve_public_addresses("example.com", resolver=resolver))
        self.assertEqual(raised.exception.code, "WEB_BLOCKED_URL")
        self.assertEqual(
            str(raised.exception),
            'URL hostname "example.com" resolves through NAT64 to a non-public IPv4 address')

    def test_nat64_public_translation_accepted(self):
        async def resolver(host):
            if host == IPV4ONLY_DISCOVERY_HOST:
                return [("2606:4700:c000:aa::", 6)]
            return [("2606:4700:0808:0808::", 6)]
        addresses = run(resolve_public_addresses("example.com", resolver=resolver))
        self.assertEqual(addresses, [PublicAddress("2606:4700:0808:0808::", 6)])

    def test_bracketed_literal_bypasses_resolver(self):
        # 字面地址在无 resolver 时直接由 _literal_family 判定，不触网
        addresses = run(resolve_public_addresses("[8.8.8.8]"))
        self.assertEqual(addresses, [PublicAddress("8.8.8.8", 4)])


class ResolveHostnameTest(unittest.TestCase):
    def test_localhost_resolves_without_network(self):
        resolved = run(_resolve_hostname("localhost"))
        self.assertTrue(resolved)
        for address, family in resolved:
            self.assertIn(family, (4, 6))
            self.assertIsInstance(address, str)


class RaceResolutionTest(unittest.TestCase):
    def test_pre_set_signal_aborts(self):
        signal = threading.Event()
        signal.set()
        with self.assertRaises(WebError) as raised:
            run(_race_resolution("localhost", signal))
        self.assertEqual(raised.exception.code, "WEB_ABORTED")

    def test_normal_resolution_returns_answers(self):
        self.assertTrue(run(_race_resolution("localhost", None)))

    def test_in_flight_cancel_aborts(self):
        class _FlipSignal:
            """实现 is_set 契约的信号替身：第二次轮询起报告取消。"""

            def __init__(self):
                self.calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls >= 2

        with self.assertRaises(WebError) as raised:
            run(_race_resolution("localhost", _FlipSignal()))
        self.assertEqual(raised.exception.code, "WEB_ABORTED")
        self.assertEqual(str(raised.exception), "web fetch aborted")


class _Provider:
    def __init__(self, id_, available=True):
        self.id = id_
        self._available = available
        self.search_requests = []
        self.fetch_requests = []

    def available(self):
        return self._available

    async def search(self, request, signal=None):
        self.search_requests.append(request)
        return {"sources": [{"url": f"https://{self.id}/{i}"} for i in range(5)],
                "truncated": False}

    async def fetch(self, request, signal=None):
        self.fetch_requests.append(request)
        return {"url": request["url"], "statusCode": 200,
                "body": {"kind": "text", "content": self.id}, "truncated": False}


class WebRuntimeTest(unittest.TestCase):
    def test_duplicate_registration_and_dispose(self):
        runtime = WebRuntime()
        dispose = runtime.register_search_provider(_Provider("a"))
        with self.assertRaises(WebError) as raised:
            runtime.register_search_provider(_Provider("a"))
        self.assertEqual(raised.exception.code, "WEB_DUPLICATE_PROVIDER")
        dispose()
        # dispose 后可再次注册同 id
        runtime.register_search_provider(_Provider("a"))

    def test_configured_search_truncates(self):
        provider = _Provider("dup")
        runtime = WebRuntime(WebRuntimeConfig(searchProvider="dup"))
        runtime.register_search_provider(provider)
        result = run(runtime.search({"query": "q", "maxResults": 2}))
        self.assertEqual(len(result["sources"]), 2)
        self.assertTrue(result["truncated"])

    def test_configured_search_no_cap(self):
        provider = _Provider("dup")
        runtime = WebRuntime(WebRuntimeConfig(searchProvider="dup"))
        runtime.register_search_provider(provider)
        result = run(runtime.search({"query": "q"}))
        self.assertEqual(len(result["sources"]), 5)
        self.assertFalse(result["truncated"])
        # maxResults >= 数量时不截断
        result2 = run(runtime.search({"query": "q", "maxResults": 99}))
        self.assertEqual(len(result2["sources"]), 5)

    def test_fetch_via_provider(self):
        runtime = WebRuntime(WebRuntimeConfig(fetchProvider="http"))
        runtime.register_fetch_provider(_Provider("http"))
        result = run(runtime.fetch({"url": "https://a/x"}))
        self.assertEqual(result["body"]["content"], "http")

    def test_configured_missing(self):
        runtime = WebRuntime(WebRuntimeConfig(searchProvider="ghost"))
        with self.assertRaises(WebError) as raised:
            run(runtime.search({"query": "q"}))
        self.assertEqual(raised.exception.code, "WEB_PROVIDER_CONFIGURED_MISSING")
        self.assertEqual(str(raised.exception),
                         'configured web provider "ghost" is not registered')

    def test_configured_unavailable(self):
        runtime = WebRuntime(WebRuntimeConfig(searchProvider="down"))
        runtime.register_search_provider(_Provider("down", available=False))
        with self.assertRaises(WebError) as raised:
            run(runtime.search({"query": "q"}))
        self.assertEqual(raised.exception.code, "WEB_PROVIDER_CONFIGURED_UNAVAILABLE")

    def test_auto_select_single(self):
        runtime = WebRuntime()
        runtime.register_search_provider(_Provider("only"))
        result = run(runtime.search({"query": "q"}))
        self.assertEqual(result["sources"][0]["url"], "https://only/0")

    def test_ambiguous_when_multiple(self):
        runtime = WebRuntime()
        runtime.register_search_provider(_Provider("a"))
        runtime.register_search_provider(_Provider("b"))
        with self.assertRaises(WebError) as raised:
            run(runtime.search({"query": "q"}))
        self.assertEqual(raised.exception.code, "WEB_PROVIDER_AMBIGUOUS")
        self.assertIn("a", str(raised.exception))
        self.assertIn("b", str(raised.exception))

    def test_unavailable_when_none_usable(self):
        runtime = WebRuntime()
        runtime.register_search_provider(_Provider("down", available=False))
        with self.assertRaises(WebError) as raised:
            run(runtime.search({"query": "q"}))
        self.assertEqual(raised.exception.code, "WEB_PROVIDER_UNAVAILABLE")

    def test_environment_fallback_selects_provider(self):
        import os
        os.environ["DSH_WEB_SEARCH_PROVIDER"] = "envid"
        try:
            runtime = WebRuntime()
            runtime.register_search_provider(_Provider("envid"))
            result = run(runtime.search({"query": "q"}))
            self.assertEqual(result["sources"][0]["url"], "https://envid/0")
        finally:
            os.environ.pop("DSH_WEB_SEARCH_PROVIDER", None)


if __name__ == "__main__":
    unittest.main()
