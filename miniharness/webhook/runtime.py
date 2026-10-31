import json
import threading
import time
from typing import Any, Callable

from ..core.scope import Context, Service

__all__ = ["WebhookRuntime", "install_webhook_runtime"]


class WebhookRuntime(Service):
    provide = "webhookRuntime"

    def __init__(self, ctx: Context):
        super().__init__(ctx, "webhookRuntime")
        self._rules: dict[str, dict] = {}
        self._api: Any = None
        self._lock = threading.Lock()
        self._closing = False

    def bind_api(self, api: Any) -> None:
        self._api = api

    def register(self, rule_id: str, kind: str, run: Callable[[dict, Any], dict | None]) -> Callable[[], None]:
        if self._closing:
            raise RuntimeError("webhook runtime is closing")
        if not isinstance(rule_id, str) or rule_id.strip() == "":
            raise TypeError("webhook rule id must be a non-empty string")
        if not isinstance(kind, str) or kind.strip() == "":
            raise TypeError("webhook rule kind must be a non-empty string")
        with self._lock:
            if rule_id in self._rules:
                raise RuntimeError(f'webhook rule "{rule_id}" is already registered')
            entry = {"id": rule_id, "kind": kind, "run": run}
            self._rules[rule_id] = entry

        def dispose() -> None:
            with self._lock:
                self._rules.pop(rule_id, None)

        return dispose

    def dispatch(self, delivery: dict) -> None:
        if self._closing:
            raise RuntimeError("webhook runtime is closing")
        snapshot = _snapshot_delivery(delivery)
        with self._lock:
            rules = [dict(entry) for entry in self._rules.values()]
        for entry in rules:
            if entry["kind"] != snapshot["kind"]:
                continue
            threading.Thread(
                target=self._invoke,
                args=(entry, snapshot),
                daemon=True,
                name=f"webhook-{entry['id']}",
            ).start()

    def _invoke(self, entry: dict, delivery: dict) -> None:
        api = self._api
        if api is None:
            return
        try:
            request = entry["run"](delivery, None)
            if request is None:
                return
            api.create_webhook_session(delivery, entry["id"], request)
        except Exception as error:  # noqa: BLE001 - fire-and-forget containment
            logger = self.ctx.logger
            if logger is not None:
                logger.warn(
                    "webhook: rule %s failed: %s",
                    entry["id"],
                    error,
                )


def _snapshot_delivery(delivery: dict) -> dict:
    if not isinstance(delivery, dict):
        raise TypeError("webhook delivery must be an object")
    kind = delivery.get("kind")
    source = delivery.get("source")
    delivery_id = delivery.get("deliveryId")
    received_at = delivery.get("receivedAt")
    event = delivery.get("event")
    if not isinstance(kind, str) or kind.strip() == "":
        raise TypeError("webhook delivery kind must be a non-empty string")
    if not isinstance(source, str) or source.strip() == "":
        raise TypeError("webhook delivery source must be a non-empty string")
    if not isinstance(delivery_id, str) or delivery_id.strip() == "":
        raise TypeError("webhook delivery id must be a non-empty string")
    if not isinstance(received_at, int) or received_at < 0:
        raise TypeError("webhook delivery receivedAt must be a non-negative integer")
    json.dumps(event)
    return {
        "kind": kind,
        "source": source,
        "deliveryId": delivery_id,
        "receivedAt": received_at,
        "event": event,
    }


def install_webhook_runtime(ctx: Context) -> WebhookRuntime:
    existing = ctx.get("webhookRuntime")
    if isinstance(existing, WebhookRuntime):
        return existing
    return WebhookRuntime(ctx)
