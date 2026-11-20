"""Secret-free vocabulary for credential-wide Instagram operation admission."""

from dataclasses import dataclass

from telegram_media_bot.domain.errors import MediaBotError


class InstagramAdmissionError(MediaBotError):
    """No provider request is permitted; never switch credentials or retry automatically."""

    def __init__(self, reason: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(
            "Instagram session paused: " + reason,
            source="instagram",
            diagnostic_category=reason,
            retry_after_seconds=retry_after_seconds,
        )


@dataclass(frozen=True, slots=True)
class InstagramAdmissionState:
    identity: str
    kind: str
    reason: str
    pause_until: float | None
    active: bool
    recent_operations: int
    daily_operations: int
    waiting: int


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    allowed: bool
    reason: str = "ready"
    retry_after_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class InstagramAdmissionSummary:
    credentials: int
    paused: int
    active: int
    waiting: int
