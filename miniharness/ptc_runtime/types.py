"""PTC 执行 seam 的词汇类型：调用方交给 PtcRuntime 什么、拿回什么。

PTC = programmatic tool calls（程序化工具调用）：模型不逐一调用工具，而是写一段程序，
在程序内通过宿主机提供的异步绑定（如 ``await tools.add({...})``）完成多步操作
（上游更名记录：packages/.agents/notes/archived/architecture/2026-08-25-rename-code-mode-to-ptc.md）。

对应 dsh 真实源码：packages/ptc-runtime/ptc-runtime/src/types.ts（纯类型，无运行时代码）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "PtcBindingErrorClass",
    "PtcBindingFunction",
    "PtcBindingNamespace",
    "PtcJsonValue",
    "PtcRunFailure",
    "PtcRunRequest",
    "PtcRunResult",
    "PtcRunSandbox",
    "PtcRunSpec",
]

#: 可无损穿越轻量 Service Definition 的 JSON 值。
PtcJsonValue = Any

#: 暴露给程序的一个宿主侧异步可调用（args 与解析值必须无损 JSON）。
PtcBindingFunction = Callable[[Any], Any]


@dataclass(frozen=True)
class PtcBindingErrorClass:
    """一个绑定命名空间的程序可见类型化拒绝（上游 PtcBindingErrorClass）。

    @param name - 构造函数全局名与产物 Error.name；同一可移植标识符规则。
    @param memberNameProperty - 成员名的非空自有属性；可移植排除集 =
        RESERVED_ERROR_MEMBERS + dunder 形名（``__x__``，中间非空）。
    """

    name: str
    memberNameProperty: str


@dataclass(frozen=True)
class PtcBindingNamespace:
    """程序以一个全局对象看到的一组绑定函数（上游 PtcBindingNamespace）。

    @param global - 程序看到的全局标识符；须匹配语言可移植子集
        ``[A-Za-z_][A-Za-z0-9_]*`` 且非任一语言保留字，也不是后端自留槽位
        （RESERVED_BINDING_GLOBALS）。
    @param functions - 可调用成员，键为程序调用的精确名（须按 null-prototype
        语义对待 ``__proto__`` 等名，绝不作为原型碰撞）。
    @param errorClass - 可选的本命名空间程序可见类型化拒绝契约。
    """

    global_: str
    functions: dict[str, PtcBindingFunction] = field(default_factory=dict)
    errorClass: PtcBindingErrorClass | None = None

    def to_wire(self) -> dict:
        """序列化描述符（不含可调用对象本身）。"""
        return {
            "global": self.global_,
            "functions": sorted(self.functions.keys()),
            **({} if self.errorClass is None else {
                "errorClass": {"name": self.errorClass.name,
                               "memberNameProperty": self.errorClass.memberNameProperty}}),
        }


@dataclass(frozen=True)
class PtcRunRequest:
    """一次程序的调用方输入（上游 PtcRunRequest）。"""

    program: str
    bindings: list[PtcBindingNamespace] = field(default_factory=list)
    cwd: str | None = None
    timeoutMs: int | None = None
    sandboxPolicy: Any = None
    signal: Any = None


@dataclass(frozen=True)
class PtcRunSpec:
    """完整解析后的执行输入：run 绝不自行补缺目录或截止（上游 PtcRunSpec）。"""

    program: str
    bindings: list[PtcBindingNamespace]
    cwd: str
    timeoutMs: int | None
    sandboxPolicy: Any = None
    signal: Any = None


@dataclass(frozen=True)
class PtcRunSandbox:
    """施加到一次程序的文件限制，独立于终局（上游 PtcRunSandbox）。"""

    mode: str
    denied: bool
    enforcement: str | None = None


@dataclass(frozen=True)
class PtcRunFailure:
    """一次运行为何失败（kind 为正交结局，独立上报，上游 PtcRunFailure）。"""

    kind: str  # exception | timeout | abort | worker-exit | invalid-output | output-limit | protocol | sandbox-unavailable
    message: str


@dataclass
class PtcRunResult:
    """一次运行的结果：error 是 resolved 结果上的字段，绝不作为 run() 的拒绝。"""

    logs: list[str] = field(default_factory=list)
    value: Any = None
    has_value: bool = False
    error: PtcRunFailure | None = None
    sandbox: PtcRunSandbox | None = None
