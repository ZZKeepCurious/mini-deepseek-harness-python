"""公网地址解析与 NAT64 校验（上游 packages/web/web-fetch-http/src/network.ts）。

一次 DNS 解析的答案集先整体过公网策略，再交给钉桩连接；连接不再二次解析，
杜绝校验与连接之间的 DNS 重绑定。mini 载体差异：

  * 上游 ``ipaddr.js`` ``range()==='unicast'`` → Python ``ipaddress.is_global``
    但需显式排除 4 个 ipaddr 归 unicast 而 is_global 放行的段（已实测对照）：
    IPv4 组播 224/4、IPv6 组播 ff00::/8、NAT64 ``64:ff9b::/96``、
    SRv6 ``5f00::/16``（teredo/6to4/orchid/私网等其余段 is_global 已天然排除）。
  * 上游 ``raceWithSignal`` 订阅 abort 事件 → mini ``run_in_executor`` +
    ``asyncio.wait`` 轮询 ``signal.is_set()``；线程内 getaddrinfo 不可取消，
    与上游"in-flight OS lookup may finish unused"一致。
  * 代理面（proxyRouteFor/requestVia）不复现：mini 永远直连钉桩路径
    （verified-diffs 登记简化）。
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Sequence

from .types import WebError, is_set_signal

__all__ = [
    "IPV4ONLY_DISCOVERY_HOST",
    "IPV4ONLY_SENTINELS",
    "PublicAddress",
    "discover_nat64_prefixes",
    "is_non_public_ip_literal",
    "is_public_ip_address",
    "resolve_public_addresses",
    "strip_ipv6_brackets",
    "translated_ipv4_address",
]

#: RFC 6052 可承载 IPv4 目的的前缀长度。
RFC6052_PREFIX_LENGTHS = (32, 40, 48, 56, 64, 96)
#: RFC 7050 保留发现域名。
IPV4ONLY_DISCOVERY_HOST = "ipv4only.arpa"
#: RFC 7050 答案必须携带的哨兵地址。
IPV4ONLY_SENTINELS = frozenset({"192.0.0.170", "192.0.0.171"})


class PublicAddress:
    """一条已验证、供钉桩连接使用的地址（network.ts:17）。"""

    __slots__ = ("address", "family")

    def __init__(self, address: str, family: int):
        self.address = address
        self.family = family

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PublicAddress):
            return NotImplemented
        return self.address == other.address and self.family == other.family

    def __repr__(self) -> str:
        return f"PublicAddress({self.address!r}, {self.family})"


def strip_ipv6_brackets(hostname: str) -> str:
    """WHATWG hostname 带方括号，IP 解析器不带（network.ts:299）。"""
    if hostname.startswith("[") and hostname.endswith("]"):
        return hostname[1:-1]
    return hostname


def is_public_ip_address(input: str) -> bool:
    """是否为全局可达单播（ipaddr.js ``range()==='unicast'`` 的 Python 对照）。

    IPv4-mapped IPv6 按内嵌 IPv4 判定；过渡/翻译前缀保持拒绝——其最终 IPv4
    目的无法在此钉桩。
    """
    try:
        parsed = ipaddress.ip_address(strip_ipv6_brackets(input))
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv4Address):
        return parsed.is_global and not parsed.is_multicast
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        mapped = parsed.ipv4_mapped
        return mapped.is_global and not mapped.is_multicast
    return parsed.is_global and not parsed.is_multicast and not _is_translation_prefix(parsed)


def _is_translation_prefix(address: ipaddress.IPv6Address) -> bool:
    """ipaddr 归 unicast 而 Python is_global 放行的翻译/段前缀排除。"""
    packed = address.packed
    # NAT64 well-known prefix 64:ff9b::/96（ipaddr.js rfc6052 → reserved-ish）
    if packed[:12] == bytes.fromhex("0064ff9b") + b"\x00" * 8:
        return True
    # Segment Routing 5f00::/16（ipaddr.js segmentRouting）
    if packed[:2] == bytes.fromhex("5f00"):
        return True
    return False


async def _resolve_hostname(hostname: str) -> list[tuple[str, int]]:
    """一次系统解析（线程内执行）；返回 [(address, family), ...]。"""
    loop = asyncio.get_running_loop()
    infos = await loop.run_in_executor(
        None,
        lambda: socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM),
    )
    seen: list[tuple[str, int]] = []
    for family, _type, _proto, _canon, sockaddr in infos:
        if family == socket.AF_INET:
            normalized = 4
        elif family == socket.AF_INET6:
            normalized = 6
        else:
            continue
        address = sockaddr[0]
        if (address, normalized) not in seen:
            seen.append((address, normalized))
    return seen


async def _race_resolution(hostname: str, signal) -> list[tuple[str, int]]:
    """解析并允许在等待期间被信号取消（对齐 raceWithSignal）。"""
    if is_set_signal(signal):
        raise WebError("web fetch aborted", "WEB_ABORTED")
    task = asyncio.ensure_future(_resolve_hostname(hostname))
    while not task.done():
        if is_set_signal(signal):
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - 弃用答案排干，避免未取回异常
                pass
            # 上游 raceWithSignal 的中间文案最终也经 translate 归并为
            # 'web fetch aborted'，此处直接产出最终模型可见消息。
            raise WebError("web fetch aborted", "WEB_ABORTED")
        await asyncio.sleep(0.01)
    return task.result()


async def resolve_public_addresses(
    hostname: str,
    signal=None,
    resolver=None,
) -> list[PublicAddress]:
    """解析 hostname 一次；答案集中任一地址非整体公网即拒绝（network.ts:75）。

    ``resolver`` 仅测试覆写，签名 ``hostname -> [(address, family), ...]``。
    """
    unbracketed = strip_ipv6_brackets(hostname)
    literal_family = _literal_family(unbracketed)
    if resolver is not None:
        resolved = await resolver(unbracketed)
    elif literal_family == 0:
        resolved = await _race_resolution(unbracketed, signal)
    else:
        resolved = [(unbracketed, literal_family)]

    if not resolved:
        raise WebError(f'hostname "{hostname}" resolved to no addresses', "WEB_PROVIDER_ERROR")

    has_ipv6 = any(family == 6 for _address, family in resolved)
    nat64_prefixes = await discover_nat64_prefixes(signal, resolver) if has_ipv6 else []

    addresses: list[PublicAddress] = []
    for address, family in resolved:
        if family not in (4, 6) or _literal_family(address) != family:
            raise WebError(
                f'hostname "{hostname}" resolved to an invalid IP address',
                "WEB_PROVIDER_ERROR",
            )
        if not is_public_ip_address(address):
            raise WebError(
                f'URL hostname "{hostname}" resolves to a non-public IP address',
                "WEB_BLOCKED_URL",
            )
        translated = translated_ipv4_address(address, nat64_prefixes)
        if translated is not None and not is_public_ip_address(translated):
            raise WebError(
                f'URL hostname "{hostname}" resolves through NAT64 to a non-public IPv4 address',
                "WEB_BLOCKED_URL",
            )
        addresses.append(PublicAddress(address, family))
    return addresses


async def discover_nat64_prefixes(signal=None, resolver=None) -> list[tuple[bytes, int]]:
    """按 RFC 7050 发现在用 DNS64 前缀（network.ts:113）；仅取 family-6 答案。"""
    if resolver is None:
        discovered = await _race_resolution(IPV4ONLY_DISCOVERY_HOST, signal)
    else:
        discovered = await resolver(IPV4ONLY_DISCOVERY_HOST)
    prefixes: list[tuple[bytes, int]] = []
    seen: set[str] = set()
    for address, family in discovered:
        if family != 6 or _literal_family(address) != 6:
            continue
        raw = ipaddress.ip_address(address).packed
        for length in RFC6052_PREFIX_LENGTHS:
            embedded = _embedded_ipv4_address(raw, length)
            if embedded is None or embedded not in IPV4ONLY_SENTINELS:
                continue
            prefix_bytes = raw[: length // 8]
            key = f"{length}:{prefix_bytes.hex()}"
            if key in seen:
                continue
            seen.add(key)
            prefixes.append((prefix_bytes, length))
    return prefixes


def translated_ipv4_address(address: str, prefixes: Sequence[tuple[bytes, int]]) -> str | None:
    """地址匹配任一已发现前缀时返回 RFC 6052 内嵌 IPv4，否则 None（network.ts:137）。"""
    if _literal_family(address) != 6:
        return None
    raw = ipaddress.ip_address(strip_ipv6_brackets(address)).packed
    for prefix_bytes, length in prefixes:
        if raw[: len(prefix_bytes)] != prefix_bytes:
            continue
        embedded = _embedded_ipv4_address(raw, length)
        if embedded is not None:
            return embedded
    return None


def _embedded_ipv4_address(raw: bytes, prefix_length: int) -> str | None:
    """从 RFC 6052 IPv6 布局取内嵌 IPv4（network.ts:149）。"""
    if prefix_length == 96:
        return ".".join(str(b) for b in raw[12:16])
    prefix_bytes = prefix_length // 8
    if raw[8] != 0:
        return None
    before = 8 - prefix_bytes
    if before < 0:
        return None
    octets = list(raw[prefix_bytes : prefix_bytes + before]) + list(raw[9 : 9 + 4 - before])
    if len(octets) != 4:
        return None
    return ".".join(str(b) for b in octets)


def is_non_public_ip_literal(hostname: str) -> bool:
    """hostname 是否为 {@link resolve_public_addresses} 会拒绝的字面地址（network.ts:171）。"""
    unbracketed = strip_ipv6_brackets(hostname)
    return _literal_family(unbracketed) != 0 and not is_public_ip_address(unbracketed)


def _literal_family(address: str) -> int:
    try:
        return 6 if ipaddress.ip_address(address).version == 6 else 4
    except ValueError:
        return 0
