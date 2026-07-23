from __future__ import annotations

import asyncio

from telegram_media_bot.application.ports.job_repository import JobRepository
from telegram_media_bot.application.ports.rate_limiter import RateLimiter
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    AccessDeniedError,
    PersistenceError,
    PolicyBackendError,
    UserRateLimitError,
)


class AccessPolicyService:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: JobRepository,
        rate_limiter: RateLimiter,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._rate_limiter = rate_limiter

    async def authorize_request(self, user_id: int) -> None:
        security = self._settings.security
        try:
            dynamically_blocked = await asyncio.to_thread(self._repository.is_user_blocked, user_id)
        except PersistenceError as exc:
            raise PolicyBackendError("Access-policy backend is unavailable") from exc
        if user_id in security.blocked_user_ids or dynamically_blocked:
            raise AccessDeniedError("User is blocked")
        if security.allowed_user_ids and user_id not in security.allowed_user_ids:
            raise AccessDeniedError("User is not on the allowlist")
        if not await self._rate_limiter.allow(user_id, security.requests_per_minute):
            raise UserRateLimitError("Per-user request limit exceeded")
