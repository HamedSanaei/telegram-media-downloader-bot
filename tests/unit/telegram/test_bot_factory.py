import asyncio

import pytest

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import LocalBotApiError
from telegram_media_bot.infrastructure.telegram.local_api import LocalBotApiManager
from telegram_media_bot.telegram.bot_factory import (
    create_bot,
    readiness_wait_required,
    wait_for_local_api_readiness,
)


async def test_bot_factory_supports_official_and_local_api(settings: Settings) -> None:
    official = create_bot(settings)
    assert not official.session.api.is_local
    await official.session.close()

    raw = settings.model_dump()
    raw["telegram"]["local_api_base_url"] = "http://telegram-bot-api:8081"
    raw["telegram"]["local_api_is_local"] = True
    configured = Settings.model_validate(raw)
    local = create_bot(configured)
    assert local.session.api.is_local
    await local.session.close()


def _local_api_settings(settings: Settings, *, mode: str, lifecycle_owner: str) -> Settings:
    raw = settings.model_dump()
    raw["telegram"]["local_api_base_url"] = "http://local-api:8081"
    raw["telegram"]["local_api_is_local"] = True
    raw["telegram"]["local_bot_api"]["enabled"] = True
    raw["telegram"]["local_bot_api"]["mode"] = mode
    raw["telegram"]["local_bot_api"]["lifecycle_owner"] = lifecycle_owner
    if mode == "managed":
        raw["telegram"]["local_bot_api"]["executable"] = "/usr/local/bin/telegram-bot-api"
        raw["telegram"]["local_bot_api"]["api_id"] = 12345
        raw["telegram"]["local_bot_api"]["api_hash"] = "0" * 32
    return Settings.model_validate(raw)


def test_readiness_wait_required_only_for_externally_started_endpoints(
    settings: Settings,
) -> None:
    assert not readiness_wait_required(settings)  # local_bot_api disabled

    external = _local_api_settings(settings, mode="external", lifecycle_owner="service")
    assert readiness_wait_required(external)

    service_managed = _local_api_settings(settings, mode="managed", lifecycle_owner="service")
    assert readiness_wait_required(service_managed)

    application_managed = _local_api_settings(
        settings, mode="managed", lifecycle_owner="application"
    )
    assert not readiness_wait_required(application_managed)


async def test_readiness_wait_returns_immediately_when_endpoint_is_ready(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes = 0

    def ready(_self: object) -> bool:
        nonlocal probes
        probes += 1
        return True

    monkeypatch.setattr(LocalBotApiManager, "endpoint_reachable", ready)

    await wait_for_local_api_readiness(
        _local_api_settings(settings, mode="external", lifecycle_owner="service")
    )

    assert probes == 1


async def test_readiness_wait_retries_with_backoff_until_ready(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes = 0

    def becoming_ready(_self: object) -> bool:
        nonlocal probes
        probes += 1
        return probes >= 3

    monkeypatch.setattr(LocalBotApiManager, "endpoint_reachable", becoming_ready)

    await wait_for_local_api_readiness(
        _local_api_settings(settings, mode="external", lifecycle_owner="service")
    )

    assert probes == 3


async def test_readiness_wait_times_out_after_bounded_deadline(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(LocalBotApiManager, "endpoint_reachable", lambda _self: False)
    configured = _local_api_settings(settings, mode="external", lifecycle_owner="service")
    raw = configured.model_dump()
    raw["telegram"]["local_bot_api"]["startup_timeout_seconds"] = 1
    configured = Settings.model_validate(raw)

    with pytest.raises(LocalBotApiError, match="did not become reachable"):
        await wait_for_local_api_readiness(configured)


async def test_readiness_wait_cancellation_stops_promptly(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(LocalBotApiManager, "endpoint_reachable", lambda _self: False)
    configured = _local_api_settings(settings, mode="external", lifecycle_owner="service")
    raw = configured.model_dump()
    raw["telegram"]["local_bot_api"]["startup_timeout_seconds"] = 60
    configured = Settings.model_validate(raw)

    task = asyncio.create_task(wait_for_local_api_readiness(configured))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
