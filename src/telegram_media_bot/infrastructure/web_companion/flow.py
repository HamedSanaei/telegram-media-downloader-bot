"""Companion-side transient Instagram connection flow (T018/T025).

Implements the ``InstagramConnectFlow`` port consumed by ``CompanionWebApp``. Username, password,
and 2FA arrive as transient bounded values, are forwarded to the connection service, and are
never stored beyond the transient phase marker; the acquirer's success (not the fake) stores the
encrypted session in the vault (T017). Browser restart or an expired interactive flow returns
generic prompts. No credential is ever logged, persisted, or echoed.
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


def _parse_input(input_value: object) -> tuple[str | None, str | None, str | None]:
    if isinstance(input_value, dict):
        username = input_value.get("username")
        password = input_value.get("password")
        code = input_value.get("code")
        return (
            username if isinstance(username, str) and username else None,
            password if isinstance(password, str) and password else None,
            code if isinstance(code, str) and code else None,
        )
    if isinstance(input_value, str) and input_value:
        # Backward-compatible raw password submission.
        return None, input_value, None
    return None, None, None


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
        input_value: object | None,
    ) -> InstagramConnectResult:
        phase = self._transient.get(f"{session_id}:phase")
        if phase is None:
            return await self._first_step(owner_user_id, session_id, input_value)
        if phase == _PHASE_2FA:
            self._transient.drop(f"{session_id}:phase")
            _, _, code = _parse_input(input_value)
            if not code:
                self._transient.set(f"{session_id}:phase", _PHASE_2FA)
                return InstagramConnectResult(InstagramConnectStage.NEED_2FA)
            result = await asyncio.to_thread(
                self._connection.submit_login,
                owner_user_id,
                twofa_code=code,
            )
            if result.connected:
                return InstagramConnectResult(InstagramConnectStage.CONNECTED)
            return InstagramConnectResult(InstagramConnectStage.DENIED)
        return InstagramConnectResult(InstagramConnectStage.DENIED)

    async def _first_step(
        self, owner_user_id: int, session_id: str, input_value: object | None
    ) -> InstagramConnectResult:
        username, password, _code = _parse_input(input_value)
        if not username or not password:
            self._transient.set(f"{session_id}:phase", _PHASE_CREDS)
            return InstagramConnectResult(InstagramConnectStage.NEED_CREDENTIALS)
        self._transient.set(f"{session_id}:phase", _PHASE_CREDS)
        result = await asyncio.to_thread(
            self._connection.submit_login,
            owner_user_id,
            username=username,
            password=password,
        )
        if result.stage is InstagramConnectStage.NEED_2FA:
            self._transient.set(f"{session_id}:phase", _PHASE_2FA)
            return InstagramConnectResult(InstagramConnectStage.NEED_2FA)
        if result.connected:
            return InstagramConnectResult(InstagramConnectStage.CONNECTED, message="")
        return InstagramConnectResult(InstagramConnectStage.DENIED)


__all__ = ["CompanionInstagramConnectionFlow"]
