from __future__ import annotations

import pytest

from telegram_media_bot.domain.errors import (
    GalleryDlAuthenticationRequiredError,
    GalleryDlEmptyResponseError,
    GalleryDlExtractionError,
    GalleryDlOutputChangedError,
    GalleryDlRateLimitedError,
    GalleryDlTransientError,
    MediaBotError,
    MediaUnavailableError,
)
from telegram_media_bot.workers import jobs


@pytest.mark.parametrize(
    ("attempt", "draw", "expected"),
    [
        (1, 0, 10.0),
        (1, 1_000_000, 20.0),
        (2, 0, 20.0),
        (3, 0, 40.0),
        (20, 0, 450.0),
        (20, 1_000_000, 900.0),
    ],
)
def test_gallery_transient_backoff_has_jitter_exponential_growth_and_cap(
    monkeypatch: pytest.MonkeyPatch, attempt: int, draw: int, expected: float
) -> None:
    monkeypatch.setattr("telegram_media_bot.workers.jobs.secrets.randbelow", lambda _bound: draw)
    error = GalleryDlTransientError("temporary network failure", adapter="gallery-dl")
    assert jobs._retry_delay_seconds(error, attempt=attempt, base_delay_seconds=20) == expected


@pytest.mark.parametrize("provider_delay", [30.0, 300.0, 900.0])
def test_rate_limit_deadline_is_never_shortened(
    monkeypatch: pytest.MonkeyPatch, provider_delay: float
) -> None:
    monkeypatch.setattr("telegram_media_bot.workers.jobs.secrets.randbelow", lambda _bound: 0)
    error = GalleryDlRateLimitedError(
        "provider throttled", adapter="gallery-dl", retry_after_seconds=provider_delay
    )
    assert jobs._retry_delay_seconds(error, attempt=1, base_delay_seconds=20) == provider_delay


@pytest.mark.parametrize("provider_delay", [901.0, float("inf"), float("nan"), -1.0])
def test_unusable_or_excessive_rate_limit_deadline_does_not_cause_early_retry(
    provider_delay: float,
) -> None:
    error = GalleryDlRateLimitedError(
        "provider throttled", adapter="gallery-dl", retry_after_seconds=provider_delay
    )
    assert jobs._retry_delay_seconds(error, attempt=1, base_delay_seconds=20) is None


@pytest.mark.parametrize(
    "error",
    [
        GalleryDlEmptyResponseError("ambiguous response"),
        GalleryDlExtractionError("unconfirmed extraction failure"),
        GalleryDlAuthenticationRequiredError("login required"),
        GalleryDlOutputChangedError("changed schema"),
        MediaUnavailableError("expired story"),
    ],
)
def test_terminal_extraction_causes_never_schedule_another_inspection(error: MediaBotError) -> None:
    assert jobs._retry_delay_seconds(error, attempt=1, base_delay_seconds=20) is None
