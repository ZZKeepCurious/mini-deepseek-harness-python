import argparse
from datetime import datetime, timezone

from superagent.app.assessor_loop import deliver_and_decide
from superagent.app.runtime import Runtime
from superagent.core.engine import Engine
from superagent.core.memory import MemoryRepositories
from superagent.core.part import EngineOptions
from superagent.executor.fake import FakeExecutor
from superagent.infra.policy import SimplePolicy


def main() -> None:
    parser = argparse.ArgumentParser(description="Super-agent orchestration demo")
    parser.add_argument("--sqlite", metavar="PATH", help="Persist state to SQLite and use Runtime + Outbox effects")
    args = parser.parse_args()

    trig = {
        "id": "demo-1",
        "source": "chat",
        "threads": [{"source": "chat", "id": "t1"}],
        "actor": {"source": "chat", "id": "alice"},
        "human": True,
        "at": "2026-09-23T00:00:01Z",
        "text": "fix the bug",
        "channel": {"source": "chat", "thread": "t1", "address": {"message": "demo-1"}},
        "meta": {},
    }

    if args.sqlite:
        rt = Runtime(db_path=args.sqlite, policy=SimplePolicy())
        print("receive:", rt.passes.receive(trig))
        rt.passes.pump()
        deliver_and_decide(
            rt.engine,
            rt.assessor.requests,
            "demo-1",
            {
                "kind": "accept",
                "title": "Fix",
                "scope": "small",
                "reason": "requested",
                "acknowledgement": "ok",
            },
        )
        rt.effects.idle("launch")
        rt.effects.idle("reply")
        tickets = rt.repo.tickets.list()
        print("tickets:", len(tickets), tickets[0]["title"] if tickets else None)
        print("outbox sent:", len(rt.sent_messages))
        rt.store.close()
        return

    repo = MemoryRepositories()
    assessor_requests = []

    class Assessor:
        def request(self, candidate):
            assessor_requests.append(candidate)

        def release(self, eval_id):
            pass

    outbox_messages = []

    class Outbox:
        def send(self, message):
            outbox_messages.append(message)

        def status(self, message_id):
            return "pending" if any(m["id"] == message_id for m in outbox_messages) else None

        def withdraw(self, message_id):
            pass

    class Scope:
        def scope(self, trigger):
            return {"listened": True, "observed": False}

    policy = SimplePolicy()
    engine = Engine(
        EngineOptions(
            repo=repo,
            scope=Scope(),
            policy=lambda: policy,
            assessor=Assessor(),
            executor=FakeExecutor(),
            outbox=Outbox(),
            now=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
    )
    print("receive:", engine.receive(trig))
    engine.evaluate_due()
    candidate = assessor_requests[-1]
    engine.decide(
        "demo-1",
        {
            "kind": "accept",
            "title": "Fix",
            "scope": "small",
            "reason": "requested",
            "acknowledgement": "ok",
            "evalId": candidate["evalId"],
        },
    )
    tickets = repo.tickets.list()
    print("tickets:", len(tickets), tickets[0]["title"] if tickets else None)
    print("outbox:", outbox_messages)


if __name__ == "__main__":
    main()
