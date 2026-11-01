from __future__ import annotations

from typing import Any, Literal, Protocol, TypedDict, runtime_checkable


GateResult = (
    dict[str, Any]  # admissible false: reason
    | dict[str, Any]  # admissible true: strength, basis
)

ScopeResult = dict[str, Any]


@runtime_checkable
class SourceScope(Protocol):
    def scope(self, input: dict[str, Any]) -> ScopeResult: ...


@runtime_checkable
class Policy(Protocol):
    version: str
    paused: bool
    evalTimeoutMs: int

    def gate(self, trigger: dict[str, Any], thread: dict[str, Any], tickets: list[dict[str, Any]]) -> dict[str, Any]: ...
    def revalidate(self, mandate: dict[str, Any]) -> bool: ...
    def admitsInferred(self, rules: list[str]) -> bool: ...


class Candidate(TypedDict, total=False):
    evalId: str
    thread: dict[str, Any]
    triggers: list[dict[str, Any]]
    strength: str
    policyVersion: str
    tickets: list[dict[str, Any]]


AssessmentOutcome = dict[str, Any]


@runtime_checkable
class Assessor(Protocol):
    def request(self, candidate: Candidate) -> None: ...
    def release(self, eval_id: str) -> None: ...


@runtime_checkable
class Executor(Protocol):
    def start(self, ticket: dict[str, Any], assignment: dict[str, Any]) -> None: ...
    def deliver(self, ticket: dict[str, Any], assignment: dict[str, Any]) -> None: ...
    def stop(self, ticket: dict[str, Any], assignment: dict[str, Any], reason: str) -> None: ...


class OutboundMessage(TypedDict, total=False):
    id: str
    channel: dict[str, Any]
    text: str
    kind: Literal["acknowledgement", "reply", "notice"]
    ticketId: str


ReplyStatus = Literal["pending", "sent", "dropped"]


@runtime_checkable
class Outbox(Protocol):
    def send(self, message: OutboundMessage) -> None: ...
    def status(self, id: str) -> ReplyStatus | None: ...
    def withdraw(self, id: str) -> None: ...


@runtime_checkable
class Repositories(Protocol):
    def atomic(self, work): ...

    triggers: Any
    threads: Any
    tickets: Any
    decisions: Any
