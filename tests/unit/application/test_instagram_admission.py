from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest
from pydantic import ValidationError

from telegram_media_bot.application.services.instagram_admission import (
    InstagramAdmissionService,
    instagram_cancellation,
)
from telegram_media_bot.bootstrap.config import InstagramAdmissionSection, Settings
from telegram_media_bot.domain.credential_resolution import (
    CredentialContext,
    CredentialKind,
    CredentialPolicy,
    ResolvedCredential,
)
from telegram_media_bot.domain.errors import (
    AuthenticationRequiredError,
    GalleryDlEmptyResponseError,
    JobCancelledError,
    RateLimitedError,
)
from telegram_media_bot.domain.instagram_admission import InstagramAdmissionError
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry
from telegram_media_bot.infrastructure.persistence.sqlite_instagram_admission import (
    SqliteInstagramAdmissionRepository,
)
from telegram_media_bot.infrastructure.security.instagram_admission import (
    instagram_identity,
    personal_instagram_identity,
)
from telegram_media_bot.infrastructure.ytdlp.error_mapper import map_ytdlp_error


class Clock:
    def __init__(self) -> None:
        self.now = 100_000.0

    def time(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.now += seconds


class Lock:
    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}

    def acquire(self, identity: str, token: str) -> bool:
        if identity in self.tokens:
            return False
        self.tokens[identity] = token
        return True

    def release(self, identity: str, token: str) -> None:
        if self.tokens.get(identity) == token:
            del self.tokens[identity]


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[tuple[InstagramAdmissionService, Clock, Lock]]:
    repo = SqliteInstagramAdmissionRepository(tmp_path / "state.sqlite3")
    repo.initialize()
    clock, lock = Clock(), Lock()
    service = InstagramAdmissionService(
        repo, lock, clock=clock.time, timer=clock.time, wait=clock.wait
    )
    yield service, clock, lock
    assert not lock.tokens


def test_shared_operator_inspection_and_download_are_separately_charged_and_spaced(
    runtime: tuple[InstagramAdmissionService, Clock, Lock],
) -> None:
    service, clock, _ = runtime
    for _ in range(2):
        with service.operation("shared", "operator"):
            pass
    assert clock.now == 100_030
    (state,) = service.repository.snapshot(now=clock.now, burst_window=3600)
    assert state.daily_operations == state.recent_operations == 2
    assert not state.active and state.waiting == 0


def test_shared_burst_budget_and_rolling_day_survive_restart(
    runtime: tuple[InstagramAdmissionService, Clock, Lock],
) -> None:
    service, clock, lock = runtime
    for _ in range(6):
        with service.operation("shared", "operator"):
            pass
    restarted = InstagramAdmissionService(
        service.repository, lock, clock=clock.time, timer=clock.time, wait=clock.wait
    )
    with (
        pytest.raises(InstagramAdmissionError, match="burst_budget"),
        restarted.operation("shared", "operator"),
    ):
        pytest.fail("over-budget provider request")
    with restarted.operation("unrelated", "personal"):
        pass
    clock.now = 103_600
    for _ in range(6):
        with restarted.operation("shared", "operator"):
            pass
    state = next(
        s
        for s in service.repository.snapshot(now=clock.now, burst_window=3600)
        if s.identity == "shared"
    )
    assert state.daily_operations == 12 and state.recent_operations == 6


def test_daily_budget_uses_rolling_24_hours_and_review_cannot_reset_it(tmp_path: Path) -> None:
    repo = SqliteInstagramAdmissionRepository(tmp_path / "state.sqlite3")
    repo.initialize()
    clock = Clock()
    service = InstagramAdmissionService(
        repo,
        Lock(),
        daily_limit=2,
        burst_limit=2,
        clock=clock.time,
        timer=clock.time,
        wait=clock.wait,
    )
    for _ in range(2):
        with service.operation("shared", "operator"):
            pass
    clock.now += 3600
    with (
        pytest.raises(InstagramAdmissionError, match="daily_budget"),
        service.operation("shared", "operator"),
    ):
        pytest.fail("daily quota bypass")
    assert not repo.review("shared", now=clock.now)
    clock.now = 186_400
    assert repo.review("shared", now=clock.now)
    service._spacing = 0
    with service.operation("shared", "operator"):
        pass
    with (
        pytest.raises(InstagramAdmissionError, match="daily_budget"),
        service.operation("shared", "operator"),
    ):
        pytest.fail("review erased unexpired history")


@pytest.mark.parametrize(
    "error,reason",
    [
        (RateLimitedError("fixture", retry_after_seconds=86401), "rate_limited"),
        (
            AuthenticationRequiredError("fixture", diagnostic_category="challenge_required"),
            "challenge_required",
        ),
        (AuthenticationRequiredError("fixture"), "reauthentication_required"),
    ],
)
def test_provider_pause_blocks_all_queued_operations_without_account_fallback(
    runtime: tuple[InstagramAdmissionService, Clock, Lock],
    error: Exception,
    reason: str,
) -> None:
    service, clock, _ = runtime
    observations: list[tuple[str, str]] = []
    service._observer = lambda outcome, why: observations.append((outcome, why))
    calls = 0
    with (
        pytest.raises(InstagramAdmissionError, match=reason),
        service.operation("shared", "operator"),
    ):
        calls += 1
        raise error
    for _ in range(3):
        with (
            pytest.raises(InstagramAdmissionError, match=reason),
            service.operation("shared", "operator"),
        ):
            calls += 1
    assert calls == 1
    assert observations.count(("paused", reason)) == 1
    assert service.repository.snapshot(now=clock.now, burst_window=3600)[0].reason == reason
    summary = service.repository.summary(now=clock.now)
    assert summary.paused == 1 and summary.active == summary.waiting == 0


def test_long_provider_cooldown_is_not_capped_at_worker_retry_limit(
    runtime: tuple[InstagramAdmissionService, Clock, Lock],
) -> None:
    service, clock, _ = runtime
    with pytest.raises(InstagramAdmissionError) as failure, service.operation("shared", "operator"):
        raise RateLimitedError("fixture", retry_after_seconds=10000)
    assert not failure.value.retryable
    clock.now += 9999
    assert not service.repository.check_pause("shared", now=clock.now).allowed
    clock.now += 1
    with service.operation("shared", "operator"):
        pass


def test_crashed_or_lost_redis_lease_remains_quarantined_until_offline_review(
    runtime: tuple[InstagramAdmissionService, Clock, Lock],
) -> None:
    service, clock, _ = runtime
    assert service.repository.admit(
        "shared",
        "operator",
        "crashed",
        now=clock.now,
        spacing=30,
        burst_window=3600,
        burst_limit=6,
        daily_limit=30,
    ).allowed
    clock.now += 86401
    with (
        pytest.raises(InstagramAdmissionError, match="active"),
        service.operation("shared", "operator"),
    ):
        pytest.fail("crashed operation resumed automatically")
    repo = service.repository
    assert isinstance(repo, SqliteInstagramAdmissionRepository)
    assert not repo.review("shared", now=clock.now)
    assert repo.review("shared", now=clock.now, clear_interrupted=True)
    with service.operation("shared", "operator"):
        pass


def test_cancellation_while_waiting_releases_waiter_without_provider_request(
    runtime: tuple[InstagramAdmissionService, Clock, Lock],
) -> None:
    service, clock, lock = runtime
    assert lock.acquire("shared", "other")
    with (
        instagram_cancellation(lambda: clock.now > 100_001),
        pytest.raises(JobCancelledError),
        service.operation("shared", "operator"),
    ):
        pytest.fail("cancelled request reached provider")
    lock.release("shared", "other")


def test_non_session_failure_does_not_pause_but_is_still_charged(
    runtime: tuple[InstagramAdmissionService, Clock, Lock],
) -> None:
    service, clock, _ = runtime
    with pytest.raises(GalleryDlEmptyResponseError), service.operation("shared", "operator"):
        raise GalleryDlEmptyResponseError("fixture")
    assert service.repository.check_pause("shared", now=clock.now).allowed
    assert service.repository.snapshot(now=clock.now, burst_window=3600)[0].daily_operations == 1


def test_cookie_generation_identity_does_not_use_workspace_or_cookie_metadata(
    tmp_path: Path,
) -> None:
    path = tmp_path / "canonical.txt"
    payload = "# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\tsecret-fixture\n"
    path.write_text(payload, encoding="utf-8")
    first = instagram_identity(ResolvedCredential.operator_public(), None, path)
    path.write_text(
        payload + ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tunrelated\n", encoding="utf-8"
    )
    assert instagram_identity(None, str(path), None) == first
    path.write_text(payload.replace("secret-fixture", "rotated-fixture"), encoding="utf-8")
    assert instagram_identity(None, None, path) != first
    personal = ResolvedCredential(
        CredentialContext(
            CredentialKind.USER_INSTAGRAM, CredentialPolicy.USER_ONLY, user_generation=7
        ),
        "first/path",
        owner_user_id=20,
    )
    assert instagram_identity(personal, None, None) == instagram_identity(
        replace(personal, materialized_cookie_path="other/path"), None, None
    )
    assert instagram_identity(personal, None, None)[0] == personal_instagram_identity(20, 7)
    assert instagram_identity(personal, None, None) != instagram_identity(
        replace(personal, owner_user_id=21), None, None
    )
    assert personal_instagram_identity(20, 7) != personal_instagram_identity(20, 8)
    assert "secret-fixture" not in first[0]


def test_missing_identity_fails_closed_and_anonymous_is_separate(tmp_path: Path) -> None:
    with pytest.raises(InstagramAdmissionError):
        instagram_identity(None, str(tmp_path / "missing"), None)
    bad = ResolvedCredential(
        CredentialContext(
            CredentialKind.USER_INSTAGRAM, CredentialPolicy.USER_ONLY, user_generation=1
        ),
        "path",
    )
    with pytest.raises(InstagramAdmissionError):
        instagram_identity(bad, None, None)
    assert (
        instagram_identity(ResolvedCredential.none(), None, tmp_path / "missing")[1] == "anonymous"
    )


def test_yt_dlp_429_precedes_login_text_and_retains_retry_after() -> None:
    from email.message import Message

    headers = Message()
    headers["Retry-After"] = "10000"
    error = map_ytdlp_error(
        HTTPError("https://fixture.invalid", 429, "login cookies too many requests", headers, None)
    )
    assert isinstance(error, RateLimitedError) and error.retry_after_seconds == 10000
    challenge = map_ytdlp_error(RuntimeError("checkpoint_required"))
    assert isinstance(challenge, AuthenticationRequiredError)
    assert challenge.diagnostic_category == "challenge_required"


def test_conservative_configuration_defaults_and_validation(settings: Settings) -> None:
    assert settings.media.instagram.admission == InstagramAdmissionSection()
    for update in (
        {"min_spacing_seconds": 0},
        {"burst_operations": 31},
        {"unknown": 1},
        {"daily_operations": 0},
    ):
        with pytest.raises(ValidationError):
            InstagramAdmissionSection.model_validate(update)


def test_metrics_use_only_bounded_outcome_and_reason() -> None:
    metrics = MetricsRegistry()
    metrics.record_instagram_admission("paused", "challenge_required")
    assert (
        'media_bot_instagram_admission_total{outcome="paused",reason="challenge_required"} 1'
        in metrics.render()
    )


def test_failed_pause_persistence_keeps_active_quarantine(
    runtime: tuple[InstagramAdmissionService, Clock, Lock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, clock, _ = runtime

    def broken(*args: Any, **kwargs: Any) -> bool:
        raise InstagramAdmissionError("state_unavailable")

    monkeypatch.setattr(service.repository, "pause", broken)
    with (
        pytest.raises(InstagramAdmissionError, match="state_unavailable"),
        service.operation("shared", "operator"),
    ):
        raise RateLimitedError("fixture")
    assert service.repository.snapshot(now=clock.now, burst_window=3600)[0].active


def test_redis_failure_fails_closed_and_token_release_is_compare_and_delete() -> None:
    from typing import cast

    from redis import Redis
    from redis.exceptions import ConnectionError

    from telegram_media_bot.infrastructure.security.instagram_admission import (
        RedisInstagramPermitLock,
    )

    class Offline:
        def set(self, *args: object, **kwargs: object) -> bool:
            raise ConnectionError("offline fixture")

        def eval(self, *args: object) -> int:
            raise ConnectionError("offline fixture")

    lock = RedisInstagramPermitLock(cast(Redis, cast(Any, Offline())))
    with pytest.raises(InstagramAdmissionError, match="coordination_unavailable"):
        lock.acquire("opaque-fixture", "token")
    with pytest.raises(InstagramAdmissionError, match="coordination_unavailable"):
        lock.release("opaque-fixture", "token")


def test_malformed_cookie_never_emits_secret_diagnostics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n.instagram.com\tFALSE\t/\tTRUE\tnot-an-expiry\tsessionid\tsensitive-fixture-value\n",
        encoding="utf-8",
    )
    with pytest.raises(InstagramAdmissionError) as error:
        instagram_identity(None, None, path)
    captured = capsys.readouterr()
    assert "sensitive-fixture-value" not in str(error.value) + captured.out + captured.err
