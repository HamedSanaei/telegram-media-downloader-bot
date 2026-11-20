from typing import Protocol

from telegram_media_bot.domain.instagram_admission import (
    AdmissionDecision,
    InstagramAdmissionState,
    InstagramAdmissionSummary,
)


class InstagramAdmissionRepository(Protocol):
    def admit(
        self,
        identity: str,
        kind: str,
        token: str,
        *,
        now: float,
        spacing: float,
        burst_window: int,
        burst_limit: int,
        daily_limit: int,
    ) -> AdmissionDecision: ...

    def finish(self, identity: str, token: str) -> None: ...

    def pause(self, identity: str, *, reason: str, until: float | None) -> bool: ...

    def check_pause(self, identity: str, *, now: float) -> AdmissionDecision: ...

    def waiter(self, identity: str, token: str, *, until: float | None) -> None: ...

    def snapshot(self, *, now: float, burst_window: int) -> tuple[InstagramAdmissionState, ...]: ...

    def summary(self, *, now: float) -> InstagramAdmissionSummary: ...


class InstagramPermitLock(Protocol):
    def acquire(self, identity: str, token: str) -> bool: ...

    def release(self, identity: str, token: str) -> None: ...
