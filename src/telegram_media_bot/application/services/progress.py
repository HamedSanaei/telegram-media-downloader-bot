from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from telegram_media_bot.domain.models import DeliveryProgressEvent, ProgressEvent


@dataclass(slots=True)
class ProgressThrottler:
    min_interval_seconds: float
    min_percent_delta: float
    _last_time: float = 0.0
    _last_percent: float = -100.0

    def should_emit(self, event: ProgressEvent, *, now: float | None = None) -> bool:
        current = monotonic() if now is None else now
        percent = event.percent
        terminal = event.status in {"finished", "postprocessing"}
        enough_time = current - self._last_time >= self.min_interval_seconds
        enough_delta = (
            percent is not None and percent - self._last_percent >= self.min_percent_delta
        )
        if not terminal and not (enough_time and (percent is None or enough_delta)):
            return False
        self._last_time = current
        if percent is not None:
            self._last_percent = percent
        return True


@dataclass(slots=True)
class DeliveryProgressThrottler:
    min_interval_seconds: float
    min_percent_delta: float
    _last_time: float = 0.0
    _last_percent: float = -100.0
    _last_stage: str = ""
    _last_ordinal: int = 0

    def should_emit(
        self,
        event: DeliveryProgressEvent,
        *,
        now: float | None = None,
    ) -> bool:
        current = monotonic() if now is None else now
        stage_changed = (
            event.stage.value != self._last_stage or event.item_ordinal != self._last_ordinal
        )
        percent = event.percent
        enough_time = current - self._last_time >= self.min_interval_seconds
        enough_delta = (
            percent is not None and percent - self._last_percent >= self.min_percent_delta
        )
        if not stage_changed and not (enough_time and (percent is None or enough_delta)):
            return False
        self._last_time = current
        self._last_stage = event.stage.value
        self._last_ordinal = event.item_ordinal
        if percent is not None:
            self._last_percent = percent
        return True
