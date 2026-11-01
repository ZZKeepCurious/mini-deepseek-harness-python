MEMBER = {"source": "chat", "id": "alice"}
GUEST = {"source": "chat", "id": "guest"}


def trigger(id: str, **overrides):
    base = {
        "id": id,
        "source": "chat",
        "threads": [{"source": "chat", "id": "t1"}],
        "actor": MEMBER,
        "human": True,
        "at": f"2026-09-23T00:00:{id.zfill(2)}Z",
        "text": f"message {id}",
        "channel": {"source": "chat", "thread": "t1", "address": {"message": id}},
        "meta": {},
    }
    base.update(overrides)
    return base
