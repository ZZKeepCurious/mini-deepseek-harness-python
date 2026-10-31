"""Host open-in-app HTTP routes（对齐 `@deepseek-ai/dsh-host-open-in-app`）。

web 组合无原生桌面解析器：apps 路由恒空列表，open 路由 503——前端据此隐藏
「Open In…」控件（与上游无 Host desktop 时一致）。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from fastapi.responses import Response

OPEN_IN_APP_APPS_PATH = "/open-in-app/apps"
OPEN_IN_APP_OPEN_PATH = "/open-in-app/open"

__all__ = [
    "OPEN_IN_APP_APPS_PATH",
    "OPEN_IN_APP_OPEN_PATH",
    "handle_open_in_app_apps",
    "handle_open_in_app_open",
]


def handle_open_in_app_apps(_request: Request) -> Response:
    body = json.dumps({"apps": []}).encode("utf-8")
    return Response(
        body,
        status_code=200,
        media_type="application/json; charset=utf-8",
        headers={"cache-control": "no-store"},
    )


async def handle_open_in_app_open(request: Request) -> Response:
    if request.method != "POST":
        return Response("method not allowed", status_code=405, headers={"allow": "POST"})
    return Response(
        "open-in-app is unavailable in this deployment",
        status_code=503,
        media_type="text/plain; charset=utf-8",
        headers={"cache-control": "no-store"},
    )
