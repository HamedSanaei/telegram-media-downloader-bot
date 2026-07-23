from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiohttp import web

from telegram_media_bot.domain.models import HealthReport
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry

HealthProbe = Callable[[], Awaitable[HealthReport]]
QueueDepthProbe = Callable[[], Awaitable[int]]


class HealthServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        probe: HealthProbe,
        metrics: MetricsRegistry,
        queue_depth: QueueDepthProbe,
        metrics_enabled: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._probe = probe
        self._metrics = metrics
        self._queue_depth = queue_depth
        self._metrics_enabled = metrics_enabled
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._liveness)
        app.router.add_get("/ready", self._readiness)
        if self._metrics_enabled:
            app.router.add_get("/metrics", self._render_metrics)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self._host, self._port).start()

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _liveness(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _readiness(self, _request: web.Request) -> web.Response:
        report = await self._probe()
        payload = {
            "status": "ok" if report.healthy else "unavailable",
            "generated_at": report.generated_at.isoformat(),
            "checks": [
                {"name": check.name, "healthy": check.healthy, "detail": check.detail}
                for check in report.checks
            ],
        }
        return web.json_response(payload, status=200 if report.healthy else 503)

    async def _render_metrics(self, _request: web.Request) -> web.Response:
        self._metrics.set_queue_depth(await self._queue_depth())
        return web.Response(text=self._metrics.render(), content_type="text/plain")
