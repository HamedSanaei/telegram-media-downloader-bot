"""Companion-side transient Instagram connection flow (T018/T025).

Implements the ``InstagramConnectFlow`` port consumed by ``CompanionWebApp``. Username, password,
and 2FA arrive as transient bounded values, are forwarded to the connection service, and are
never stored beyond the transient phase marker; the acquirer's success (not the fake) stores the
encrypted session in the vault (T017). Browser restart or an expired interactive flow returns
generic prompts. No credential is ever logged, persisted, or echoed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import monotonic

from telegram_media_bot.application.services.instagram_connection import (
    InstagramConnectionService,
)
from telegram_media_bot.domain.web_companion import (
    InstagramConnectResult,
    InstagramConnectStage,
)

_PHASE_CREDS = "creds"
_PHASE_2FA = "2fa"


@dataclass(slots=True, repr=False)
class _Attempt:
    owner: int
    connection: InstagramConnectionService
    expires_at: float
    username: str | None = None
    phase: str = _PHASE_CREDS
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
        if max_age_seconds < 1 or max_sessions < 1:
            raise ValueError("flow bounds must be positive")
        self._max_age_seconds = max_age_seconds
        self._max_sessions = max_sessions
        self._attempts: dict[str, _Attempt] = {}

    async def step(
        self,
        *,
        owner_user_id: int,
        session_id: str,
        input_value: object | None,
    ) -> InstagramConnectResult:
        now = monotonic()
        expired = session_id in self._attempts and self._attempts[session_id].expires_at <= now
        self._attempts = {
            key: value for key, value in self._attempts.items() if value.expires_at > now
        }
        if expired:
            return InstagramConnectResult(InstagramConnectStage.DENIED)
        attempt = self._attempts.get(session_id)
        if attempt is None:
            if len(self._attempts) >= self._max_sessions:
                return InstagramConnectResult(InstagramConnectStage.NOT_AVAILABLE)
            attempt = _Attempt(
                owner_user_id, self._connection.new_login_attempt(), now + self._max_age_seconds
            )
            self._attempts[session_id] = attempt
        if attempt.owner != owner_user_id or attempt.lock.locked():
            return InstagramConnectResult(InstagramConnectStage.DENIED)
        async with attempt.lock:
            username, password, code = _parse_input(input_value)
            if attempt.phase == _PHASE_2FA:
                if not code:
                    return InstagramConnectResult(InstagramConnectStage.NEED_2FA)
                username, password = attempt.username, None
            else:
                if not username or not password:
                    return InstagramConnectResult(InstagramConnectStage.NEED_CREDENTIALS)
                attempt.username = username
                code = None
            try:
                result = await asyncio.to_thread(
                    attempt.connection.acquire_login,
                    username=username,
                    password=password,
                    twofa_code=code,
                )
                # A timed-out/cancelled provider thread can finish, but never writes the vault.
                if (
                    monotonic() >= attempt.expires_at
                    or self._attempts.get(session_id) is not attempt
                ):
                    self._attempts.pop(session_id, None)
                    return InstagramConnectResult(InstagramConnectStage.DENIED)
                if result.stage is InstagramConnectStage.NEED_2FA:
                    attempt.phase = _PHASE_2FA
                    return InstagramConnectResult(InstagramConnectStage.NEED_2FA)
                if result.connected and result.session_bytes:
                    await asyncio.to_thread(attempt.connection.store_login, owner_user_id, result)
                    self._attempts.pop(session_id, None)
                    return InstagramConnectResult(InstagramConnectStage.CONNECTED)
                # A rejected login may be retried with corrected credentials, not a stuck phase.
                self._attempts.pop(session_id, None)
                return InstagramConnectResult(InstagramConnectStage.DENIED)
            except BaseException:
                self._attempts.pop(session_id, None)
                raise


__all__ = ["CompanionInstagramConnectionFlow"]
