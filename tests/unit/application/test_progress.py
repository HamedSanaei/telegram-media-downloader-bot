from telegram_media_bot.application.services.progress import (
    DeliveryProgressThrottler,
    ProgressThrottler,
)
from telegram_media_bot.domain.models import (
    DeliveryProgressEvent,
    DeliveryStage,
    JobId,
    ProgressEvent,
)


def event(downloaded: int, total: int = 100) -> ProgressEvent:
    return ProgressEvent(
        job_id=JobId("job"),
        status="downloading",
        downloaded_bytes=downloaded,
        total_bytes=total,
    )


def test_progress_requires_time_and_percentage_delta() -> None:
    throttler = ProgressThrottler(min_interval_seconds=2, min_percent_delta=5)
    assert throttler.should_emit(event(10), now=2)
    assert not throttler.should_emit(event(14), now=5)
    assert not throttler.should_emit(event(20), now=3)
    assert throttler.should_emit(event(20), now=5)


def test_terminal_progress_is_always_emitted() -> None:
    throttler = ProgressThrottler(min_interval_seconds=60, min_percent_delta=100)
    finished = ProgressEvent(job_id=JobId("job"), status="finished")
    assert throttler.should_emit(finished, now=0)


def test_delivery_progress_emits_stage_changes_and_throttles_chunks() -> None:
    throttler = DeliveryProgressThrottler(min_interval_seconds=2, min_percent_delta=5)
    uploading = DeliveryProgressEvent(
        job_id=JobId("job"),
        stage=DeliveryStage.UPLOADING,
        transferred_bytes=10,
        total_bytes=100,
    )
    assert throttler.should_emit(uploading, now=0)
    assert not throttler.should_emit(
        DeliveryProgressEvent(
            job_id=JobId("job"),
            stage=DeliveryStage.UPLOADING,
            transferred_bytes=14,
            total_bytes=100,
        ),
        now=3,
    )
    assert throttler.should_emit(
        DeliveryProgressEvent(
            job_id=JobId("job"),
            stage=DeliveryStage.FINALIZING,
            transferred_bytes=100,
            total_bytes=100,
        ),
        now=3,
    )
