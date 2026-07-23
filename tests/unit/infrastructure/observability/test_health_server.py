import json

from aiohttp.test_utils import make_mocked_request

from telegram_media_bot.domain.models import ComponentHealth, HealthReport
from telegram_media_bot.infrastructure.observability.health_server import HealthServer
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry


async def test_health_readiness_and_metrics_responses() -> None:
    async def healthy() -> HealthReport:
        return HealthReport(checks=(ComponentHealth("redis", True, "ok"),))

    async def depth() -> int:
        return 3

    metrics = MetricsRegistry()
    server = HealthServer(
        host="127.0.0.1", port=0, probe=healthy, metrics=metrics, queue_depth=depth
    )
    live = await server._liveness(make_mocked_request("GET", "/health"))
    ready = await server._readiness(make_mocked_request("GET", "/ready"))
    rendered = await server._render_metrics(make_mocked_request("GET", "/metrics"))
    assert live.status == 200
    assert ready.status == 200
    assert ready.text is not None
    assert rendered.text is not None
    assert json.loads(ready.text)["status"] == "ok"
    assert "media_bot_queue_depth 3" in rendered.text


async def test_unhealthy_readiness_returns_503() -> None:
    async def unhealthy() -> HealthReport:
        return HealthReport(checks=(ComponentHealth("redis", False),))

    async def depth() -> int:
        return 0

    server = HealthServer(
        host="127.0.0.1",
        port=0,
        probe=unhealthy,
        metrics=MetricsRegistry(),
        queue_depth=depth,
    )
    response = await server._readiness(make_mocked_request("GET", "/ready"))
    assert response.status == 503
