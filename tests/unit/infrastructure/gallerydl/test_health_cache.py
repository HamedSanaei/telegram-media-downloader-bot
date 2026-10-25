from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import cast
from unittest.mock import Mock

import pytest

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.infrastructure.gallerydl import adapter as adapter_module
from telegram_media_bot.infrastructure.gallerydl.adapter import (
    GALLERY_DL_VERSION,
    GalleryDlEngine,
)
from telegram_media_bot.infrastructure.gallerydl.models import GalleryProcessResult
from telegram_media_bot.infrastructure.gallerydl.runner import GalleryDlRunner


def _version_result(version: str = GALLERY_DL_VERSION) -> GalleryProcessResult:
    return GalleryProcessResult(
        return_code=0,
        stdout=version.encode(),
        stderr=b"",
        elapsed_seconds=0.01,
    )


def test_healthy_gallery_probe_is_cached_for_five_minutes(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [100.0]
    monkeypatch.setattr(adapter_module, "monotonic", lambda: clock[0])
    runner = Mock(spec=GalleryDlRunner)
    runner.run.return_value = _version_result()
    engine = GalleryDlEngine(settings, runner=cast(GalleryDlRunner, runner))

    assert engine.health().healthy
    clock[0] = 399.9
    assert engine.health().healthy
    assert runner.run.call_count == 1

    clock[0] = 400.0
    assert engine.health().healthy
    assert runner.run.call_count == 2


def test_failed_gallery_probe_recovers_after_short_ttl(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [100.0]
    monkeypatch.setattr(adapter_module, "monotonic", lambda: clock[0])
    runner = Mock(spec=GalleryDlRunner)
    runner.run.side_effect = [OSError("unavailable"), _version_result()]
    engine = GalleryDlEngine(settings, runner=cast(GalleryDlRunner, runner))

    assert not engine.health().healthy
    clock[0] = 129.9
    assert not engine.health().healthy
    assert runner.run.call_count == 1

    clock[0] = 130.0
    assert engine.health().healthy
    assert runner.run.call_count == 2


def test_concurrent_gallery_health_checks_share_one_subprocess(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter_module, "monotonic", lambda: 100.0)
    runner = Mock(spec=GalleryDlRunner)
    runner.run.return_value = _version_result()
    engine = GalleryDlEngine(settings, runner=cast(GalleryDlRunner, runner))

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: engine.health(), range(16)))

    assert all(outcome.healthy for outcome in outcomes)
    assert runner.run.call_count == 1
