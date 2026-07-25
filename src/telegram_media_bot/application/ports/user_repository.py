from __future__ import annotations

from datetime import date
from typing import Protocol

from telegram_media_bot.domain.models import JobId, UserProfile


class UserRepository(Protocol):
    def upsert_user(self, profile: UserProfile, *, started: bool = False) -> None: ...

    def record_request(self, user_id: int, day: date) -> None: ...

    def record_download_outcome(
        self,
        *,
        job_id: JobId,
        user_id: int,
        day: date,
        succeeded: bool,
        delivered_bytes: int = 0,
    ) -> bool: ...
