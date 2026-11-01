from __future__ import annotations

from superagent.app.effects import Effects
from superagent.app.outbox import InMemoryOutbox, Outbox
from superagent.app.passes import Passes
from superagent.app.store import Store
from superagent.core.engine import Engine
from superagent.core.memory import MemoryRepositories
from superagent.core.part import EngineOptions
from superagent.executor.queued import LaunchDeliveryExecutor
from superagent.infra.policy import SimplePolicy
from superagent.sources.registry import SourceRegistry


class FakeAssessor:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.released: list[str] = []

    def request(self, candidate: dict) -> None:
        self.requests.append(candidate)

    def release(self, eval_id: str) -> None:
        self.released.append(eval_id)


class Runtime:
    def __init__(
        self,
        db_path: str | None = None,
        policy: SimplePolicy | None = None,
        executor=None,
        assessor=None,
    ) -> None:
        self.closing_flag = False
        self.store = Store(db_path) if db_path else None
        self.repo = self.store if self.store else MemoryRepositories()
        self.registry = SourceRegistry()
        self.sent_messages: list[dict] = []
        self.registry._send_impl = self._send_to_source
        self.effects = Effects(store=self.store)
        self.assessor = assessor or FakeAssessor()
        self._policy = policy or SimplePolicy()
        self.executor = executor or LaunchDeliveryExecutor(self)
        self.outbox_port = InMemoryOutbox()
        self.engine = Engine(
            EngineOptions(
                repo=self.repo,
                policy=lambda: self._policy,
                scope=self.registry,
                assessor=self.assessor,
                executor=self.executor,
                outbox=self.outbox_port,
            )
        )
        self.passes = Passes(self)
        if self.store:
            self.outbox = Outbox(self)
            self.engine.options.outbox = self.outbox
        else:
            self.outbox = self.outbox_port
        self.effects.start()

    def _send_to_source(self, receipt: dict, commands=None) -> str:
        self.sent_messages.append(dict(receipt))
        if self.store:
            self.store.receipts.mark_sent(receipt["id"])
        return "sent"

    def configured(self) -> bool:
        return True

    def closing(self) -> bool:
        return self.closing_flag

    def set_policy(self, policy: SimplePolicy) -> None:
        self._policy = policy

    @property
    def sources(self):
        return self.registry
