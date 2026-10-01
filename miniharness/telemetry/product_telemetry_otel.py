"""产品分析策略适配器（对齐 packages/host/product-telemetry-otel）。

把应用组合选择的分析记录交给共享 `ctx.otel` 服务的普通事件通道（按条数批处理）。
挂载本身不发送任何东西；拥有 fiber 卸载时经 `AbortController` 在外层期限到点后
取消在途导出。记录类型是 `OTelEventRecord` 的别名。
"""
from __future__ import annotations

import asyncio
import re
import urllib.parse

from ..core.scope import Context, Service
from .otel import OTelEventRecord
from .otel_transport import AbortController

__all__ = [
    "DEFAULT_ENDPOINT",
    "DEFAULT_CHANNEL",
    "ProductTelemetry",
    "ProductTelemetryRecord",
    "resolve_product_telemetry_config",
]

DEFAULT_ENDPOINT = "https://dsh-otel-collector.deepseeksvc.com/v1/logs"
DEFAULT_CHANNEL = "dsh_otel_report"

_MAX_TIMER_DELAY_MILLIS = 2_147_483_647
_HEADER_VALUE = re.compile(r"^[\x20-\x7e\t]+$")

#: 记录是普通事件的别名（调用方只选已批准的分析字段）。
ProductTelemetryRecord = OTelEventRecord

_DEFAULTS = {
    "endpoint": DEFAULT_ENDPOINT,
    "channel": DEFAULT_CHANNEL,
    "maxExportBatchSize": 512,
    "maxQueueSize": 2048,
    "scheduledDelayMillis": 30000,
    "timeoutMillis": 15000,
    "exportTimeoutMillis": 20000,
    "shutdownTimeoutMillis": 21000,
}


def _positive_integer(value: object, name: str) -> int:
    if (not isinstance(value, int) or isinstance(value, bool)
            or value < 1 or value > _MAX_TIMER_DELAY_MILLIS):
        raise ValueError(
            f"product-telemetry-otel: {name} must be a positive integer no greater than "
            f"{_MAX_TIMER_DELAY_MILLIS}")
    return value


def resolve_product_telemetry_config(config: dict | None) -> dict:
    """校验并填充配置（对齐上游 Config schema 的默认与失败语义）。"""
    config = config or {}
    resolved = dict(_DEFAULTS)
    for key in _DEFAULTS:
        if key in config and config[key] is not None:
            resolved[key] = config[key]

    endpoint = resolved["endpoint"]
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("product-telemetry-otel: endpoint must use HTTP or HTTPS")

    channel = resolved["channel"]
    if not isinstance(channel, str) or not _HEADER_VALUE.match(channel):
        raise ValueError(
            "product-telemetry-otel: channel must be a valid HTTP header value")

    service_name = config.get("serviceName")
    service_version = config.get("serviceVersion")
    if not isinstance(service_name, str) or service_name == "":
        raise ValueError("product-telemetry-otel: serviceName is required")
    if not isinstance(service_version, str) or service_version == "":
        raise ValueError("product-telemetry-otel: serviceVersion is required")
    resolved["serviceName"] = service_name
    resolved["serviceVersion"] = service_version

    compression = config.get("compression")
    if compression not in (None, "none", "gzip"):
        raise ValueError("product-telemetry-otel: compression must be 'none' or 'gzip'")
    resolved["compression"] = compression

    for key in ("maxExportBatchSize", "maxQueueSize", "scheduledDelayMillis",
                "timeoutMillis", "exportTimeoutMillis", "shutdownTimeoutMillis"):
        resolved[key] = _positive_integer(resolved[key], key)
    if resolved["maxExportBatchSize"] > resolved["maxQueueSize"]:
        raise ValueError("product-telemetry-otel: maxExportBatchSize must not exceed maxQueueSize")
    return resolved


class ProductTelemetry(Service):
    """宿主分析发送器：挂载不发送；拥有 fiber 卸载时排空。"""

    provide = "productTelemetry"
    inject = ["otel"]

    def __init__(self, ctx: Context, config: dict | None = None):
        resolved = resolve_product_telemetry_config(config)
        super().__init__(ctx, "productTelemetry")
        otel = ctx.get("otel")
        if otel is None:
            raise RuntimeError("product-telemetry-otel: the shared otel service is required")
        self._shutdown_timeout = resolved["shutdownTimeoutMillis"]
        self._reporter = otel.create_event_reporter({
            "exporter": {
                "url": resolved["endpoint"],
                "headers": {"x-channel": resolved["channel"]},
                "timeoutMillis": resolved["timeoutMillis"],
                "compression": resolved["compression"],
            },
            "resourceAttributes": {
                "service.name": resolved["serviceName"],
                "service.version": resolved["serviceVersion"],
            },
            "scope": {"name": "product-telemetry-otel"},
            "processor": {
                "maxExportBatchSize": resolved["maxExportBatchSize"],
                "maxQueueSize": resolved["maxQueueSize"],
                "scheduledDelayMillis": resolved["scheduledDelayMillis"],
                "exportTimeoutMillis": resolved["exportTimeoutMillis"],
            },
            "onFailure": lambda message, error=None: self._warn(message, error),
        })
        ctx.effect(lambda: self._dispose, "productTelemetry shutdown")

    def _warn(self, message: str, error: BaseException | None = None) -> None:
        logger = self.ctx.root.logger
        if logger is None:
            return
        if error is None:
            logger.warn(message)
        else:
            logger.warn(message, error)

    async def _dispose(self) -> None:
        controller = AbortController()
        loop = asyncio.get_running_loop()

        def on_deadline() -> None:
            self._warn(
                "Product telemetry shutdown deadline exceeded; pending events may be lost")
            controller.abort(RuntimeError("Product telemetry shutdown deadline exceeded"))

        timer = loop.call_later(self._shutdown_timeout / 1000.0, on_deadline)
        try:
            await self._reporter.shutdown(controller.signal)
        finally:
            timer.cancel()

    def emit(self, record: ProductTelemetryRecord) -> None:
        """入队一条已选产品事件（不等待网络投递）。"""
        self._reporter.emit(record)
