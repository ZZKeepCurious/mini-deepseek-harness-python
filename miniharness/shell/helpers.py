"""沙箱结算分类助手（上游 bash-sandbox/src/helpers.ts 移植）。

三路归因的判定面：runner 启动失败（基础设施错误，命令没跑）/
denial（被沙箱拦下）/ 普通非零退出（命令自身失败）。
"""

from __future__ import annotations

import errno
import os
import stat

# 已证实可标识「可执行解析/权限失败」的本地 spawn 错误码
# （上游 EXECUTABLE_SPAWN_CODES = EACCES/ENOENT）
EXECUTABLE_SPAWN_ERRNOS = frozenset({errno.EACCES, errno.ENOENT})


def _is_usable_workdir(path: str) -> bool:
    """调用方持有的 spawn cwd 可进入（目录且可执行检索）。"""
    try:
        if not stat.S_ISDIR(os.stat(path).st_mode):
            return False
        return os.access(path, os.X_OK)
    except OSError:
        return False


def is_runner_spawn_failure(error: BaseException, runner_program: str | None,
                            workdir: str) -> bool:
    """spawn 异常是否携带「runner 可执行自身」的证据。

    只归因 ENOENT/EACCES 且错误路径恰为 argv[0]（或无路径但 syscall 文本
    精确指向该程序）、并独立排除 cwd 不可用的情况。cwd 在归因时点检查、
    与 spawn 非原子；并发替换路径可能改变归因，但绝不可能放行未受限执行。
    """
    if runner_program is None or not _is_usable_workdir(workdir):
        return False
    filename = getattr(error, "filename", None)
    if filename is not None and os.path.abspath(filename) != os.path.abspath(runner_program):
        return False
    if not isinstance(error, OSError):
        return False
    return error.errno in EXECUTABLE_SPAWN_ERRNOS


def matches_signature(exit_code: int | None, stderr_text: str,
                      signatures) -> bool:
    """非零退出 + stderr 大小写不敏感子串命中（所选后端的 denial 方言）。"""
    if exit_code is None or exit_code == 0:
        return False
    lowered = stderr_text.lower()
    return any(signature.lower() in lowered for signature in signatures)


def classify_denial(result: dict, signatures) -> bool:
    """按所选后端 denial 方言归类一次失败的前台运行。"""
    return matches_signature(result.get("exitCode"), result.get("stderr", ""),
                             signatures)


def classify_runner_failure(exit_code: int | None, stderr_text: str,
                            rules: list[dict]) -> dict | None:
    """按结构化 runner 失败规则归类一次已结算进程。

    每条规则要求：非零退出 → 可选 allowedExitCodes 门 → 逐行排除精确的
    informational 行后 fatal 子串命中。返回首个命中的原始 fatal 行
    （{detail}，供基础设施错误 detail），证据不足返回 None。
    """
    if exit_code is None or exit_code == 0:
        return None
    lines = stderr_text.splitlines()
    for rule in rules:
        allowed = rule.get("allowedExitCodes")
        if allowed is not None and exit_code not in allowed:
            continue
        informational = {line.lower()
                         for line in rule.get("informationalLines") or []}
        # 空白子串不构成有意义的 runner 证据：过滤掉但保留其余有效签名
        fatal_signatures = [s.lower() for s in rule.get("fatalSignatures", [])
                            if s.strip()]
        for line in lines:
            lowered = line.lower()
            if lowered in informational:
                continue
            if any(signature in lowered for signature in fatal_signatures):
                return {"detail": line}
    return None
