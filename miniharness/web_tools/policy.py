"""URL 校验与内容类型分类（上游 packages/web/web-fetch-http/src/policy.ts）。

纯函数、零网络的半个 provider；另半个（传输/重定向/限额/解码读取）在
fetch_http.py。错误码与消息逐字对齐 policy.ts。

与上游的载体差异（登记于 verified-diffs）：
  * 上游用 WHATWG ``URL``；mini 用 ``urllib.parse.urlsplit`` + 本地归一
    （协议小写、默认端口省略、空 hostname 判非法、host 小写）。空 netloc /
    相对 URL / 端口越界在 WHATWG 抛 parse error，mini 在 parse_fetch_url
    显式判 ``invalid URL: {input}``。
  * ``TextDecoder`` → ``codecs.lookup`` + ``errors='replace'``：标签集近似
    （WHATWG Encoding 标签 vs Python 注册名），非法序列同为 U+FFFD。
"""
from __future__ import annotations

import codecs
import re
from urllib.parse import urlsplit

from .types import WebError

__all__ = [
    "WEB_FETCH_MAX_URL_LENGTH",
    "FetchUrl",
    "classify_content_type",
    "decoder_for_charset",
    "is_same_origin",
    "parse_charset",
    "parse_fetch_url",
    "validate_fetch_url",
]

#: 请求 URL 长度上限（policy.ts:12）。
WEB_FETCH_MAX_URL_LENGTH = 2048

#: WHATWG 对 http/https 省略的默认端口。
_DEFAULT_PORTS = {"http": 80, "https": 443}

_CHARSET_RE = re.compile(r';\s*charset\s*=\s*"?([^";]+)"?', re.IGNORECASE)
_MIME_PARAMS_RE = re.compile(r";.*$", re.DOTALL)


class FetchUrl:
    """归一后的已校验 HTTP(S) URL（上游 WHATWG ``URL`` 的 mini 替身）。

    字段语义对齐 WHATWG：``protocol`` 含冒号；``port`` 为字符串、默认端口
    省略为空串；``hostname`` 小写且不含 IPv6 方括号（mini 全链自洽，
    连接侧再补括号）；``origin`` = ``scheme://host[:port]``（跨源消息用）。
    ``href`` 含 fragment（结果展示），``request_url`` 不含（HTTP 不发 fragment）。
    """

    __slots__ = ("protocol", "hostname", "port", "username", "password", "path",
                 "query", "fragment", "href", "origin", "request_url")

    def __init__(self, protocol: str, hostname: str, port: str, username: str,
                 password: str, path: str, query: str, fragment: str):
        self.protocol = protocol
        self.hostname = hostname
        self.port = port
        self.username = username
        self.password = password
        self.path = path
        self.query = query
        self.fragment = fragment
        userinfo = f"{username}:{password}" if (username or password) else ""
        authority = f"{userinfo}@" if userinfo else ""
        host_display = f"[{hostname}]" if ":" in hostname else hostname
        port_display = f":{port}" if port else ""
        self.origin = f"{protocol}//{host_display}{port_display}"
        target = path or "/"
        if query:
            target += f"?{query}"
        self.href = f"{self.origin}{target}" + (f"#{fragment}" if fragment else "")
        self.request_url = f"{self.origin}{target}"

    def __repr__(self) -> str:  # pragma: no cover - 诊断辅助
        return f"FetchUrl({self.href!r})"


def parse_fetch_url(input: str) -> FetchUrl:
    """解析请求 URL 并执行网络无关的传输限制：仅 HTTP(S)、禁内嵌凭据。

    provider 在解析目的地址前调用（policy.ts:25）。
    """
    try:
        parts = urlsplit(input)
        scheme = parts.scheme.lower()
        hostname = parts.hostname  # 触发 host/IPv6 校验，非法即 ValueError
        port = parts.port
        username = parts.username or ""
        password = parts.password or ""
    except ValueError as error:
        raise WebError(f"invalid URL: {input}", "WEB_INVALID_URL") from error
    if not scheme or hostname is None or hostname == "":
        # WHATWG 对相对 URL / 空 host 抛 parse error → 同一消息。
        raise WebError(f"invalid URL: {input}", "WEB_INVALID_URL")
    if scheme not in ("http", "https"):
        raise WebError(
            f'unsupported URL scheme "{scheme}:" (only http and https are allowed)',
            "WEB_INVALID_URL",
        )
    if username or password:
        raise WebError("credentials in URLs are not allowed", "WEB_BLOCKED_URL")
    if port is not None and not 0 <= port <= 65535:
        # WHATWG 端口范围 0..65535，越界即 parse error。
        raise WebError(f"invalid URL: {input}", "WEB_INVALID_URL")
    normalized_port = "" if port is None or port == _DEFAULT_PORTS[scheme] else str(port)
    return FetchUrl(
        protocol=f"{scheme}:",
        hostname=hostname.lower(),
        port=normalized_port,
        username=username,
        password=password,
        path=parts.path,
        query=parts.query,
        fragment=parts.fragment,
    )


def validate_fetch_url(input: str) -> FetchUrl:
    """完整前置策略：长度界 + :func:`parse_fetch_url`；地址解析与钉桩在其后。"""
    if len(input) > WEB_FETCH_MAX_URL_LENGTH:
        raise WebError(
            f"URL exceeds the maximum length of {WEB_FETCH_MAX_URL_LENGTH}",
            "WEB_INVALID_URL",
        )
    return parse_fetch_url(input)


def is_same_origin(a: FetchUrl, b: FetchUrl) -> bool:
    """scheme、hostname、port 全等才同源；跨源重定向拒绝（policy.ts:65）。"""
    return a.protocol == b.protocol and a.hostname == b.hostname and a.port == b.port


def classify_content_type(content_type: str | None) -> str | None:
    """把响应 ``Content-Type`` 归入可解码 kind（'html'/'text'），不支持返回 None。"""
    mime = _MIME_PARAMS_RE.sub("", content_type or "").strip().lower()
    if mime in ("text/html", "application/xhtml+xml"):
        return "html"
    if mime.startswith("text/"):
        return "text"
    if mime in ("application/json", "application/xml") or mime.endswith("+json") or mime.endswith("+xml"):
        return "text"
    return None


def parse_charset(content_type: str | None) -> str | None:
    """抽取 ``charset`` 参数并小写；缺省返回 None（policy.ts:96）。"""
    match = _CHARSET_RE.search(content_type or "")
    return match.group(1).strip().lower() if match else None


def decoder_for_charset(charset: str | None):
    """返回声明编码的解码器；未声明默认 UTF-8，标签不可识别时 fail loud。

    返回 ``(codec_name, errors)`` 二元组，读取侧以 ``bytes.decode`` 消费
    （上游 TextDecoder 同为不抛错替换模式，非法序列 → U+FFFD）。
    """
    if charset is None:
        return "utf-8"
    try:
        codecs.lookup(charset)
    except LookupError as error:
        raise WebError(f'unsupported charset "{charset}"', "WEB_UNSUPPORTED_CONTENT_TYPE") from error
    return charset
