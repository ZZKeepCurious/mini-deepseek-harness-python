"""监听地址分类（对齐 `packages/host/webserver/src/index.ts` 的 `isWildcardHost`/`isLoopbackHost`）。

纯 stdlib（`ipaddress`），零第三方依赖：`web/launcher` 与 `cli` 的 `--host` 校验共用。
按**解析后的地址值**分类（非文本），故 `0.0.0.0`/`::`/`::ffff:0.0.0.0` 等一切等价拼写
（含 IPv4-mapped 与 zone 形式）都归为通配。
"""
from __future__ import annotations

import ipaddress
from typing import Any

__all__ = ["is_wildcard_host", "is_loopback_host"]


def _parse(host: str) -> Any | None:
    bare = host.split("%", 1)[0]
    try:
        return ipaddress.ip_address(bare)
    except ValueError:
        return None


def _is_unspecified(addr: Any) -> bool:
    if addr.is_unspecified:
        return True
    mapped = getattr(addr, "ipv4_mapped", None)
    return mapped is not None and mapped.is_unspecified


def is_wildcard_host(host: str) -> bool:
    """host 是否为通配（未指定）地址（上游 `isWildcardHost`）。

    非 IP 文本（主机名）→ False。`::ffff:0.0.0.0` 等 IPv4-mapped 形式按映射后的
    IPv4 判别。
    """
    addr = _parse(host)
    return addr is not None and _is_unspecified(addr)


def is_loopback_host(host: str) -> bool:
    """host 是否为回环地址（上游 `isLoopbackHost`）。

    仅按解析后地址判别（`127.0.0.1`、`::1` 及其映射形式）；主机名（如 `localhost`）
    不算回环——与上游「parse 后 range === 'loopback'」一致。
    """
    addr = _parse(host)
    if addr is None:
        return False
    if addr.is_loopback:
        return True
    mapped = getattr(addr, "ipv4_mapped", None)
    return mapped is not None and mapped.is_loopback