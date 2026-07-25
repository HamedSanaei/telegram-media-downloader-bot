from __future__ import annotations

import asyncio

from telegram_media_bot.application.ports.job_repository import JobRepository
from telegram_media_bot.application.ports.membership import MembershipChecker
from telegram_media_bot.application.ports.rate_limiter import RateLimiter
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    AccessDeniedError,
    MembershipRequiredError,
    PersistenceError,
    PolicyBackendError,
    UserRateLimitError,
)
from telegram_media_bot.domain.models import RequiredChannel


class AccessPolicyService:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: JobRepository,
        rate_limiter: RateLimiter,
        membership_checker: MembershipChecker | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._rate_limiter = rate_limiter
        self._membership_checker = membership_checker

    async def authorize_request(
        self,
        user_id: int,
        *,
        force_membership_refresh: bool = False,
        consume_rate_limit: bool = True,
    ) -> None:
        security = self._settings.security
        try:
            dynamically_blocked = await asyncio.to_thread(self._repository.is_user_blocked, user_id)
        except PersistenceError as exc:
            raise PolicyBackendError("Access-policy backend is unavailable") from exc
        if user_id in security.blocked_user_ids or dynamically_blocked:
            raise AccessDeniedError("User is blocked")
        if security.allowed_user_ids and user_id not in security.allowed_user_ids:
            raise AccessDeniedError("User is not on the allowlist")
        required = self._settings.telegram.required_channels
        if required.enabled and user_id not in self._settings.telegram.admin_ids:
            if self._membership_checker is None:
                raise PolicyBackendError("Membership checker is unavailable")
            channels = tuple(
                RequiredChannel(
                    chat_id=channel.chat_id,
                    title=channel.title,
                    join_url=channel.join_url,
                )
                for channel in required.channels
            )
            missing = await self._membership_checker.missing_channels(
                user_id,
                channels,
                force_refresh=force_membership_refresh,
            )
            if missing:
                raise MembershipRequiredError(missing)
        if consume_rate_limit and not await self._rate_limiter.allow(
            user_id, security.requests_per_minute
        ):
            raise UserRateLimitError("Per-user request limit exceeded")
