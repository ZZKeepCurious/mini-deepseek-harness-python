from __future__ import annotations


class SourceRegistry:
    def __init__(self, settings=None) -> None:
        self._kinds: dict[str, dict] = {}
        self._settings = settings or (lambda: {"sources": {}})

    def register(self, kind: dict):
        kind_id = kind["kind"]
        if kind_id in self._kinds:
            raise RuntimeError(f"Source kind already registered: {kind_id}")
        self._kinds[kind_id] = kind

        def unregister() -> None:
            self._kinds.pop(kind_id, None)

        return unregister

    def scope(self, input: dict) -> dict:
        source = input.get("source")
        kind = self._kinds.get(source)
        if kind and kind.get("scope"):
            return kind["scope"](input)
        return {"listened": True, "observed": False}

    def instance(self, source_id: str):
        return {"id": source_id, "enabled": True} if source_id in self._kinds else None

    def channel_allowed(self, channel: dict) -> bool:
        return True

    def send(self, receipt: dict, commands=None) -> str:
        impl = getattr(self, "_send_impl", None)
        if impl:
            return impl(receipt, commands)
        return "sent"
