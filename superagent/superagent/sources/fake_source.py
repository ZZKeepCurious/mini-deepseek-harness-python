from __future__ import annotations

FAKE_SOURCE_KIND = {
    "kind": "fake",
    "health": [],
    "scope": lambda input: {"listened": True, "observed": False},
}


def fake_trigger(id: str, text: str = "hello", thread: str = "t1") -> dict:
    return {
        "id": id,
        "source": "fake",
        "threads": [{"source": "fake", "id": thread}],
        "actor": {"source": "fake", "id": "user"},
        "human": True,
        "at": "2026-09-23T00:00:00Z",
        "text": text,
        "channel": {"source": "fake", "thread": thread, "address": {"message": id}},
        "meta": {},
    }
