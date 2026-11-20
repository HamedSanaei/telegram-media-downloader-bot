from __future__ import annotations

import math
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from time import monotonic, sleep, time

from telegram_media_bot.application.ports.download_engine import CancellationCheck
from telegram_media_bot.application.ports.instagram_admission import (
    InstagramAdmissionRepository,
    InstagramPermitLock,
)
from telegram_media_bot.domain.errors import (
    AuthenticationRequiredError,
    JobCancelledError,
    MediaBotError,
    RateLimitedError,
)
from telegram_media_bot.domain.instagram_admission import InstagramAdmissionError

_CANCELLATION: ContextVar[CancellationCheck | None] = ContextVar(
    "instagram_cancellation", default=None
)
_WORKSPACE: ContextVar[Path | None] = ContextVar("instagram_workspace", default=None)


@contextmanager
def instagram_cancellation(
    check: CancellationCheck, *, workspace: Path | None = None
) -> Iterator[None]:
    token = _CANCELLATION.set(check)
    workspace_token = _WORKSPACE.set(workspace)
    try:
        yield
    finally:
        _CANCELLATION.reset(token)
        _WORKSPACE.reset(workspace_token)


def instagram_operation_workspace(fallback: Path) -> Path:
    return _WORKSPACE.get() or fallback


class InstagramAdmissionService:
    """Authorize every provider operation, independently of Telegram user/job limits."""

    def __init__(
        self,
        repository: InstagramAdmissionRepository,
        lock: InstagramPermitLock,
        *,
        spacing_seconds: float = 30,
        burst_window_seconds: int = 3600,
        burst_limit: int = 6,
        daily_limit: int = 30,
        rate_limit_cooldown_seconds: int = 3600,
        max_wait_seconds: float = 120,
        clock: Callable[[], float] = time,
        timer: Callable[[], float] = monotonic,
        wait: Callable[[float], None] = sleep,
        observer: Callable[[str, str], None] | None = None,
    ) -> None:
        self.repository = repository
        self._lock = lock
        self._spacing = spacing_seconds
        self.burst_window_seconds = burst_window_seconds
        self._burst_limit = burst_limit
        self._daily_limit = daily_limit
        self._cooldown = rate_limit_cooldown_seconds
        self._max_wait = max_wait_seconds
        self._clock = clock
        self._timer = timer
        self._wait = wait
        self._observer = observer

    def _observe(self, outcome: str, reason: str) -> None:
        if self._observer is not None:
            # Observability must never change the safety decision or mask an upstream error.
            with suppress(Exception):
                self._observer(outcome, reason)

    @contextmanager
    def operation(
        self,
        identity: str,
        kind: str,
        *,
        is_cancelled: CancellationCheck | None = None,
    ) -> Iterator[None]:
        token = secrets.token_hex(24)
        deadline = self._timer() + self._max_wait
        admitted = False
        locked = False
        safe_to_release = True
        check_cancelled = is_cancelled or _CANCELLATION.get()
        try:
            self.repository.waiter(identity, token, until=self._clock() + self._max_wait + 5)
            while not admitted:
                if check_cancelled is not None and check_cancelled():
                    raise JobCancelledError("Instagram admission cancelled")
                decision = self.repository.check_pause(identity, now=self._clock())
                if not decision.allowed:
                    raise InstagramAdmissionError(
                        decision.reason, retry_after_seconds=decision.retry_after_seconds
                    )
                locked = self._lock.acquire(identity, token)
                if locked:
                    decision = self.repository.admit(
                        identity,
                        kind,
                        token,
                        now=self._clock(),
                        spacing=self._spacing,
                        burst_window=self.burst_window_seconds,
                        burst_limit=self._burst_limit,
                        daily_limit=self._daily_limit,
                    )
                    admitted = decision.allowed
                    if not admitted:
                        self._lock.release(identity, token)
                        locked = False
                if not admitted:
                    wait_reason = decision.reason if not decision.allowed else "active"
                    remaining = deadline - self._timer()
                    # Never hold a worker waiting out a provider circuit or a quota window.
                    if wait_reason not in {"active", "spacing"} or remaining <= 0:
                        raise InstagramAdmissionError(
                            wait_reason, retry_after_seconds=decision.retry_after_seconds or 1
                        )
                    self._wait(min(0.25, remaining))
            self.repository.waiter(identity, token, until=None)
            if check_cancelled is not None and check_cancelled():
                raise JobCancelledError("Instagram admission cancelled")
            self._observe("admitted", "ready")
            try:
                yield
            except MediaBotError as exc:
                reason: str | None = None
                until: float | None = None
                cooldown = exc.retry_after_seconds
                if isinstance(exc, RateLimitedError) or exc.http_status == 429:
                    reason = "rate_limited"
                elif isinstance(exc, AuthenticationRequiredError):
                    reason = (
                        "challenge_required"
                        if exc.diagnostic_category == "challenge_required"
                        else "reauthentication_required"
                    )
                elif cooldown is not None and cooldown > 0:
                    reason = "provider_cooldown"
                if reason is None:
                    raise
                # An invalid/unknown deadline fails closed to review, never to an early probe.
                if reason in {"rate_limited", "provider_cooldown"} and (
                    cooldown is None or (math.isfinite(cooldown) and cooldown >= 0)
                ):
                    until = self._clock() + max(self._cooldown, cooldown or 0)
                safe_to_release = False
                changed = self.repository.pause(identity, reason=reason, until=until)
                safe_to_release = True
                if changed:
                    self._observe("paused", reason)
                paused = InstagramAdmissionError(
                    reason, retry_after_seconds=None if until is None else until - self._clock()
                )
                paused.http_status = exc.http_status
                paused.adapter = exc.adapter
                paused.failure_stage = exc.failure_stage
                raise paused from exc
        except InstagramAdmissionError as exc:
            self._observe("denied", exc.diagnostic_category or "state_unavailable")
            raise
        finally:
            # Keep the durable active marker if release cannot be recorded. Redis expiry alone
            # must never reopen an operation whose termination was not confirmed.
            try:
                if admitted and safe_to_release:
                    self.repository.finish(identity, token)
            finally:
                try:
                    if locked:
                        self._lock.release(identity, token)
                finally:
                    self.repository.waiter(identity, token, until=None)
