"""第 6 章：凭据多来源 —— 四层解析的 LocalCredentialProvider。

对应 dsh 真实源码：packages/credentials/credentials（Service Definition）
+ packages/credentials/credentials-local（LocalCredentialProvider）。

上游语义（已核实，credentials-local/src/index.ts 头注释 + resolve/
describe/set/unset/assertUnshadowed/assertOwnerOnly/parseCredentialsDocument）：
  * 层级按信任度：
      inherited process environment      (只读，胜出)
      > $DSH_HOME/.credentials.yaml      (provider 管理，可写)
      > <调用 cwd>/.env                   (只读回退，project)
      > $DSH_HOME/.env                    (只读回退，user)
  * 继承环境胜出：`DEEPSEEK_API_KEY=… dsh`、CI secret、容器 -e 是本次
    运行的显式意图，且进程内不可编辑，所以必须"可见只读"而非静默遮蔽写。
  * 管理文件层的写（set/unset）每次重读磁盘后只补丁自己的键——注释与
    未触碰条目的格式全部幸存；外部编辑热生效；重载整表替换，删掉的条目
    绝不在内存残留。
  * 文档只放凭据：严格 CredentialRef→字符串映射而非 dotenv 文件——一个
    Harness 拥有、绝不物化进环境的存储不能同时充当环境层，否则会按优先
    级遮蔽非密条目使其静默不可达。
  * resolve 层级顺序：env → file → project-env → user-env（都没有 →
    undefined）；describe 报告 {configured, source, writable}（只有继承
    环境不可写）；set/unset 在 env 层已提供该 ref 时拒绝（写了也会被
    遮蔽成无效果）。
  * 文档解析严格（不是跳过）：非映射根、非 POSIX 标识符的 key、非字符串
    值、空串值全部拒绝——"我存的键没效果"比报错更糟；重复键是解析错误。
   * 权限：POSIX 上组/其他位可读的凭据文档在读之前直接拒绝；创建与替换
     以 0600（目录 0700）落盘；Windows 无 mode 可查，检查跳过而非假装。
   * 写锁：每次写先取 `<file>.lock` 兄弟锁，读-改-写全程持锁（上游
     dsh-atomic-write `withFileLock` + credentials-local index.ts:384 同款）；
     竞争指数退避至 2000ms 期限，超时按上游措辞 fail loud。载体用
     `filelock` 库：OS 级锁随进程死亡自动释放，比上游 wx-file 协议少了
     "孤儿锁需人工清理"边（登记为库载体改进，verified-diffs §3.10）。

载体简化（须在文档标注）：上游文档是 YAML（yaml 包），mini 用 JSON
（stdlib），"严格映射 + 失败即拒"的语义不变；无文件 watch（外部编辑靠
写入路径的重读折叠生效）；.env 解析覆盖上游 launch-environment 的常见子集
（KEY=VALUE + # 注释 + 引号剥离）。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from ..boot.dotenv import _is_posix_identifier, parse_dotenv

CREDENTIALS_FILENAME = ".credentials.json"
DOTENV_FILENAME = ".env"

# POSIX 上"组/其他"可读位：凭据文档必须一个都没有（上游 GROUP_OTHER_BITS）
GROUP_OTHER_BITS = 0o077

#: 写锁等待期限（上游 atomic-write/src/index.ts:79 `LOCK_TIMEOUT_MS = 2_000`
#: ——协议健壮性不变量而非部署可调项；测试经 mock.patch 缩短）。
LOCK_TIMEOUT_SECONDS = 2.0


class CredentialWriteLocked(RuntimeError):
    """跨进程写锁在期限内未获得（上游超时措辞逐字）。"""



def resolve_dsh_home() -> str:
    """$DSH_HOME 或 ~/.dsh（上游 resolveDshHome 同语义）。"""
    return os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    """object_pairs_hook：重复键是解析错误（上游 YAML uniqueKeys:true，index.ts:160）。"""
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen[key] = value
    return seen


def parse_credentials_document(text: str, filename: str) -> dict[str, str]:
    """解析凭据文档：严格映射，任何坏条目整体拒绝（不是跳过）。

    与上游 parseCredentialsDocument 同语义；错误信息只带键名与位置，
    绝不引用值（值就是秘密）。重复键 fail-closed（上游 uniqueKeys:true）。
    """
    try:
        root = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"credentials-local: invalid document at {filename}: "
            f"JSON parse error at line {error.lineno}, column {error.colno}"
        ) from error
    except ValueError as error:  # 重复键
        raise ValueError(
            f"credentials-local: invalid document at {filename}: {error}"
        ) from error
    if not isinstance(root, dict):
        raise TypeError(f"credentials-local: {filename} must be a mapping of credential reference to value")
    entries: dict[str, str] = {}
    for key, value in root.items():
        if not _is_posix_identifier(key):
            raise ValueError(f"credentials-local: invalid credential reference {key!r} in {filename}")
        if not isinstance(value, str):
            raise TypeError(f'credentials-local: the value for "{key}" in {filename} must be a string')
        if len(value) == 0:
            raise ValueError(f'credentials-local: the value for "{key}" in {filename} is empty; remove the key instead')
        entries[key] = value
    return entries


def _assert_owner_only(filename: str, stat_fn: Any = os.stat) -> None:
    """POSIX 上拒绝组/其他可读的凭据文档；Windows 无 mode 可查，跳过。

    与上游 assertOwnerOnly 同语义：文件缺失不报错；其余 stat 失败上抛。
    """
    if os.name == "nt":
        return
    try:
        mode = stat_fn(filename).st_mode
    except FileNotFoundError:
        return
    if (mode & GROUP_OTHER_BITS) != 0:
        raise ValueError(
            f"credentials-local: {filename} is readable beyond its owner "
            f"(mode {oct(mode & 0o777)}); run chmod 600 {filename} before starting again"
        )


class LocalCredentialProvider:
    """四层凭据提供者：env > 管理文件 > project .env > user .env。

    对齐上游 LocalCredentialProvider 的 resolve/describe/set/unset 语义；
    mini 同步实现、无 watch（简化标注，见模块头）。
    """

    def __init__(self, filename: str | None = None,
                 dsh_home: str | None = None,
                 project_dir: str | None = None,
                 read_env: bool = True):
        self._filename = os.path.abspath(filename or os.path.join(dsh_home or resolve_dsh_home(), CREDENTIALS_FILENAME))
        self._project_dir = project_dir or os.getcwd()
        self._user_dotenv = os.path.join(dsh_home or resolve_dsh_home(), DOTENV_FILENAME)
        self._read_env = read_env
        self._values: dict[str, str] = {}
        self._text: str | None = None
        self._load_initial()

    # ---------- 四层 ----------

    def _inherited(self, ref: str) -> str | None:
        if not self._read_env:
            return None
        value = os.environ.get(ref)
        return value if value else None

    def _dotenv_fallback(self, ref: str) -> tuple[str, str] | None:
        """project .env 优先于 user .env（更具体的位次赢，同环境分层）。"""
        for source, path in (("project-env", os.path.join(self._project_dir, DOTENV_FILENAME)),
                             ("user-env", self._user_dotenv)):
            value = self._read_dotenv_file(path).get(ref)
            if value:
                return value, source
        return None

    def _read_dotenv_file(self, path: str) -> dict[str, str]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return parse_dotenv(handle.read())
        except OSError:
            return {}

    # ---------- 查询 ----------

    def resolve(self, key: str) -> tuple[str, str] | None:
        """按层序解析：env → file → project-env → user-env；都没有 → None。

        与上游 resolve 返回 {value, source} | undefined 同语义。
        """
        inherited = self._inherited(key)
        if inherited is not None:
            return inherited, "env"
        stored = self._values.get(key)
        if stored is not None:
            return stored, "file"
        fallback = self._dotenv_fallback(key)
        if fallback is not None:
            return fallback
        return None

    def describe(self, key: str) -> dict:
        """{configured, source, writable}：只有继承环境层不可写。"""
        inherited = self._inherited(key)
        if inherited is not None:
            return {"configured": True, "source": "env", "writable": False}
        stored = self._values.get(key)
        if stored is not None:
            return {"configured": True, "source": "file", "writable": True}
        fallback = self._dotenv_fallback(key)
        if fallback is not None:
            return {"configured": True, "source": fallback[1], "writable": True}
        return {"configured": False, "writable": True}

    # ---------- 写（provider 管理文件层） ----------

    def set(self, key: str, value: str) -> None:
        if len(value) == 0:
            raise ValueError(f'credentials-local: an empty value cannot be stored for "{key}"; use unset')
        self._write(key, value)

    def unset(self, key: str) -> None:
        self._write(key, None)

    def _write(self, key: str, value: str | None) -> None:
        self._assert_unshadowed(key, "set" if value is not None else "unset")
        # 锁的排他创建需要父目录存在（上游 credentials-local index.ts:381：
        # 先建 0700 目录，再取 <file>.lock 兄弟锁）
        directory = os.path.dirname(self._filename)
        os.makedirs(directory, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)
        lock_path = f"{self._filename}.lock"
        try:
            with FileLock(lock_path, timeout=LOCK_TIMEOUT_SECONDS):
                # 读-改-写全程持锁：先重读磁盘折叠外部编辑（含并发写入者
                # 与 watcher 防抖窗口内的外部编辑），再补丁自己的键（上游
                # reconcileFromDisk + renderDocument 在 withFileLock 下同款）
                self._reconcile_from_disk()
                existing = self._values.get(key)
                if value is None and existing is None:
                    return
                next_values = dict(self._values)
                if value is None:
                    del next_values[key]
                else:
                    next_values[key] = value
                next_text = json.dumps(next_values, ensure_ascii=False, indent=2) + "\n"
                self._atomic_write(next_text)
                self._text = next_text
                self._values = next_values
        except Timeout as error:
            raise CredentialWriteLocked(
                f"atomic-write: timed out waiting for the writer lock at {lock_path}"
            ) from error

    def _assert_unshadowed(self, key: str, verb: str) -> None:
        if self._inherited(key) is not None:
            raise ValueError(
                f'credentials-local: "{key}" is supplied read-only by the launching environment, '
                f"so {verb} would be shadowed; unset it in the shell you start dsh from instead"
            )

    def _reconcile_from_disk(self) -> None:
        _assert_owner_only(self._filename)
        try:
            with open(self._filename, "r", encoding="utf-8") as handle:
                text = handle.read()
        except FileNotFoundError:
            text = None
        if text == self._text or text is None:
            return
        self._values = parse_credentials_document(text, self._filename)
        self._text = text

    def _atomic_write(self, text: str) -> None:
        """临时文件 + 原子替换；POSIX 0600 落盘（Windows 无 mode 语义，跳过）。"""
        _assert_owner_only(self._filename)
        directory = os.path.dirname(self._filename)
        os.makedirs(directory, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)
        descriptor, tmp_path = tempfile.mkstemp(dir=directory, prefix=".credentials-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
            if os.name != "nt":
                os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._filename)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ---------- 启动 ----------

    def _load_initial(self) -> None:
        """启动读：缺文件 = 空存储；存在的坏文档绝不当"没存凭据"（fail loud）。"""
        _assert_owner_only(self._filename)
        try:
            with open(self._filename, "r", encoding="utf-8") as handle:
                text = handle.read()
        except FileNotFoundError:
            return
        self._values = parse_credentials_document(text, self._filename)
        self._text = text

    @property
    def values(self) -> dict[str, str]:
        return dict(self._values)

    @property
    def filename(self) -> str:
        return self._filename