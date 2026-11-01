from __future__ import annotations


class SimplePolicy:
    def __init__(
        self,
        version: str = "v1",
        paused: bool = False,
        eval_timeout_ms: int = 60_000,
        gate=None,
        revalidate=None,
        admits_inferred=None,
    ) -> None:
        self._version = version
        self._paused = paused
        self.evalTimeoutMs = eval_timeout_ms
        self._gate = gate
        self._revalidate = revalidate
        self._admits_inferred = admits_inferred

    @property
    def version(self) -> str:
        return self._version

    @property
    def paused(self) -> bool:
        return self._paused

    def set_version(self, value: str) -> None:
        self._version = value

    def set_paused(self, value: bool) -> None:
        self._paused = value

    def gate(self, trigger, thread, tickets):
        if self._gate:
            return self._gate(trigger, thread, tickets)
        if not trigger.get("human"):
            return {"admissible": False, "reason": "not human"}
        return {"admissible": True, "strength": "explicit", "basis": {"via": "listed"}}

    def revalidate(self, mandate) -> bool:
        if self._revalidate:
            return self._revalidate(mandate)
        return self._version != "revoked"

    def admitsInferred(self, rules: list[str]) -> bool:
        if self._admits_inferred:
            return self._admits_inferred(rules)
        return len(rules) > 0
