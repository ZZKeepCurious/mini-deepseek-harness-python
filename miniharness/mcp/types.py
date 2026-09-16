"""mcp-client 配置类型 + 重连策略解析。

对应 dsh 真实源码：packages/mcp/mcp-client/src/connection.ts（ReconnectConfig /
RECONNECT_DEFAULTS / resolveReconnectPolicy）与 index.ts（Config schema /
SERVER_NAME_PATTERN / DEFAULT_TOOL_CALL_TIMEOUT_MS）。上游用 Schemastery 在
loader 归一配置；mini 以显式解析函数承载同样的 fail-loud 校验（程序化构造
同样复检，对齐"misconfiguration fails loud at load"）。
"""
from __future__ import annotations

import re

__all__ = [
    "MAX_TIMER_DELAY_MS",
    "DEFAULT_TOOL_CALL_TIMEOUT_MS",
    "DEFAULT_MAX_INSTRUCTION_BYTES",
    "GENERATION_CLOSE_TIMEOUT_MS",
    "SERVER_NAME_PATTERN",
    "RECONNECT_DEFAULTS",
    "resolve_reconnect_policy",
    "resolve_mcp_config",
]

#: 单次定时器可容纳的最大 ms（上游 dsh-timeout MAX_TIMER_DELAY_MS）。
MAX_TIMER_DELAY_MS = 2_147_483_647

#: 单次 MCP 工具调用/资源请求默认超时（上游 index.ts:37）。
DEFAULT_TOOL_CALL_TIMEOUT_MS = 60_000

#: 归因 server instructions 的默认 UTF-8 字节上限（上游 connection.ts:49）。
DEFAULT_MAX_INSTRUCTION_BYTES = 32_768

#: 关闭一代连接的传输级确认屏障（上游 connection.ts:54，SDK stdio 传输持有
#: 两个两秒终止宽限，再留一秒给进程-关闭证据；超时 fail closed 防重叠子进程）。
GENERATION_CLOSE_TIMEOUT_MS = 5_000

#: serverName 合法区间，保持在公共工具名预算之下（上游 index.ts:40）。
SERVER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

#: 重连默认值（上游 connection.ts:41-46）。
RECONNECT_DEFAULTS = {
    "enabled": True,
    "initialDelayMs": 500,
    "maxDelayMs": 30_000,
    "maxAttempts": 10,
}


def resolve_reconnect_policy(config: dict | None, path: str) -> dict:
    """把原始 reconnect 配置解析为 supervisor 实际运行的冻结策略。

    @param config - 缺省用默认值；未知键 / 越界值在此 fail-loud 拒绝本实例。
    @param path - 诊断前缀（如 `mcp-client(foo): reconnect`）。
    @returns 冻结的完整策略 dict。
    """
    if config is not None:
        if not isinstance(config, dict):
            raise TypeError(f"{path} must be an object")
        for key in config:
            if key not in RECONNECT_DEFAULTS:
                raise KeyError(f"{path}.{key} is not a reconnect option")
    enabled = config.get("enabled", RECONNECT_DEFAULTS["enabled"]) if config else RECONNECT_DEFAULTS["enabled"]
    initial = config.get("initialDelayMs", RECONNECT_DEFAULTS["initialDelayMs"]) if config else RECONNECT_DEFAULTS["initialDelayMs"]
    maximum = config.get("maxDelayMs", RECONNECT_DEFAULTS["maxDelayMs"]) if config else RECONNECT_DEFAULTS["maxDelayMs"]
    attempts = config.get("maxAttempts", RECONNECT_DEFAULTS["maxAttempts"]) if config else RECONNECT_DEFAULTS["maxAttempts"]
    if not isinstance(initial, (int, float)) or isinstance(initial, bool) \
            or not (0 < initial <= MAX_TIMER_DELAY_MS):
        raise ValueError(
            f"{path}.initialDelayMs must be a positive finite number no greater than {MAX_TIMER_DELAY_MS}")
    if not isinstance(maximum, (int, float)) or isinstance(maximum, bool) \
            or not (0 < maximum <= MAX_TIMER_DELAY_MS):
        raise ValueError(
            f"{path}.maxDelayMs must be a positive finite number no greater than {MAX_TIMER_DELAY_MS}")
    if initial > MAX_TIMER_DELAY_MS or maximum > MAX_TIMER_DELAY_MS:
        raise ValueError(f"{path}: delay bounds exceed {MAX_TIMER_DELAY_MS}")
    if initial > maximum:
        raise ValueError(f"{path}.initialDelayMs must be less than or equal to maxDelayMs")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        raise ValueError(f"{path}.maxAttempts must be a positive integer")
    return {
        "enabled": bool(enabled),
        "initialDelayMs": int(initial),
        "maxDelayMs": int(maximum),
        "maxAttempts": int(attempts),
    }


def resolve_mcp_config(config: dict) -> dict:
    """归一 + 校验一份 mcp-client 插件配置（对齐上游 Config schema 默认值）。

    @param config - 原始配置（transport 必选；stdio：command 必选 /
      args/env/cwd 有默认；streamable-http：url 必选 / headers 默认 {}；
      toolCallTimeoutMs/failOnStartupError/reconnect 均有默认）。
    @returns 含全部默认值的规范化 dict。
    @raises ValueError/TypeError - 配置非法，fail-loud。
    """
    if not isinstance(config, dict):
        raise TypeError("mcp-client config must be an object")
    transport = config.get("transport")
    if transport not in ("stdio", "streamable-http"):
        raise ValueError('mcp-client config.transport must be "stdio" or "streamable-http"')
    server_name = config.get("serverName")
    if not isinstance(server_name, str) or not SERVER_NAME_PATTERN.match(server_name):
        raise ValueError("mcp-client config.serverName must match [A-Za-z0-9_-]{1,32}")
    timeout = config.get("toolCallTimeoutMs", DEFAULT_TOOL_CALL_TIMEOUT_MS)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("mcp-client config.toolCallTimeoutMs must be a positive number")
    max_instruction_bytes = config.get("maxInstructionBytes", DEFAULT_MAX_INSTRUCTION_BYTES)
    if not isinstance(max_instruction_bytes, int) or isinstance(max_instruction_bytes, bool) \
            or max_instruction_bytes < 1:
        raise ValueError("mcp-client config.maxInstructionBytes must be a positive integer")
    reconnect = resolve_reconnect_policy(
        config.get("reconnect"),
        f"mcp-client({server_name}): reconnect",
    ) if config.get("reconnect") is not None else dict(RECONNECT_DEFAULTS)
    out = {
        "transport": transport,
        "serverName": server_name,
        "toolCallTimeoutMs": timeout,
        "failOnStartupError": bool(config.get("failOnStartupError", False)),
        "maxInstructionBytes": max_instruction_bytes,
        "reconnect": reconnect,
    }
    if transport == "stdio":
        command = config.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError("mcp-client config.command must be a non-empty string")
        out.update({
            "command": command,
            "args": list(config.get("args", []) or []),
            "env": dict(config.get("env", {}) or {}),
            "cwd": config.get("cwd", "") or "",
        })
    else:
        url = config.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("mcp-client config.url must be a non-empty string")
        out.update({
            "url": url,
            "headers": dict(config.get("headers", {}) or {}),
        })
    return out