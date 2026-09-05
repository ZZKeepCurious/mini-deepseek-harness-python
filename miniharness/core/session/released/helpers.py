"""released 格式共享助手（上游 session-format/src/{json,error}.ts 的 mini 载体）。

语言层差异（登记 verified-diffs §2.24）：
  * Python int 无界 → safe integer 显式实现 `±(2**53-1)` 边界 + `-0` 拒绝
    （`math.copysign` 判别；`json.loads` 不产 -0，防线面向构造方）；
  * deep_equal **类型敏感**：`True != 1`、`1 != 1.0`（TS `===` 语义，防 cross-check
    假绿；bool 是 int 子类是最易踩的坑）；
  * lossless JSON 拒 NaN/Infinity（`json.dumps` 默认放行，须显式拒）；
  * 不移植 Object.freeze（构造时校验 + 只读约定替代）。
"""
from __future__ import annotations

import math
from typing import Any

__all__ = [
    "SAFE_INT_MAX",
    "SessionFormatError",
    "SessionFormatUnsupportedMigrationError",
    "count",
    "deep_equal",
    "exact_keys",
    "fail",
    "is_json_object",
    "lossless_json",
    "safe_integer",
    "snapshot_json",
    "unsupported",
]

#: JS `Number.MAX_SAFE_INTEGER`（dt/time/seq 累加的溢出边界）。
SAFE_INT_MAX = 2**53 - 1


def fail(message: str) -> SessionFormatError:
    return SessionFormatError(message)


def unsupported(message: str) -> SessionFormatUnsupportedMigrationError:
    return SessionFormatUnsupportedMigrationError(message)


def is_safe_int(value: Any) -> bool:
    """JS `Number.isSafeInteger`：整数、非 bool、界内、非 -0。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    if not -SAFE_INT_MAX - 1 < value < SAFE_INT_MAX + 1:
        return False
    if value == 0 and math.copysign(1.0, value) < 0:
        return False
    return True


def count(value: Any, label: str) -> int:
    """非负安全整数（上游 sessionFormatCount）。"""
    if not is_safe_int(value) or value < 0:
        raise fail(f"{label} must be a non-negative safe integer")
    return value


def safe_integer(value: Any, label: str) -> int:
    """安全整数（上游 sessionFormatSafeInteger）。"""
    if not is_safe_int(value):
        raise fail(f"{label} must be a safe integer")
    return value


def is_json_object(value: Any) -> bool:
    """非 null 非数组的对象（上游 isSessionFormatJsonObject）。"""
    return isinstance(value, dict)


def lossless_json(value: Any, label: str = "Session value") -> Any:
    """无损 JSON 值：拒 NaN/Infinity/非 JSON 标量容器（上游 snapshotSessionFormatJson
    的检查半边；冻结半边由只读约定替代）。"""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if not is_safe_int(value):
            raise fail(f"{label} is not lossless JSON")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise fail(f"{label} is not lossless JSON")
        return value
    if isinstance(value, (list, tuple)):
        return [lossless_json(member, label) for member in value]
    if isinstance(value, dict):
        return {str(key): lossless_json(member, label) for key, member in value.items()}
    raise fail(f"{label} is not lossless JSON")


def snapshot_json(value: Any, label: str = "Session value") -> Any:
    """detach + 校验（返回深拷贝；上游深冻结由只读约定替代）。"""
    return lossless_json(value, label)


def deep_equal(a: Any, b: Any) -> bool:
    """类型敏感 JSON 深比较（上游 deepEqualJson：`1 !== true`、`1 !== 1.0`）。"""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        # bool 是 int 子类，必须先于数字分支（TS true !== 1）
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return type(a) is type(b) and a == b
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(deep_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(deep_equal(a[k], b[k]) for k in a)
    return False


def exact_keys(value: Any, required: tuple[str, ...] | list[str], optional: tuple[str, ...] | list[str],
               label: str, member: str = "member") -> None:
    """键闭集断言。member 措辞随调用方方言：v0/v1 用 ``member``、v2 codec/validation
    用 ``field``（上游三套措辞并存，逐字保留）。"""
    if not is_json_object(value):
        raise fail(f"{label} must be a JSON object")
    allowed = set(required) | set(optional)
    for key in value:
        if key not in allowed:
            raise fail(f'{label} has unexpected {member} "{key}"')
    for key in required:
        if key not in value:
            raise fail(f'{label} lacks required {member} "{key}"')


class SessionFormatError(Exception):
    """不可恢复/不可无损迁移的 durable 制品失败（上游同名类）。"""


class SessionFormatUnsupportedMigrationError(SessionFormatError):
    """可读制品的 released 源策略无受支持迁移（上游同名子类）。"""
