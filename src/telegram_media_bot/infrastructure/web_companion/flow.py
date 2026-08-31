"""Companion-side transient Instagram connection flow (T018).

Implements the ``InstagramConnectFlow`` port consumed by ``CompanionWebApp``. Password and 2FA
input arrive as bounded per-request strings, are forwarded to the connection service, and are
never stored beyond the transient phase marker. A successful login stores the encrypted session in
the vault (T017). Session-browser restart or an expired interactive flow returns generic prompts.
"""

from __future__ import annotations

import asyncio

from telegram_media_bot.application.services.instagram_connection import (
    InstagramConnectionService,
)
from telegram_media_bot.domain.web_companion import (
    BoundedMemoryFlowState,
    InstagramConnectResult,
    InstagramConnectStage,
)

_PHASE_CREDS = "creds"
_PHASE_2FA = "2fa"


class CompanionInstagramConnectionFlow:
    def __init__(
        self,
        connection: InstagramConnectionService,
        *,
        max_age_seconds: int,
        max_sessions: int,
    ) -> None:
        self._connection = connection
        self._transient = BoundedMemoryFlowState(
            max_age_seconds=max_age_seconds, max_entries=max_sessions
        )

    async def step(
        self,
        *,
        owner_user_id: int,
        session_id: str,
        input_value: str | None,
    ) -> InstagramConnectResult:
        phase = self._transient.get(f"{session_id}:phase")
        if phase is None:
            return await self._first_step(owner_user_id, session_id, input_value)
        if phase == _PHASE_2FA:
            self._transient.drop(f"{session_id}:phase")
            result = await asyncio.to_thread(
                self._connection.submit_login,
                owner_user_id,
                twofa_code=input_value,
            )
            if result.connected:
                return InstagramConnectResult(InstagramConnectStage.CONNECTED)
            return InstagramConnectResult(InstagramConnectStage.DENIED)
        return InstagramConnectResult(InstagramConnectStage.DENIED)

    async def _first_step(
        self, owner_user_id: int, session_id: str, input_value: str | None
    ) -> InstagramConnectResult:
        if not input_value:
            self._transient.set(f"{session_id}:phase", _PHASE_CREDS)
            return InstagramConnectResult(InstagramConnectStage.NEED_CREDENTIALS)
        self._transient.set(f"{session_id}:phase", _PHASE_CREDS)
        result = await asyncio.to_thread(
            self._connection.submit_login, owner_user_id, password=input_value
        )
        if result.stage is InstagramConnectStage.NEED_2FA:
            self._transient.set(f"{session_id}:phase", _PHASE_2FA)
            return InstagramConnectResult(InstagramConnectStage.NEED_2FA)
        if result.connected:
            return InstagramConnectResult(InstagramConnectStage.CONNECTED, message="")
        return InstagramConnectResult(InstagramConnectStage.DENIED)


__all__ = ["CompanionInstagramConnectionFlow"]
