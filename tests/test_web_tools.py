"""web_tools 族测试：seam 选择与封顶、搜索/抓取工具契约、DeepSeek provider wire、
fetch_http 限额与重定向、HTML→markdown、装配面。网络用例只打 127.0.0.1 环回
本地线程服务（proxy 环境已绕过）。
"""
from __future__ import annotations

import asyncio
import http.server
import json
import threading
import time
import unittest

from miniharness.core.scope import Context
from miniharness.core.tools import ToolExec, ToolRegistry
from miniharness.web_tools import (
    WEB_FETCH_SECTION_ORDER,
    WEB_SEARCH_SECTION_ORDER,
    WebConfig,
    install_web,
    register_web_tools,
)
from miniharness.web_tools.fetch_http import HttpFetchLimits, HttpFetchProvider, build_http_limits, run_deadlined
from miniharness.web_tools.fetch_tool import (
    TRUNCATION_FOOTER,
    exceeds_conversion_depth,
    format_fetch_output,
    parse_fetch_args,
    web_fetch_tool,
)
from miniharness.web_tools.network import PublicAddress, is_public_ip_address
from miniharness.web_tools.policy import (
    WEB_FETCH_MAX_URL_LENGTH,
    FetchUrl,
    parse_fetch_url,
    validate_fetch_url,
)
from miniharness.web_tools.search_deepseek import (
    DEEPSEEK_PROVIDER_ID,
    DeepSeekSearchProvider,
    citation_snippets,
    map_anthropic_response,
)
from miniharness.web_tools.search_tool import (
    WEB_SEARCH_MAX_QUERIES,
    WEB_SEARCH_MAX_RESULTS,
    format_search_output,
    merge_search_results,
    parse_search_args,
    run_search_queries,
    web_search_section_text,
    web_search_tool,
)
from miniharness.web_tools.trust import EXTERNAL_WEB_CONTENT_NOTICE
from miniharness.web_tools.types import WebError


def run(coro):
    return asyncio.run(coro)


def _options(base="http://127.0.0.1:1", max_tokens=4096, max_uses=5, key=None, resolve=lambda: ""):
    return lambda: {
        "baseURL": base,
        "model": "deepseek-v4-flash",
        "apiVersion": "2023-06-01",
        "maxTokens": max_tokens,
        "maxUses": max_uses,
        "apiKey": key or "",
        "apiKeyEnv": "DEEPSEEK_API_KEY",
        "resolveApiKey": resolve,
    }


class ParseSearchArgsTest(unittest.TestCase):
    def test_empty_rejected(self):
        with self.assertRaisesRegex(ValueError, "queries must contain at least one query"):
            parse_search_args({"queries": []}, 4)

    def test_over_bound_rejected_with_plural(self):
        with self.assertRaisesRegex(ValueError, "queries must contain at most 4 queries"):
            parse_search_args({"queries": ["a", "b", "c", "d", "e"]}, 4)

    def test_over_bound_rejected_with_singular(self):
        with self.assertRaisesRegex(ValueError, "queries must contain at most 1 query"):
            parse_search_args({"queries": ["a", "b"]}, 1)

    def test_blank_rejected(self):
        with self.assertRaisesRegex(ValueError, "each query must be a non-empty string"):
            parse_search_args({"queries": ["ok", "   "]}, 4)

    def test_dedupe_after_bound_keeps_order(self):
        self.assertEqual(
            parse_search_args({"queries": ["b", "a", "b", "c", "a"]}, 6),
            ["b", "a", "c"],
        )


class FormatSearchOutputTest(unittest.TestCase):
    def test_full_layout(self):
        text = format_search_output({
            "content": "answer",
            "sources": [
                {"url": "https://a/x", "title": "T", "snippet": "S", "publishedAt": "d"},
                {"url": "https://b/y"},
            ],
            "truncated": False,
        })
        self.assertEqual(
            text,
            f"{EXTERNAL_WEB_CONTENT_NOTICE}\n\nanswer\n\n"
            "Sources:\n- [T](https://a/x) — S (d)\n- [b](https://b/y)\n\n"
            "Cite the relevant URLs above as markdown links in your answer.",
        )

    def test_label_falls_back_to_hostname(self):
        text = format_search_output({"content": None, "sources": [
            {"url": "https://example.com/page"}, {"url": "###not-a-url###"},
        ], "truncated": False})
        self.assertIn("- [example.com](https://example.com/page)", text)
        self.assertIn("- [###not-a-url###](###not-a-url###)", text)

    def test_no_sources_no_content(self):
        text = format_search_output({"content": None, "sources": [], "truncated": False})
        self.assertIn("No results found.", text)

    def test_truncated_note_counts_sources(self):
        text = format_search_output({"content": "answer", "sources": [
            {"url": "https://a/x"},
        ], "truncated": True})
        self.assertIn("(Showing the first 1 sources. Refine the query for more.)", text)


class MergeSearchResultsTest(unittest.TestCase):
    def test_round_robin_dedupe_and_sections(self):
        merged = merge_search_results(
            ["q1", "q2"],
            [
                {"content": "c1", "sources": [{"url": "https://u/1"}, {"url": "https://u/2"}], "truncated": False},
                {"content": "c2", "sources": [{"url": "https://u/2"}, {"url": "https://u/3"}], "truncated": True},
            ],
            8,
        )
        self.assertEqual([s["url"] for s in merged["sources"]], [
            "https://u/1", "https://u/2", "https://u/3"])
        self.assertEqual(merged["content"], "### q1\n\nc1\n\n### q2\n\nc2")
        self.assertTrue(merged["truncated"])

    def test_dedup_gives_room_and_caps(self):
        merged = merge_search_results(
            ["q1", "q2"],
            [
                {"sources": [{"url": "https://u/1"}, {"url": "https://u/2"}], "truncated": False},
                {"sources": [{"url": "https://u/1"}, {"url": "https://u/3"}], "truncated": False},
            ],
            2,
        )
        # 去重后 u1,u2,u3 只有两只名额；rank0 填满两只，rank1 时已满 → dropped
        self.assertEqual([s["url"] for s in merged["sources"]], ["https://u/1", "https://u/2"])
        self.assertTrue(merged["truncated"])

    def test_merge_truncation_when_cap_hits_first_rank(self):
        merged = merge_search_results(
            ["q1"],
            [{"sources": [{"url": "https://u/1"}, {"url": "https://u/2"}], "truncated": False}],
            1,
        )
        self.assertEqual(merged["sources"], [{"url": "https://u/1"}])
        self.assertTrue(merged["truncated"])


class _FakeWeb:
    def __init__(self, searches, fetch=None, fail=None):
        self._searches = searches
        self._fetch = fetch
        self._fail = fail
        self.failed_at = None

    async def search(self, request, signal=None):
        if self._fail is not None:
            self.failed_at = request["query"]
            raise self._fail
        return {"sources": self._searches[request["query"]], "truncated": False}

    async def fetch(self, request, signal=None):
        return self._fetch


class RunSearchQueriesTest(unittest.TestCase):
    def test_single_keeps_provider_result(self):
        web = _FakeWeb({"q": [{"url": "https://u/1"}]})
        result = run(run_search_queries(web, ["q"], 8, None))
        self.assertEqual(result["sources"], [{"url": "https://u/1"}])

    def test_multi_merges_and_rethrows_first_failure(self):
        error = WebError("boom", "WEB_PROVIDER_ERROR")
        web = _FakeWeb({"a": [], "b": []}, fail=error)
        web._searches = {"a": [{"url": "https://u/1"}]}
        try:
            run(run_search_queries(web, ["a", "b"], 8, None))
        except WebError as e:
            self.assertIs(e, error)
        else:
            self.fail("expected first failure to be rethrown")


class PolicyTest(unittest.TestCase):
    def test_invalid_url(self):
        with self.assertRaises(WebError) as cm:
            parse_fetch_url("not a url without scheme")
        self.assertEqual(cm.exception.code, "WEB_INVALID_URL")
        self.assertEqual(str(cm.exception).startswith("invalid URL:"), True)

    def test_unsupported_scheme(self):
        with self.assertRaisesRegex(WebError, 'unsupported URL scheme "ftp:" \\(only http and https are allowed\\)'):
            parse_fetch_url("ftp://example.com/x")

    def test_credentials_rejected(self):
        with self.assertRaises(WebError) as cm:
            parse_fetch_url("https://u:p@example.com/")
        self.assertEqual(cm.exception.code, "WEB_BLOCKED_URL")

    def test_length_bound(self):
        with self.assertRaisesRegex(WebError, f"URL exceeds the maximum length of {WEB_FETCH_MAX_URL_LENGTH}"):
            validate_fetch_url("https://e.com/" + "x" * WEB_FETCH_MAX_URL_LENGTH)

    def test_default_port_omitted_and_lowercased(self):
        url = parse_fetch_url("HTTP://Example.COM:80/A?b=1#frag")
        self.assertEqual(url.protocol, "http:")
        self.assertEqual(url.hostname, "example.com")
        self.assertEqual(url.port, "")
        self.assertEqual(url.origin, "http://example.com")
        self.assertEqual(url.href, "http://example.com/A?b=1#frag")
        self.assertEqual(url.request_url, "http://example.com/A?b=1")

    def test_origin_and_cross_origin(self):
        a = parse_fetch_url("https://a.com/x")
        b = parse_fetch_url("https://a.com/y")
        c = parse_fetch_url("https://a.com:8443/y")
        self.assertTrue(a.origin == b.origin)
        self.assertNotEqual(c.origin, a.origin)


class PublicAddressTest(unittest.TestCase):
    def test_private_rejected(self):
        for addr in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1"):
            self.assertFalse(is_public_ip_address(addr))

    def test_public_accepted(self):
        self.assertTrue(is_public_ip_address("8.8.8.8"))
        self.assertTrue(is_public_ip_address("1.1.1.1"))
        self.assertTrue(is_public_ip_address("2606:4700::1111"))

    def test_multicast_and_transition_rejected(self):
        self.assertFalse(is_public_ip_address("224.0.0.1"))        # IPv4 multicast
        self.assertFalse(is_public_ip_address("ff00::1"))           # IPv6 multicast
        self.assertFalse(is_public_ip_address("64:ff9b::808:808"))  # NAT64
        self.assertFalse(is_public_ip_address("5f00::1"))           # SRv6

    def test_literal_family_checked_in_resolve(self):
        from miniharness.web_tools import network
        async def scenario():
            try:
                await network.resolve_public_addresses("10.1.2.3")
            except WebError as e:
                return e
        error = run(scenario())
        self.assertEqual(error.code, "WEB_BLOCKED_URL")


class BuildLimitsTest(unittest.TestCase):
    def test_positive_finite(self):
        for name, bad in (("maxResponseBytes", 0), ("maxBodyChars", -1)):
            with self.assertRaisesRegex(ValueError, rf"web-fetch-http: {name} must be a positive finite number"):
                build_http_limits({name: bad})

    def test_timeout_upper_bound(self):
        with self.assertRaisesRegex(ValueError, "web-fetch-http: timeoutMs must be no greater than 2147483647"):
            build_http_limits({"timeoutMs": 2_147_483_648})

    def test_non_negative_integer(self):
        with self.assertRaisesRegex(ValueError, "web-fetch-http: maxRedirects must be a non-negative integer"):
            build_http_limits({"maxRedirects": -1})
        with self.assertRaisesRegex(ValueError, "web-fetch-http: maxRedirects must be a non-negative integer"):
            build_http_limits({"maxRedirects": 1.5})

    def test_defaults(self):
        limits = build_http_limits(None)
        self.assertEqual(
            limits, HttpFetchLimits(5_000_000, 100_000, 30_000, 5,
                                    "deepseek-harness/0.0.1 (+https://github.com/deepseek-ai)"))


class RunDeadlinedTest(unittest.TestCase):
    def test_success(self):
        async def work():
            return "ok"
        self.assertEqual(run(run_deadlined(work(), 1000, None)), "ok")

    def test_timeout(self):
        async def work():
            await asyncio.sleep(5)
        with self.assertRaises(WebError) as cm:
            run(run_deadlined(work(), 30, None))
        self.assertEqual(cm.exception.code, "WEB_FETCH_TIMEOUT")
        self.assertEqual(str(cm.exception), "web fetch timed out")

    def test_abort_wins_over_work_done_first(self):
        async def work():
            await asyncio.sleep(0.05)
            return "late"
        signal = threading.Event()
        signal.set()
        with self.assertRaises(WebError) as cm:
            run(run_deadlined(work(), 500, signal))
        self.assertEqual(cm.exception.code, "WEB_ABORTED")

    def test_web_error_passes_through(self):
        async def work():
            raise WebError("already classified", "WEB_INVALID_URL")
        with self.assertRaises(WebError) as cm:
            run(run_deadlined(work(), 500, None))
        self.assertEqual(cm.exception.code, "WEB_INVALID_URL")

    def test_plain_error_classified(self):
        async def work():
            raise OSError("boom")
        with self.assertRaises(WebError) as cm:
            run(run_deadlined(work(), 500, None))
        self.assertEqual(cm.exception.code, "WEB_PROVIDER_ERROR")
        self.assertEqual(str(cm.exception), "web fetch failed: OSError: boom")

    def test_plain_error_with_signal_is_aborted(self):
        async def work():
            await asyncio.sleep(0.02)
            raise OSError("boom")
        signal = threading.Event()
        signal.set()
        with self.assertRaises(WebError) as cm:
            run(run_deadlined(work(), 500, signal))
        self.assertEqual(cm.exception.code, "WEB_ABORTED")


class _HttpServer:
    def __init__(self, behaviors):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.behaviors = behaviors
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        self.port = server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


async def _pinned_local(host, signal):
    return [PublicAddress("127.0.0.1", 4)]


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self._route()

    def do_POST(self):  # noqa: N802
        self._route()

    def _route(self):
        self.close_connection = True
        behavior = self.server.behaviors.get(self.path, (404, {}, b"not found"))
        if isinstance(behavior, tuple):
            status, headers, body = behavior
            delay = 0.0
            omit_length = False
        else:
            status = behavior.get("status", 200)
            headers = behavior.get("headers", {})
            body = behavior.get("body", b"")
            delay = behavior.get("delay", 0.0)
            omit_length = behavior.get("omitLength", False)
        if delay:
            time.sleep(delay)
        merged = dict(headers)
        if omit_length:
            merged["Connection"] = "close"
        else:
            merged.setdefault("Content-Length", str(len(body)))
        self.send_response(status)
        for key, value in merged.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *args):  # pragma: no cover - 静音
        pass


class HttpFetchProviderTest(unittest.TestCase):
    def tearDown(self):
        for server in getattr(self, "_servers", []):
            server.close()

    def _provider(self, server, **overrides):
        return HttpFetchProvider(
            build_http_limits(overrides),
            resolve_addresses=_pinned_local,
        )

    def test_fetch_text_plausible(self):
        server = _HttpServer({"/t": (200, {"Content-Type": "text/plain; charset=utf-8"}, b"hello world")})
        self._servers = [server]
        provider = self._provider(server)
        result = run(provider.fetch({"url": server.base + "/t"}))
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], {"kind": "text", "content": "hello world"})
        self.assertFalse(result["truncated"])

    def test_unsupported_charset(self):
        server = _HttpServer({"/t": (200, {"Content-Type": "text/plain; charset=not-a-encoding"}, b"x")})
        self._servers = [server]
        provider = self._provider(server)
        with self.assertRaises(WebError) as cm:
            run(provider.fetch({"url": server.base + "/t"}))
        self.assertEqual(cm.exception.code, "WEB_UNSUPPORTED_CONTENT_TYPE")
        self.assertIn('unsupported charset "not-a-encoding"', str(cm.exception))

    def test_unsupported_content_type(self):
        server = _HttpServer({"/z": (200, {"Content-Type": "application/zip"}, b"x" * 10)})
        self._servers = [server]
        provider = self._provider(server)
        with self.assertRaises(WebError) as cm:
            run(provider.fetch({"url": server.base + "/z"}))
        self.assertEqual(cm.exception.code, "WEB_UNSUPPORTED_CONTENT_TYPE")

    def test_declared_content_length_too_large(self):
        server = _HttpServer({"/big": (200, {"Content-Type": "text/plain",
                                             "Content-Length": "999999999"}, b"x" * 8)})
        self._servers = [server]
        provider = self._provider(server, maxResponseBytes=10)
        with self.assertRaises(WebError) as cm:
            run(provider.fetch({"url": server.base + "/big"}))
        self.assertEqual(cm.exception.code, "WEB_FETCH_TOO_LARGE")
        self.assertIn("response exceeds the maximum of 10 bytes", str(cm.exception))

    def test_body_char_cap_truncates(self):
        server = _HttpServer({"/chars": (200, {"Content-Type": "text/plain; charset=utf-8"}, b"x" * 5000)})
        self._servers = [server]
        provider = self._provider(server, maxBodyChars=100)
        result = run(provider.fetch({"url": server.base + "/chars"}))
        self.assertTrue(result["truncated"])
        self.assertEqual(result["body"]["content"], "x" * 100)

    def test_stream_byte_cap_truncates_not_rejects(self):
        server = _HttpServer({"/s": {"status": 200, "headers": {"Content-Type": "text/plain"},
                                     "body": b"x" * 5000, "omitLength": True}})
        self._servers = [server]
        provider = self._provider(server, maxResponseBytes=100)
        result = run(provider.fetch({"url": server.base + "/s"}))
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["body"]["content"]), 100)

    def test_same_origin_redirect_followed_and_fragment_stripped(self):
        server = _HttpServer({
            "/r": (302, {"Location": "/final"}, b""),
            "/final": (200, {"Content-Type": "text/html"}, b"<title>done</title><p>ok</p>"),
        })
        self._servers = [server]
        provider = self._provider(server)
        result = run(provider.fetch({"url": server.base + "/r#frag"}))
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["url"], server.base + "/final")
        self.assertNotIn("#frag", result["url"])
        self.assertEqual(result["body"]["kind"], "html")

    def test_cross_origin_redirect_rejected(self):
        other = _HttpServer({"/dest": (200, {"Content-Type": "text/plain"}, b"x")})
        server = _HttpServer({"/r": (302, {"Location": f"{other.base}/dest"}, b"")})
        self._servers = [server, other]
        provider = self._provider(server)
        with self.assertRaises(WebError) as cm:
            run(provider.fetch({"url": server.base + "/r"}))
        self.assertEqual(cm.exception.code, "WEB_REDIRECT_BLOCKED")
        self.assertIn("cross-origin redirect to", str(cm.exception))

    def test_redirect_without_location(self):
        server = _HttpServer({"/r": (302, {}, b"")})
        self._servers = [server]
        provider = self._provider(server)
        with self.assertRaises(WebError) as cm:
            run(provider.fetch({"url": server.base + "/r"}))
        self.assertEqual(cm.exception.code, "WEB_PROVIDER_ERROR")

    def test_redirect_budget_exceeded(self):
        server = _HttpServer({
            "/1": (302, {"Location": "/2"}, b""),
            "/2": (302, {"Location": "/3"}, b""),
            "/3": (200, {"Content-Type": "text/plain"}, b"x"),
        })
        self._servers = [server]
        provider = self._provider(server, maxRedirects=1)
        with self.assertRaises(WebError) as cm:
            run(provider.fetch({"url": server.base + "/1"}))
        self.assertEqual(cm.exception.code, "WEB_REDIRECT_BLOCKED")
        self.assertIn("exceeded the maximum of 1 redirects", str(cm.exception))

    def test_fragment_never_delivered(self):
        seen = {}

        class Recording(_Handler):
            def do_GET(self):
                seen["path"] = self.path
                super().do_GET()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recording)
        server.behaviors = {"/page": (200, {"Content-Type": "text/plain"}, b"ok")}
        try:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            provider = HttpFetchProvider(
                build_http_limits(None),
                resolve_addresses=_pinned_local,
            )
            port = server.server_address[1]
            run(provider.fetch({"url": f"http://127.0.0.1:{port}/page#sec"}))
            self.assertEqual(seen, {"path": "/page"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_boot_abort_and_runtime_abort(self):
        server = _HttpServer({"/t": (200, {"Content-Type": "text/plain"}, b"x")})
        self._servers = [server]
        provider = self._provider(server)
        signal = threading.Event()
        signal.set()
        with self.assertRaises(WebError) as cm:
            run(provider.fetch({"url": server.base + "/t"}, signal))
        self.assertEqual(cm.exception.code, "WEB_ABORTED")

        slow = _HttpServer({"/s": {"status": 200, "headers": {"Content-Type": "text/plain"},
                                   "body": b"x", "delay": 1.0}})
        self._servers.append(slow)
        provider = self._provider(slow)
        late_signal = threading.Event()

        def set_signal():
            time.sleep(0.2)
            late_signal.set()

        threading.Thread(target=set_signal, daemon=True).start()
        with self.assertRaises(WebError) as cm:
            run(provider.fetch({"url": slow.base + "/s"}, late_signal))
        self.assertEqual(cm.exception.code, "WEB_ABORTED")

    def test_available_always_true(self):
        server = _HttpServer({"/": (200, {}, b"")})
        self._servers = [server]
        self.assertTrue(self._provider(server).available())


class FetchToolTest(unittest.TestCase):
    def test_parse_empty_url(self):
        with self.assertRaisesRegex(ValueError, "url must be a non-empty string"):
            parse_fetch_args({"url": "  "})

    def test_format_caps_and_truncates(self):
        result = {
            "url": "https://a/x",
            "statusCode": 200,
            "body": {"kind": "text", "content": "hello world"},
            "truncated": False,
        }
        text = format_fetch_output(result, 10 ** 6)
        self.assertTrue(text.startswith("Fetched https://a/x (HTTP 200)"))
        self.assertIn(EXTERNAL_WEB_CONTENT_NOTICE, text)

        capped = format_fetch_output(result, 20)
        self.assertEqual(len(capped), 20)
        self.assertTrue(capped.startswith("Fetched"))
        self.assertNotIn(TRUNCATION_FOOTER, capped)

    def test_format_truncation_flag_adds_footer(self):
        result = {"url": "u", "statusCode": 200,
                  "body": {"kind": "text", "content": "x"}, "truncated": True}
        text = format_fetch_output(result, 10 ** 6)
        self.assertIn(TRUNCATION_FOOTER, text)

    def test_cap_smaller_than_footer_slices(self):
        result = {"url": "u", "statusCode": 200,
                  "body": {"kind": "text", "content": "xyz"}, "truncated": True}
        text = format_fetch_output(result, 10)
        self.assertEqual(len(text), 10)

    def test_depth_guard(self):
        self.assertFalse(exceeds_conversion_depth("<div><p>hi</p></div>"))
        self.assertFalse(exceeds_conversion_depth("<div>" * 100 + "x" + "</div>" * 100))
        self.assertTrue(exceeds_conversion_depth("<div>" * 600 + "x" + "</div>" * 600))

    def test_remove_non_visible_affects_conversion(self):
        from miniharness.web_tools.fetch_tool import render_body
        html = ("<html><body><h1>Hi</h1>"
                "<script>alert('bad')</script>"
                "<p style=\"display:none\">hidden</p>"
                "<ul><li>a</li><li>b</li></ul></body></html>")
        rendered = render_body({"kind": "html", "content": html}, 10 ** 6)
        self.assertIn("# Hi", rendered["text"])
        self.assertNotIn("alert", rendered["text"])
        self.assertNotIn("hidden", rendered["text"])
        text = rendered["text"].splitlines()
        self.assertIn("- a", text)
        self.assertIn("- b", text)

    def test_html_conversion_failure_omits_markup(self):
        from miniharness.web_tools.fetch_tool import render_body
        self.assertFalse(render_body({"kind": "html", "content": "x"}, 10 ** 6)["sourceTruncated"])
        truncated = render_body({"kind": "html", "content": "x" * 100}, 10)
        self.assertTrue(truncated["sourceTruncated"])

    def test_text_passthrough(self):
        from miniharness.web_tools.fetch_tool import render_body
        rendered = render_body({"kind": "text", "content": "plain\ncontent"}, 10 ** 6)
        self.assertEqual(rendered["text"], "plain\ncontent")

    def test_web_fetch_tool_execute_passthrough(self):
        web = _FakeWeb({}, fetch={
            "url": "u", "statusCode": 200,
            "body": {"kind": "text", "content": "x"}, "truncated": False,
        })
        tool = web_fetch_tool(web, 30_000, 200_000)
        exec_ = ToolExec()
        value = asyncio.run(tool.execute({"url": "u"}, exec_))
        self.assertEqual(value, {"url": "u", "statusCode": 200,
                                 "body": {"kind": "text", "content": "x"}, "truncated": False})
        self.assertEqual(tool.timeout_ms, 30_000)
        self.assertTrue(tool.is_concurrency_safe)


class SearchToolRegistrationTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="test")
        self.reg = ToolRegistry(self.ctx)

    def test_register_and_execute_roundtrip(self):
        web = _FakeWeb({})
        web._searches = {"q": [{"url": "https://a/x", "title": "T", "snippet": "sn"}]}
        register_web_tools(self.reg, web, _FakePrompt.capture(), WebConfig())
        tool = self.reg.resolve("web_search")
        self.assertIsNotNone(tool)
        value = asyncio.run(tool.execute({"queries": ["q"]}, ToolExec()))
        self.assertEqual(value, {"sources": [{"url": "https://a/x", "title": "T", "snippet": "sn"}],
                                 "truncated": False})
        blocks = tool.render({"queries": ["q"]}, value)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "text")
        self.assertIn("Sources:", blocks[0]["text"])

    def test_timeout_fields_defaults(self):
        web = _FakeWeb({})
        reg = ToolRegistry(Context(name="test"))
        register_web_tools(reg, web, None, WebConfig())
        search = reg.resolve("web_search")
        self.assertEqual(search.timeout_ms, 60_000)
        fetch = reg.resolve("web_fetch")
        self.assertEqual(fetch.timeout_ms, 30_000)
        self.assertEqual(fetch.output["required"], ["url", "statusCode", "body", "truncated"])

    def test_fetch_disabled(self):
        web = _FakeWeb({})
        reg = ToolRegistry(Context(name="test"))
        register_web_tools(reg, web, None, WebConfig(fetch=False))
        self.assertIsNone(reg.resolve("web_fetch"))
        self.assertIsNotNone(reg.resolve("web_search"))

    def test_search_disabled(self):
        web = _FakeWeb({})
        reg = ToolRegistry(Context(name="test"))
        register_web_tools(reg, web, None, WebConfig(search=False))
        self.assertIsNone(reg.resolve("web_search"))
        self.assertIsNotNone(reg.resolve("web_fetch"))

    def test_sections_registered_with_order_and_variants(self):
        captured = _FakePrompt.capture()
        web = _FakeWeb({})
        register_web_tools(ToolRegistry(Context(name="test")), web, captured, WebConfig())
        names = [s["name"] for s in captured.sections]
        self.assertEqual(names, ["tool:web_search", "tool:web_fetch"])
        self.assertEqual(captured.sections[0]["order"], WEB_SEARCH_SECTION_ORDER)
        self.assertEqual(captured.sections[1]["order"], WEB_FETCH_SECTION_ORDER)
        self.assertIn("web_fetch", captured.sections[0]["text"])
        self.assertIn("web_search", captured.sections[1]["text"])

    def test_section_variant_without_fetch(self):
        captured = _FakePrompt.capture()
        web = _FakeWeb({})
        register_web_tools(ToolRegistry(Context(name="test")), web, captured, WebConfig(fetch=False))
        self.assertNotIn("web_fetch", captured.sections[0]["text"])
        self.assertIn("snippets when available", captured.sections[0]["text"])

    def test_no_system_prompt_skips_sections(self):
        web = _FakeWeb({})
        reg = ToolRegistry(Context(name="test"))
        register_web_tools(reg, web, None, WebConfig())
        self.assertIsNotNone(reg.resolve("web_search"))


class _FakePrompt:
    def __init__(self):
        self.sections = []

    @staticmethod
    def capture():
        return _FakePrompt()

    def section(self, name, order, text, complete=False):
        self.sections.append({"name": name, "order": order, "text": text})
        return lambda: None


class InstallWebTest(unittest.TestCase):
    def test_provides_web_and_registers_providers(self):
        ctx = Context(name="test")
        runtime = install_web(ctx, WebConfig())
        self.assertIs(ctx.get("web"), runtime)
        self.assertEqual(
            [p.id for p in list(runtime._search_providers.values())], [DEEPSEEK_PROVIDER_ID])
        self.assertEqual([p.id for p in list(runtime._fetch_providers.values())], ["http"])
        self.assertEqual(runtime.tool_config.search_timeout_ms, 60_000)

    def test_invalid_config_fails_loud(self):
        from miniharness.web_tools import WebConfig
        ctx = Context(name="test")
        with self.assertRaisesRegex(ValueError,
                                    r"tool-web: searchMaxResults must be a positive integer"):
            install_web(ctx, WebConfig(search_max_results=0))

    def test_credential_missing_message(self):
        original = _options()
        provider = DeepSeekSearchProvider(lambda: dict(original(), apiKey="", resolveApiKey=lambda: ""))
        with self.assertRaises(WebError) as cm:
            run(provider.search({"query": "q"}))
        self.assertEqual(cm.exception.code, "WEB_PROVIDER_CREDENTIAL_MISSING")
        self.assertIn('DeepSeek search has no API key for "DEEPSEEK_API_KEY"', str(cm.exception))


class DeepSeekSearchProviderTest(unittest.TestCase):
    def tearDown(self):
        for server in getattr(self, "_servers", []):
            server.close()

    def _provider(self, base_url):
        return DeepSeekSearchProvider(_options(base=base_url, key="k1"))

    def test_available_independent_of_key(self):
        self.assertFalse(DeepSeekSearchProvider(_options(max_tokens=-1)).available())
        self.assertFalse(DeepSeekSearchProvider(_options(max_uses=0)).available())
        self.assertFalse(DeepSeekSearchProvider(_options(base="not a url")).available())
        self.assertTrue(DeepSeekSearchProvider(_options()).available())

    def test_citation_snippets(self):
        blocks = [
            {"type": "text", "citations": [{"url": "https://a/x", "cited_text": "one"}]},
            {"type": "tool_use"},
            {"type": "text", "citations": [
                {"url": "https://a/x", "cited_text": "dup"},
                {"url": "https://b/y", "cited_text": "two"},
            ]},
        ]
        self.assertEqual(citation_snippets(blocks), {"https://a/x": "one", "https://b/y": "two"})

    def test_map_gathers_sources_and_truncates_absent_fields(self):
        response = {"content": [
            {"type": "web_search_tool_result", "content": [
                {"type": "web_search_result", "url": "https://a/x", "title": "A", "page_age": "2 days"},
                {"type": "web_search_result", "url": "https://b/y"},
            ]},
            {"type": "text", "citations": [{"url": "https://a/x", "cited_text": "snippet a"}]},
        ]}
        result = map_anthropic_response(response)
        self.assertEqual(result["sources"], [
            {"url": "https://a/x", "title": "A", "snippet": "snippet a", "publishedAt": "2 days"},
            {"url": "https://b/y"},
        ])
        self.assertNotIn("content", result)
        self.assertFalse(result["truncated"])

    def test_map_rejects_no_result_blocks(self):
        with self.assertRaises(WebError) as cm:
            map_anthropic_response({"content": [{"type": "text", "text": "no results"}]})
        self.assertEqual(cm.exception.code, "WEB_PROVIDER_ERROR")
        self.assertIn("no web_search_tool_result blocks", str(cm.exception))

    def test_successful_search_wire(self):
        server = _HttpServer({"/messages": (200, {
            "Content-Type": "application/json",
        }, _json_bytes({"content": [
            {"type": "web_search_tool_result", "content": [
                {"type": "web_search_result", "url": "https://a/x", "title": "A"},
            ]},
        ]}))})
        self._servers = [server]
        provider = self._provider(server.base)
        result = run(provider.search({"query": "q1"}))
        self.assertEqual(result["sources"], [{"url": "https://a/x", "title": "A"}])

    def test_redirect_rejected(self):
        server = _HttpServer({"/messages": (302, {"Location": "/elsewhere"}, b"")})
        self._servers = [server]
        provider = self._provider(server.base)
        with self.assertRaises(WebError) as cm:
            run(provider.search({"query": "q"}))
        self.assertEqual(cm.exception.code, "WEB_PROVIDER_ERROR")
        self.assertIn("web request rejected redirect", str(cm.exception))

    def test_http_error_detail(self):
        server = _HttpServer({"/messages": (400, {"Content-Type": "application/json"},
                                             _json_bytes({"error": {"message": "bad request"}}))})
        self._servers = [server]
        provider = self._provider(server.base)
        with self.assertRaises(WebError) as cm:
            run(provider.search({"query": "q"}))
        self.assertIn("DeepSeek API error (HTTP 400): bad request", str(cm.exception))

    def test_http_error_string_detail(self):
        server = _HttpServer({"/messages": (429, {"Content-Type": "application/json"},
                                             _json_bytes({"error": "rate limited"}))})
        self._servers = [server]
        provider = self._provider(server.base)
        with self.assertRaises(WebError) as cm:
            run(provider.search({"query": "q"}))
        self.assertIn("DeepSeek API error (HTTP 429): rate limited", str(cm.exception))

    def test_abort_during_request(self):
        server = _HttpServer({"/messages": {"status": 200,
                                            "headers": {"Content-Type": "application/json"},
                                            "body": _json_bytes({"content": []}), "delay": 5.0}})
        self._servers = [server]
        provider = self._provider(server.base)
        signal = threading.Event()

        def set_signal():
            time.sleep(0.3)
            signal.set()

        threading.Thread(target=set_signal, daemon=True).start()
        with self.assertRaises(WebError) as cm:
            run(provider.search({"query": "q"}, signal))
        self.assertEqual(cm.exception.code, "WEB_ABORTED")
        self.assertEqual(str(cm.exception), "DeepSeek search aborted")


def _json_bytes(value):
    import json
    return json.dumps(value).encode("utf-8")