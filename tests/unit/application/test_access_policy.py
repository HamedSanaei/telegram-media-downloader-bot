from typing import Any, cast

import pytest

from telegram_media_bot.application.ports.job_repository import JobRepository
from telegram_media_bot.application.ports.rate_limiter import RateLimiter
from telegram_media_bot.application.services.access_policy import AccessPolicyService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    AccessDeniedError,
    PersistenceError,
    PolicyBackendError,
    UserRateLimitError,
)


class FakeRepository:
    blocked = False
    unavailable = False

    def is_user_blocked(self, _user_id: int) -> bool:
        if self.unavailable:
            raise PersistenceError("database unavailable")
        return self.blocked


class FakeRateLimiter:
    allowed = True

    async def allow(self, _user_id: int, _limit: int) -> bool:
        return self.allowed

    async def close(self) -> None:
        return None


def service(
    settings: Settings, repository: FakeRepository, limiter: FakeRateLimiter
) -> AccessPolicyService:
    return AccessPolicyService(
        settings=settings,
        repository=cast(JobRepository, cast(Any, repository)),
        rate_limiter=cast(RateLimiter, limiter),
    )


async def test_access_policy_allows_normal_user(settings: Settings) -> None:
    await service(settings, FakeRepository(), FakeRateLimiter()).authorize_request(42)


async def test_access_policy_enforces_static_dynamic_and_rate_limits(settings: Settings) -> None:
    raw = settings.model_dump()
    raw["security"]["allowed_user_ids"] = [42]
    configured = Settings.model_validate(raw)
    with pytest.raises(AccessDeniedError):
        await service(configured, FakeRepository(), FakeRateLimiter()).authorize_request(7)

    repository = FakeRepository()
    repository.blocked = True
    with pytest.raises(AccessDeniedError):
        await service(settings, repository, FakeRateLimiter()).authorize_request(42)

    limiter = FakeRateLimiter()
    limiter.allowed = False
    with pytest.raises(UserRateLimitError):
        await service(settings, FakeRepository(), limiter).authorize_request(42)


async def test_access_policy_fails_closed_when_repository_is_unavailable(
    settings: Settings,
) -> None:
    repository = FakeRepository()
    repository.unavailable = True
    with pytest.raises(PolicyBackendError):
        await service(settings, repository, FakeRateLimiter()).authorize_request(42)
