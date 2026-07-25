from __future__ import annotations

from typing import Protocol

from telegram_media_bot.domain.models import RequiredChannel


class MembershipChecker(Protocol):
    async def missing_channels(
        self,
        user_id: int,
        channels: tuple[RequiredChannel, ...],
        *,
        force_refresh: bool = False,
    ) -> tuple[RequiredChannel, ...]: ...

    async def close(self) -> None: ...
