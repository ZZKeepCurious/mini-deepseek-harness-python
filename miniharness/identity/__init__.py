"""Per-harness-home 匿名用户 id（遥测与反馈关联）。

对照上游：packages/identity/anonymous-user-id/src/index.ts（`getOrCreateAnonymousUserId`
+ `.anonymous-user-id` 文件）。语义：

  * id 为随机 UUID v4，以**裸 UUID 行**持久化在 `$DSH_HOME/.anonymous-user-id`
    （无包装格式）；绝不从主机名/网络地址/git remote 等衍化。
  * 作用域是 harness home 而非机器：共享同一 `$DSH_HOME` 的进程报告同一 id；
    删除文件下次启动重新铸新 id。
  * 同步读写（boot/CLI 可单 API 使用），按解析后的文件路径进程内 memo：
    一个进程只碰一次磁盘，运行中文件被删也不影响当前进程 id。
  * 并发首启由独占创建（wx 语义）settle：败者重读胜者 id。
  * 持久化 best-effort：home 只读时仍返回可用 id（反馈/遥测不被阻塞）。
"""
from __future__ import annotations

import os
import re
import uuid as _uuid
from typing import Callable, Optional

from ..core.home_paths import resolve_dsh_home

__all__ = ["ANONYMOUS_USER_ID_FILE_NAME", "AnonymousUserId", "get_or_create_anonymous_user_id"]

#: harness home 内存储 id 的文件名：裸 UUID 行，无包装格式。
ANONYMOUS_USER_ID_FILE_NAME = ".anonymous-user-id"

_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

#: 按解析后的文件路径键控的进程生命周期 memo（不同测试 home 决不复用 id）。
_memo: dict[str, str] = {}


class AnonymousUserId(str):
    """harness-home 作用域的匿名用户 id（随机 UUID v4）。"""


def _read_persisted_id(file: str) -> Optional[str]:
    """读文件中的合法持久化 id；缺失/不可读/非法返回 None。

    对齐上游 `readPersistedId`：`trim()` 后再匹配 UUID 正规式。
    """
    try:
        with open(file, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    value = text.strip()
    return value if _UUID_PATTERN.match(value) else None


def get_or_create_anonymous_user_id(
    env: Optional[dict] = None, random_uuid: Optional[Callable[[], str]] = None
) -> AnonymousUserId:
    """返回 harness home 的匿名用户 id；首次使用时创建并持久化。

    @param env: 读取 `DSH_HOME` 的环境（测试钩子）；缺省 os.environ。
    @param random_uuid: UUID 生成器（测试钩子）；缺省 uuid.uuid4。
    @returns: 该 harness home 的稳定匿名用户 id。

    对齐上游 `getOrCreateAnonymousUserId`：先 memo，再读盘，再独占创建
    （wx 失败 → 重读胜者 → 无效则覆盖写），持久化失败 best-effort 返回内存 id。
    """
    file = os.path.join(resolve_dsh_home(env=env), ANONYMOUS_USER_ID_FILE_NAME)
    cached = _memo.get(file)
    if cached is not None:
        return AnonymousUserId(cached)

    persisted = _read_persisted_id(file)
    if persisted is not None:
        _memo[file] = persisted
        return AnonymousUserId(persisted)

    generate = random_uuid or (lambda: str(_uuid.uuid4()))
    created = generate()
    try:
        os.makedirs(os.path.dirname(file), exist_ok=True)
        with open(file, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{created}\n")
        _memo[file] = created
        return AnonymousUserId(created)
    except FileExistsError:
        # 独占创建拒绝（对齐上游 wx）：并发胜者已先写入或既有损坏文件。重读
        # 采用合法胜者；非法重读落入覆盖写路径。
        reread = _read_persisted_id(file)
        if reread is not None:
            _memo[file] = reread
            return AnonymousUserId(reread)
        try:
            with open(file, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(f"{created}\n")
        except OSError:
            # 覆盖写也失败：保留内存 id，本次运行仍用一致 id。
            pass
        _memo[file] = created
        return AnonymousUserId(created)
    except OSError:
        # 只读 home / 其它非 EEXIST 失败：best-effort，返回内存 id。
        _memo[file] = created
        return AnonymousUserId(created)