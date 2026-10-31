from typing import Any

from .runtime import install_webhook_runtime
from .rules.github_ready_review import register_github_ready_review_rule

__all__ = ["mount_github_webhook_ingress"]


def mount_github_webhook_ingress(api: Any, config: dict) -> None:
    """注册 GitHub webhook HTTP 入口与可选 ready-for-review 规则（web profile 插件调用）。"""
    path = config.get("path")
    source = config.get("source")
    if not isinstance(path, str) or path == "":
        raise ValueError("github webhook ingress requires a non-empty path")
    if not isinstance(source, str) or source == "":
        raise ValueError("github webhook ingress requires a non-empty source")
    runtime = install_webhook_runtime(api.ctx)
    runtime.bind_api(api)
    rule = config.get("rule")
    if rule is not None:
        if not isinstance(rule, dict):
            raise TypeError("github webhook rule config must be an object")
        register_github_ready_review_rule(runtime, {**rule, "source": source})
    routes = getattr(api, "_github_webhook_routes", None)
    if routes is None:
        routes = []
        api._github_webhook_routes = routes
    routes.append(dict(config))
