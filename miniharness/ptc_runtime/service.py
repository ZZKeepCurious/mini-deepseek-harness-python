"""PTC 执行能力 seam 的 Service Definition。

对应 dsh 真实源码：packages/ptc-runtime/ptc-runtime/src/index.ts。

Seam 只描述「对宿主异步绑定运行一段模型写的程序」；运行时对工具/会话一无所知——
那些关注点由 consumer 拥有。本模块含保留名常量 + 抽象 PtcRuntime。
"""
from __future__ import annotations

import re

from .types import PtcRunRequest, PtcRunResult, PtcRunSpec

__all__ = [
    "DUNDER_MEMBER",
    "PORTABLE_RESERVED_WORDS",
    "RESERVED_BINDING_GLOBALS",
    "RESERVED_ERROR_MEMBERS",
    "PtcRuntime",
    "is_portable_identifier",
    "validate_binding_namespaces",
]

#: 每个后端都拒绝的绑定全局名（某后端在程序命名空间拥有该槽位）。
#: console（Node 日志捕获）+ __dsh_main__/__builtins__/__name__（Python bootstrap
#: 与预置模块全局）+ __debug__（CPython 将裸 __debug__ 编译为常量 True 且拒绝赋值，
#: 注入的全局不可达）。共享一个集合以保证可移植承诺：一个后端有效的命名空间列表
#: 在所有后端都有效。
RESERVED_BINDING_GLOBALS = frozenset({
    "console", "__dsh_main__", "__builtins__", "__name__", "__debug__",
})

#: PtcBindingErrorClass.memberNameProperty 每个后端都拒绝的名（共享契约）。
RESERVED_ERROR_MEMBERS = frozenset({
    "name", "message", "stack",
    "args", "with_traceback", "add_note",
})

#: dunder 形（``__x__``，中间非空）：Python 的对象协议槽位，作为错误成员一律拒。
DUNDER_MEMBER = re.compile(r"^__.+__$")

#: 所有可移植目标语言（ECMAScript ∪ Python）的保留字，作为命名空间 global /
#: 错误类名一律拒。可移植标识符契约承诺「一个后端有效的命名空间列表在所有后端
#: 都有效」，逐语言检查会让 lambda 通过 TS 后端却挂掉 Python 后端。
PORTABLE_RESERVED_WORDS = frozenset({
    # ECMAScript 保留字与严格模式保留名。
    "await", "break", "case", "catch", "class", "const", "continue", "debugger",
    "default", "delete", "do", "else", "enum", "export", "extends", "false",
    "finally", "for", "function", "if", "import", "in", "instanceof", "new",
    "null", "return", "super", "switch", "this", "throw", "true", "try", "typeof",
    "var", "void", "while", "with", "yield", "let", "static", "implements",
    "interface", "package", "private", "protected", "public", "arguments", "eval",
    # Python 3.x 关键字与软关键字（'type' 与 '_' 为软关键字：实践合法名，为安全保留）。
    "False", "None", "True", "and", "as", "assert", "async", "def", "del", "elif",
    "except", "from", "global", "is", "lambda", "nonlocal", "not", "or", "pass",
    "raise", "match", "type", "_",
})

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_portable_identifier(name: str) -> bool:
    """名是否匹配语言可移植标识符子集且非任一语言保留字（上游规则）。"""
    return bool(_IDENTIFIER.match(name)) and name not in PORTABLE_RESERVED_WORDS


def validate_binding_namespaces(bindings: list) -> None:
    """校验绑定命名空间列表（seam 误用 → 拒绝；上游 contract 校验）。

    每个 namespace.global 必须可移植且非保留槽位；errorClass.name 同规则；
    errorClass.memberNameProperty 非空且不落在可移植排除集（含 dunder 形）。
    重复 global 拒绝（同一命名空间冲突）。
    """
    seen: set[str] = set()
    for namespace in bindings:
        global_name = namespace.global_
        if not is_portable_identifier(global_name):
            raise ValueError(
                f"ptc binding namespace global {global_name!r} is not a portable identifier")
        if global_name in RESERVED_BINDING_GLOBALS:
            raise ValueError(
                f"ptc binding namespace global {global_name!r} is reserved by a backend slot")
        if global_name in seen:
            raise ValueError(f"ptc binding namespace global {global_name!r} is declared twice")
        seen.add(global_name)
        error_class = namespace.errorClass
        if error_class is None:
            continue
        if not is_portable_identifier(error_class.name):
            raise ValueError(
                f"ptc binding error class name {error_class.name!r} is not a portable identifier")
        member = error_class.memberNameProperty
        if not member:
            raise ValueError("ptc binding error class memberNameProperty must be non-empty")
        if member in RESERVED_ERROR_MEMBERS or DUNDER_MEMBER.match(member):
            raise ValueError(
                f"ptc binding error class memberNameProperty {member!r} is reserved")


class PtcRuntime:
    """注册一个 ``ctx.ptcRuntime`` 实现的抽象 Service Definition（上游 PtcRuntime）。

    程序 / 预算 / 中止 / 基底失败都在 PtcRunResult 中 resolve；只有 Service
    Definition 契约误用才 reject。实现应桥接可结构化克隆的绑定、为每个声明的
    命名空间物化拒绝类、把程序当作敌意对等体、隔离多次运行，并在拆解时终止并
    等待在途运行。
    """

    def __init__(self) -> None:
        pass

    @property
    def service_key(self) -> str:
        return "ptcRuntime"

    @property
    def language(self) -> str:
        """run 期望 program 使用的源语言（小写标识符，信息性、非门控）。"""
        raise NotImplementedError

    @property
    def isolation(self) -> str:
        """执行基底（小写标识符，信息性描述符，非安全声明）。"""
        raise NotImplementedError

    @property
    def execution_instructions(self) -> str:
        """provider 拥有的程序使用指引（consumer 与源语言一同呈现）。"""
        return ""

    @property
    def sandbox_mode(self) -> str | None:
        """部署文件策略模式；无约束支持时为 None。"""
        return None

    @property
    def timeout(self) -> dict | None:
        """配置的数值时间默认与上限；不支持 per-call 覆盖时为 None。"""
        return None

    def resolve(self, request: PtcRunRequest) -> PtcRunSpec:
        """执行前解析支持的选项与 provider 默认（上游 resolve）。"""
        raise NotImplementedError

    async def run(self, spec: PtcRunSpec) -> PtcRunResult:
        """执行已解析输入；程序结局作为结果字段 resolve（上游 run）。"""
        raise NotImplementedError
