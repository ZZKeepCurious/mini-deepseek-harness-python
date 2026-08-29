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
* 文档布局 version 1（rc.2 起）：`{version: 1, refs: {...}, records: {...}}`
     ——refs 是 CredentialRef→非空字符串；records 是 `<scope>/<id>`→带标签
    记录（api-key {key?, env?} / grant {payload}）。空文档（或纯注释）=
    空存储，无需 version；非空无 version = pre-release flat 布局 → 拒读并指
    路（"Add `version: 1` and nest the existing N entries under `refs:`.
    No values need to change."），但**可识别的 flat 文档在启动时自动迁移**
    （renderFlatLayoutMigration：持锁重读后原值换布局落盘——老构建存的键
    必须活过布局变更，不需手工编辑）；version 不符 / 未知顶层键 fail-closed。
   * 记录键语法（上游 credentials/src/index.ts:18-21）：
     `KEY_SEGMENT_PATTERN = /^[a-z][a-z0-9-]*$/` 两段各校验（小写连字符标识
     符，与 CredentialRef 的 POSIX 语法不相交、永不碰撞）；`parseCredentialKey`
     要求恰两段（`must be "<scope>/<id>"`）。服务侧（P2-20，本模块新增）：
     `credential_key`/`parse_credential_key`/`is_credential_key_segment`/
     `credential_ref`/`is_credential_ref_name`/`credential_key_scope`/
     `credential_key_id` 助手，与上游同名导出同语义。
   * 记录服务 API（P2-20，对齐 credentials-local index.ts:652-726 +
     records.spec.ts）：`read_record`/`describe_record`/`list_records`/
     `modify_record`/`delete_record` 五件套。`modify_record`（含 delete）是
     唯一写路径：跨进程记录锁 + `reconcileFromDisk` 先重读，mutate 收当前
     记录、返回替换值或 None（None = 拒绝，不写不通知），先准入后渲染
     （grant→payload JSON 可往返；api-key→`assertStorableApiKey` 空 key /
     env 名 POSIX / env 值非空）——"读路径拒什么写路径就先拒什么，绝不让
     下一个启动拒绝这份文档"。备写经 `_render_document` 重渲染落盘、内存随
     后更新。描述语义：存在即事实（无 key 无 env 的 api-key 仍是配置过的
     store 决策，不是空白）；record 无分层遮蔽，writable 恒真。
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
     竞争指数退避至无人持锁时的 30 秒期限（上游 `DOCUMENT_LOCK_WAIT_MS =
     30_000`，index.ts:100-112——记录 mutate 的 owner 决策含网络往返，
     "文件工作的默认会为此期间杀掉每个竞争者"，故 refs/records/迁移所有
     写路径都用这期限而非 atomic-write 的 2s 默认），超时按上游措辞
     fail loud。载体用 `filelock` 库：OS 级锁随进程死亡自动释放，比上游
     wx-file 协议少了 "孤儿锁需人工清理"边（登记为库载体改进，
     verified-diffs §3.10）。

载体简化（须在文档标注）：上游文档是 YAML（yaml 包），mini 用 JSON
（stdlib），"严格映射 + 失败即拒"的语义不变；无文件 watch（外部编辑靠
写入路径的重读折叠生效）；.env 解析覆盖上游 launch-environment 的常见子集
（KEY=VALUE + # 注释 + 引号剥离）。modifyRecord/deleteRecord 为同步实现
（上游为 async + 操作队列 enqueue；mini 单进程同步，跨进程互斥由容器锁
守护，队列等价于天然顺序）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from ..boot.dotenv import _is_posix_identifier, parse_dotenv

CREDENTIALS_FILENAME = ".credentials.json"
DOTENV_FILENAME = ".env"

#: 文档布局版本（上游 credentials-local index.ts:167 DOCUMENT_VERSION = 1；
#: 序列化字段或 fold 语义变更时递增，旧版本拒读）
DOCUMENT_VERSION = 1

# POSIX 上"组/其他"可读位：凭据文档必须一个都没有（上游 GROUP_OTHER_BITS）
GROUP_OTHER_BITS = 0o077

#: 记录键（<scope>/<id>）每段的合法语法（上游 credentials/src/index.ts:21
#: KEY_SEGMENT_PATTERN = /^[a-z][a-z0-9-]*$/ ——小写连字符标识符）
KEY_SEGMENT_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")

#: 写锁等待期限（上游 credentials-local index.ts:112 DOCUMENT_LOCK_WAIT_MS =
#: 30_000；refs/records/迁移各写路径统一以它覆盖 atomic-write 的 2s 默认——
#: 记录 mutate 的 owner 决策含网络往返，见模块头写法说明。测试经 mock.patch
#: 缩短）。
DOCUMENT_LOCK_WAIT_SECONDS = 30.0


class CredentialWriteLocked(RuntimeError):
    """跨进程写锁在期限内未获得（上游超时措辞逐字）。"""


logger = logging.getLogger(__name__)



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


def _load_json_document(text: str, filename: str) -> Any:
    """严格 JSON 载体解析：重复键 fail-closed（上游 YAML uniqueKeys:true）；
    错误信息只带位置与键名，绝不引用值（值就是秘密）。"""
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"credentials-local: invalid document at {filename}: "
            f"JSON parse error at line {error.lineno}, column {error.colno}"
        ) from error
    except ValueError as error:  # 重复键
        raise ValueError(
            f"credentials-local: invalid document at {filename}: {error}"
        ) from error


def _parse_refs(section: Any, filename: str) -> dict[str, str]:
    """refs 段准入：POSIX 标识符键 over 非空字符串值（上游 parseRefs）。"""
    entries: dict[str, str] = {}
    for key, value in _as_section(section, "refs", filename).items():
        if not _is_posix_identifier(key):
            raise ValueError(f"credentials-local: invalid credential reference {key!r} in {filename}")
        if not isinstance(value, str):
            raise TypeError(f'credentials-local: the value for "{key}" in {filename} must be a string')
        if len(value) == 0:
            raise ValueError(f'credentials-local: the value for "{key}" in {filename} is empty; remove the key instead')
        entries[key] = value
    return entries


def _as_section(section: Any, name: str, filename: str) -> dict[str, Any]:
    """一段文档作为普通映射；缺省与 null 都表示空段（上游 asSection）。"""
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise TypeError(f'credentials-local: "{name}" in {filename} must be a mapping')
    return section


def _is_credential_key(key: str) -> bool:
    """严格两段准入：`<scope>/<id>`，每段 {@link KEY_SEGMENT_PATTERN}
    （上游 parseCredentialKey + credentialKey，credentials/src/index.ts:21,
    68-91：scope=拥有插件的注册名，与 CredentialRef 语法不相交，两个键空间
    永不碰撞；非小写连字符段/多于两段一律拒绝）。"""
    return isinstance(key, str) and _try_parse_credential_key(key) is not None


def _try_parse_credential_key(key: str) -> tuple[str, str] | None:
    """解析成功返回 (scope, id)，失败返回 None（供准入判断用）。"""
    if not isinstance(key, str):
        return None
    segments = key.split("/")
    if len(segments) != 2:
        return None
    scope, record_id = segments
    if not KEY_SEGMENT_PATTERN.match(scope) or not KEY_SEGMENT_PATTERN.match(record_id):
        return None
    return scope, record_id


def credential_key(scope: str, id: str) -> str:
    """品牌一个 `<scope>/<id>` 记录键（上游 credentialKey）。

    两个段都必须匹配 {@link KEY_SEGMENT_PATTERN}，否则 TypeError 逐字对齐
    上游文案。
    """
    for segment in (scope, id):
        if not KEY_SEGMENT_PATTERN.match(segment):
            raise TypeError(f'credential key segment "{segment}" must match /^[a-z][a-z0-9-]*$/')
    return f"{scope}/{id}"


def parse_credential_key(value: str) -> str:
    """品牌一个已储存的 `<scope>/<id>` 字符串。

    这是 credentialKey 的读半边：要求恰两段，每段合法；否则 TypeError 逐字
    对齐上游 parseCredentialKey（两个分支：非两段 → `must be "<scope>/<id>"`；
    段非法 → credentialKey 的段文案）。
    """
    segments = value.split("/")
    if len(segments) != 2:
        raise TypeError(f'credential key "{value}" must be "<scope>/<id>"')
    scope, record_id = segments
    return credential_key(scope, record_id)


def is_credential_key_segment(value: str) -> bool:
    """一个裸串能否作为记录键段（上游 isCredentialKeySegment）。"""
    return bool(KEY_SEGMENT_PATTERN.match(value))


def is_credential_ref_name(value: str) -> bool:
    """一个裸串能否作为引用名（上游 isCredentialRefName）。"""
    return _is_posix_identifier(value)


def credential_ref(value: str) -> str:
    """品牌一个 CredentialRef（上游 credentialRef）。"""
    if not is_credential_ref_name(value):
        raise TypeError(f'credential ref "{value}" must match /^[A-Za-z_][A-Za-z0-9_]*$/')
    return value


def credential_key_scope(key: str) -> str:
    """记录键的拥有插件段（上游 credentialKeyScope）。"""
    return _try_parse_credential_key(key)[0]


def credential_key_id(key: str) -> str:
    """记录键的 id 段（上游 credentialKeyId）。"""
    return _try_parse_credential_key(key)[1]


def _assert_record_fields(key: str, fields: dict[str, Any], allowed: set[str], filename: str) -> None:
    """拒绝标签未定义的字段——笔误不能被静默丢弃（上游 assertFields）。"""
    for field in fields:
        if field not in allowed:
            raise ValueError(f'credentials-local: record "{key}" in {filename} has unknown field "{field}"')


def _parse_record_env(key: str, env: Any, filename: str) -> dict[str, str] | None:
    """api-key 记录的 provider 环境：POSIX 名 over 非空字符串（上游 parseRecordEnv）。
    env 名经 credential_ref 逐字对齐上游（`credential ref "X" must match ...`）。"""
    if env is None:
        return None
    if not isinstance(env, dict):
        raise TypeError(f'credentials-local: record "{key}" in {filename} has a non-mapping env')
    parsed: dict[str, str] = {}
    for name, value in env.items():
        credential_ref(name)
        if not isinstance(value, str) or len(value) == 0:
            raise TypeError(f'credentials-local: record "{key}" env "{name}" in {filename} must be a non-empty string')
        parsed[name] = value
    return parsed


def _assert_json_value(label: str, value: Any, seen: set[int]) -> None:
    """payload 必须经得起 JSON 往返（上游 assertJsonValue）。"""
    if id(value) in seen:
        raise ValueError(f"credentials-local: {label} is cyclic")
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise ValueError(f"credentials-local: {label} is not finite")
    if isinstance(value, (int, float)):
        return
    if isinstance(value, list):
        seen = seen | {id(value)}
        for item in value:
            _assert_json_value(label, item, seen)
        return
    if isinstance(value, dict):
        seen = seen | {id(value)}
        for item_key, item in value.items():
            if not isinstance(item_key, str):
                raise ValueError(f"credentials-local: {label} has a non-string key")
            _assert_json_value(label, item, seen)
        return
    raise TypeError(f"credentials-local: {label} is not JSON")


def _assert_storable_api_key(key: str, record: dict[str, Any]) -> None:
    """写路径 api-key 准入：空/非字符串 key、非法 env 名、空 env 值都拒
    （上游 assertStorableApiKey，credentials-local index.ts:307-317）——否则
    会 persist 一份 parseRecord 在下次启动拒绝的文档。文案逐字对齐上游；
    mini 无 TS 类型挡非字符串，故非字符串 key/env 值也按读路径同等拒绝。
    """
    stored_key = record.get("key")
    if stored_key is not None and stored_key == "":
        raise TypeError(f'credentials-local: record "{key}" has an empty key; omit the field instead')
    if stored_key is not None and not isinstance(stored_key, str):
        raise TypeError(f'credentials-local: record "{key}" has a non-string key')
    for name, value in (record.get("env") or {}).items():
        credential_ref(name)
        if not isinstance(value, str) or value == "":
            raise TypeError(f'credentials-local: record "{key}" env "{name}" must be a non-empty string')


def _parse_record(key: str, value: Any, filename: str) -> dict[str, Any]:
    """单条记录准入：拒未知标签或字段而非丢弃（上游 parseRecord）。"""
    if not isinstance(value, dict):
        raise TypeError(f'credentials-local: record "{key}" in {filename} must be a mapping')
    kind = value.get("kind")
    if kind == "api-key":
        _assert_record_fields(key, value, {"kind", "key", "env"}, filename)
        api_key = value.get("key")
        if api_key is not None and (not isinstance(api_key, str) or len(api_key) == 0):
            raise TypeError(f'credentials-local: record "{key}" in {filename} has a non-string or empty key')
        env = _parse_record_env(key, value.get("env"), filename)
        record: dict[str, Any] = {"kind": "api-key"}
        if api_key is not None:
            record["key"] = api_key
        if env is not None:
            record["env"] = env
        return record
    if kind == "grant":
        _assert_record_fields(key, value, {"kind", "payload"}, filename)
        if "payload" not in value:
            raise ValueError(f'credentials-local: record "{key}" in {filename} has no payload')
        _assert_json_value(f'record "{key}" payload in {filename}', value["payload"], set())
        return {"kind": "grant", "payload": value["payload"]}
    if kind is None:
        raise ValueError(f'credentials-local: record "{key}" in {filename} has no kind')
    raise ValueError(
        f'credentials-local: record "{key}" in {filename} has unknown kind {json.dumps(kind)}'
    )


def _parse_records(section: Any, filename: str) -> dict[str, dict[str, Any]]:
    """records 段准入：`<scope>/<id>` 键 over 带标签映射（上游 parseRecords）。
    键经 parse_credential_key 严格语法（非两段/段非法逐字抛上游 TypeError，
    而非套本地前缀——上游即让 parseCredentialKey 的异常直接冒泡）。"""
    entries: dict[str, dict[str, Any]] = {}
    for key, value in _as_section(section, "records", filename).items():
        parse_credential_key(key)
        entries[key] = _parse_record(key, value, filename)
    return entries


def parse_credentials_document(text: str, filename: str) -> dict[str, dict]:
    """解析凭据文档为 version-1 布局：任何坏条目整体拒绝（不是跳过）。

    返回 {"refs": {...}, "records": {...}}。空文档（含空白）是空存储、无需
    version；非空无 version = pre-release flat 布局 → 拒读并指路；version
    不符 / 未知顶层键 fail-closed。错误信息只带键名与位置，绝不引用值。
    """
    root = _load_json_document(text, filename) if text.strip() else {}
    if not isinstance(root, dict):
        raise TypeError(f"credentials-local: {filename} must be a mapping")
    keys = list(root)
    # 空（或纯注释）文档就是空存储：后续布局不可能赋予它别的含义
    if len(keys) == 0:
        return {"refs": {}, "records": {}}
    if "version" not in root:
        raise ValueError(
            f"credentials-local: {filename} uses the pre-release flat layout. "
            f"Add `version: {DOCUMENT_VERSION}` and nest the existing "
            f"{len(keys)} {'entry' if len(keys) == 1 else 'entries'} under `refs:`. "
            "No values need to change."
        )
    if root["version"] != DOCUMENT_VERSION:
        raise ValueError(
            f"credentials-local: {filename} declares version {json.dumps(root['version'])};"
            f" this build reads version {DOCUMENT_VERSION}"
        )
    for key in keys:
        if key not in ("version", "refs", "records"):
            raise ValueError(f'credentials-local: unknown top-level key "{key}" in {filename}')
    return {"refs": _parse_refs(root.get("refs"), filename),
            "records": _parse_records(root.get("records"), filename)}


def render_flat_layout_migration(text: str) -> str | None:
    """pre-release flat 文档 → version-1 布局文本；不可识别 → None。

    识别条件（上游 renderFlatLayoutMigration 同款）：非空顶层映射、无
    version 键、每个键都是可寻址引用名、每个值都是非空字符串。任何本构建
    无法证明理解的文档都不改写（响亮拒绝继续成立）；可识别时值逐字保留、
    只换外围布局。
    """
    try:
        flat = _load_json_document(text, filename="migration")
    except (TypeError, ValueError):
        return None
    if not isinstance(flat, dict) or len(flat) == 0 or "version" in flat:
        return None
    for key, value in flat.items():
        if not _is_posix_identifier(key):
            return None
        if not isinstance(value, str) or len(value) == 0:
            return None
    migrated: dict[str, Any] = {"version": DOCUMENT_VERSION, "refs": flat}
    return json.dumps(migrated, ensure_ascii=False, indent=2) + "\n"


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

    对齐上游 LocalCredentialProvider 的 resolve/describe/set/unset 语义 +
    记录半边五件套（readRecord/describeRecord/listRecords/modifyRecord/
    deleteRecord，P2-20）；mini 同步实现、无 watch、无可发事件（notify 类
    上游经事件总线 fan-out，mini 无 credentials 事件总线故省略，见 §3.10
    B11①）。"""

    def __init__(self, filename: str | None = None,
                 dsh_home: str | None = None,
                 project_dir: str | None = None,
                 read_env: bool = True):
        self._filename = os.path.abspath(filename or os.path.join(dsh_home or resolve_dsh_home(), CREDENTIALS_FILENAME))
        self._project_dir = project_dir or os.getcwd()
        self._user_dotenv = os.path.join(dsh_home or resolve_dsh_home(), DOTENV_FILENAME)
        self._read_env = read_env
        self._values: dict[str, str] = {}
        # version-1 记录段：外部写入的合法记录按上游准入读取并原样保留（写
        # refs 时 records 不丢）；P2-20 起提供服务侧读写（record 五件套）
        self._records: dict[str, dict] = {}
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

    # ---------- 记录：读 ----------

    def read_record(self, key: str) -> dict | None:
        """读一条存储的记录，原样返回（上游 readRecord）。

        键经 parse_credential_key 严格语法拒绝非法地址；未存 → None。
        """
        parse_credential_key(key)
        return self._records.get(key)

    def describe_record(self, key: str) -> dict:
        """{configured, kind?, writable}：存在即事实（上游 describeRecord）。

        记录没有高于本文档的分层，writable 恒真；无 key 无 env 的 api-key
        记录是"确认走自身环境发现"的存储决策，不是空白——presence 即
        configured。kind 仅在已存储时出现。
        """
        parse_credential_key(key)
        stored = self._records.get(key)
        if stored is None:
            return {"configured": False, "writable": True}
        return {"configured": True, "kind": stored["kind"], "writable": True}

    def list_records(self) -> list[dict]:
        """枚举每条存储记录的地址与标签，绝不暴露值（上游 listRecords）。

        返回 [{key, kind}, ...]，键即 parse_credential_key 已证可寻址
        的 `<scope>/<id>`。
        """
        return [{"key": key, "kind": record["kind"]}
                for key, record in self._records.items()]

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
            with FileLock(lock_path, timeout=DOCUMENT_LOCK_WAIT_SECONDS):
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
                next_text = self._render_document(next_values)
                self._atomic_write(next_text)
                self._text = next_text
                self._values = next_values
        except Timeout as error:
            raise CredentialWriteLocked(
                f"atomic-write: timed out waiting for the writer lock at {lock_path}"
            ) from error

    def _render_document(self, refs: dict[str, str],
                         records: dict | None = None) -> str:
        """version-1 布局渲染：refs + records（无记录省略该段）。

        refs 写路径沿用既有键；记录写路径（modify/delete_record）传入下一
        时刻的记录表快照，避免先把内存改脏再做原子写。
        """
        document: dict[str, Any] = {"version": DOCUMENT_VERSION, "refs": refs}
        next_records = self._records if records is None else records
        if next_records:
            document["records"] = next_records
        return json.dumps(document, ensure_ascii=False, indent=2) + "\n"

    # ---------- 记录：写（modifyRecord 是唯一的记录写路径） ----------

    def modify_record(self, key: str, mutate) -> dict | None:
        """串行化读-改-写一条记录——唯一写路径（上游 modifyRecord）。

        mutate 收当前记录（未存 → None），返回替换记录或 None（None = 拒绝，
        不写不通知、返回现状）。键按上游 parseCredentialKey 拒绝非法语法。
        写前准入 = 读路径准入（grant→payload JSON 往返；api-key→
        assertStorableApiKey），先建 0700 目录再取 `<file>.lock` 兄弟锁，
        持锁内 reconcileFromDisk 折叠外部编辑——两个进程同时刷新一个令牌
        时谁先谁后，绝不互相覆盖。
        """
        parse_credential_key(key)
        directory = os.path.dirname(self._filename)
        os.makedirs(directory, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)
        lock_path = f"{self._filename}.lock"
        try:
            with FileLock(lock_path, timeout=DOCUMENT_LOCK_WAIT_SECONDS):
                self._reconcile_from_disk()
                current = self._records.get(key)
                next_record = mutate(current)
                if next_record is None:
                    return current
                if next_record.get("kind") == "grant":
                    _assert_json_value(f'record "{key}" payload', next_record["payload"], set())
                elif next_record.get("kind") == "api-key":
                    _assert_storable_api_key(key, next_record)
                else:
                    raise TypeError(f'credentials-local: record "{key}" cannot be stored: '
                                    f'unknown kind {json.dumps(next_record.get("kind"))}')
                next_records = dict(self._records)
                next_records[key] = next_record
                next_text = self._render_document(self._values, next_records)
                self._atomic_write(next_text)
                self._text = next_text
                self._records = next_records
                return next_record
        except Timeout as error:
            raise CredentialWriteLocked(
                f"atomic-write: timed out waiting for the writer lock at {lock_path}"
            ) from error

    def delete_record(self, key: str) -> None:
        """删除一条记录；删除不存在的记录是 no-op（上游 deleteRecord）。

        写纪律同 modifyRecord（持锁 + reconcile + 原子落盘 + 内存随后更新）。
        """
        parse_credential_key(key)
        directory = os.path.dirname(self._filename)
        os.makedirs(directory, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)
        lock_path = f"{self._filename}.lock"
        try:
            with FileLock(lock_path, timeout=DOCUMENT_LOCK_WAIT_SECONDS):
                self._reconcile_from_disk()
                if key not in self._records:
                    return
                next_records = dict(self._records)
                del next_records[key]
                next_text = self._render_document(self._values, next_records)
                self._atomic_write(next_text)
                self._text = next_text
                self._records = next_records
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
        document = parse_credentials_document(text, self._filename)
        self._values = document["refs"]
        self._records = document["records"]
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
        """启动读：缺文件 = 空存储；存在的坏文档绝不当"没存凭据"（fail loud）。

        可识别的 pre-release flat 文档先自动迁移（上游 migrateFlatDocument：
        持锁重读后换布局落盘，值逐字保留——并发启动竞速时落败方读到非
        flat 文档则原样交给普通解析）。"""
        _assert_owner_only(self._filename)
        try:
            with open(self._filename, "r", encoding="utf-8") as handle:
                text = handle.read()
        except FileNotFoundError:
            return
        if render_flat_layout_migration(text) is not None:
            text = self._migrate_flat_document(text)
        document = parse_credentials_document(text, self._filename)
        self._values = document["refs"]
        self._records = document["records"]
        self._text = text

    def _migrate_flat_document(self, recognized: str) -> str:
        """一次性升级可识别 flat 布局：写锁内重读磁盘再迁移（并发启动可能
        已迁移；重读结果不可识别则原样返回交普通解析）。"""
        directory = os.path.dirname(self._filename)
        os.makedirs(directory, exist_ok=True)
        lock_path = f"{self._filename}.lock"
        try:
            with FileLock(lock_path, timeout=DOCUMENT_LOCK_WAIT_SECONDS):
                with open(self._filename, "r", encoding="utf-8") as handle:
                    current = handle.read()
                migrated = render_flat_layout_migration(current)
                if migrated is None:
                    return current
                self._atomic_write(migrated)
                logger.info(
                    "credentials-local: migrated %s to the version %d layout; values are unchanged",
                    self._filename, DOCUMENT_VERSION,
                )
                return migrated
        except Timeout as error:
            raise CredentialWriteLocked(
                f"atomic-write: timed out waiting for the writer lock at {lock_path}"
            ) from error

    @property
    def values(self) -> dict[str, str]:
        return dict(self._values)

    @property
    def records(self) -> dict[str, dict]:
        """存储的记录表只读视图（上游 provider.records 开放字段）。"""
        return {key: dict(record) for key, record in self._records.items()}

    @property
    def filename(self) -> str:
        return self._filename