import hashlib
import hmac
import json
import os
import time

from fastapi import Request
from fastapi.responses import Response

from ..seams.credentials_local import credential_ref

MAX_BODY_BYTES = 64 * 1024

__all__ = ["create_github_webhook_handler", "resolve_github_webhook_secret"]


def resolve_github_webhook_secret(ctx, secret_env: str) -> str | None:
    ref = credential_ref(secret_env)
    credentials = ctx.get("credentials")
    if credentials is not None:
        record = credentials.read_record(ref)
        if isinstance(record, dict):
            value = record.get("value")
            if isinstance(value, str) and value != "":
                return value
        described = credentials.describe(secret_env)
        if described.get("configured") is True:
            resolved = getattr(credentials.provider, "resolve", None)
            if callable(resolved):
                row = resolved(ref)
                if row is not None and row[0]:
                    return row[0]
    env_value = os.environ.get(secret_env)
    return env_value if env_value else None


def create_github_webhook_handler(api, config: dict):
    path = config["path"]
    source = config["source"]
    secret_env = config.get("secretEnv", "DSH_GITHUB_WEBHOOK_SECRET")
    max_body_bytes = int(config.get("maxBodyBytes", MAX_BODY_BYTES))

    async def handler(request: Request) -> Response:
        try:
            if request.method != "POST":
                return Response("method not allowed", status_code=405, headers={"allow": "POST"})
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                return Response("content type must be application/json", status_code=415)
            body = await request.body()
            if len(body) > max_body_bytes:
                return Response("request body too large", status_code=413)
            signature = _required_header(request, "x-hub-signature-256")
            delivery_id = _required_header(request, "x-github-delivery")
            event_name = _required_header(request, "x-github-event")
            secret = resolve_github_webhook_secret(api.ctx, secret_env)
            if secret is None:
                return Response("GitHub webhook secret is unavailable", status_code=503)
            if not _verify_signature(secret, body, signature):
                return Response("invalid webhook signature", status_code=401)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return Response("request body is not valid JSON", status_code=400)
            if not isinstance(payload, dict):
                return Response("GitHub webhook payload must be a JSON object", status_code=400)
            runtime = api.ctx.get("webhookRuntime")
            if runtime is None:
                return Response("webhook runtime is unavailable", status_code=503)
            delivery = {
                "kind": "github",
                "source": source,
                "deliveryId": delivery_id,
                "event": {"name": event_name, "payload": payload},
                "receivedAt": int(time.time() * 1000),
            }
            runtime.dispatch(delivery)
            return Response(status_code=202)
        except _HttpError as error:
            return Response(error.message, status_code=error.status)
        except Exception:
            return Response("webhook ingress is unavailable", status_code=503)

    handler.__name__ = f"github_webhook_{path.strip('/').replace('/', '_')}"
    return handler


def _required_header(request: Request, name: str) -> str:
    value = request.headers.get(name)
    if value is None or value.strip() == "":
        raise _HttpError(400, f"missing {name} header")
    return value.strip()


class _HttpError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def _verify_signature(secret: str, body: bytes, signature: str) -> bool:
    if not signature.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(expected, signature)
