"""Deterministic fake Instagram session acquirer (T018, test/operator use).

Provides a network-free, reproducible path behind the `InstagramSessionAcquirer` port: correct
password yields a CONNECTED result with opaque fake session cookie bytes; a configured
``challenge_required`` requires a 2FA code; wrong credentials produce a stable DENIED result. It
is the composition default until an operator supplies a real upstream adapter.
"""

from __future__ import annotations

from telegram_media_bot.domain.instagram_connection import (
    InstagramLoginResult,
    LoginFailureCategory,
)
from telegram_media_bot.domain.web_companion import InstagramConnectStage

_FAKE_SESSION = (
    b"# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\tfake"
)


class FakeInstagramSessionAcquirer:
    def __init__(
        self,
        *,
        challenge_required: bool = False,
        reject_always: bool = False,
    ) -> None:
        self._challenge = challenge_required
        self._reject = reject_always

    def step(
        self,
        *,
        password: str | None,
        twofa_code: str | None,
    ) -> InstagramLoginResult:
        if self._reject:
            return InstagramLoginResult(
                InstagramConnectStage.DENIED, LoginFailureCategory.WRONG_CREDENTIALS
            )
        # A login is valid when a password (first step) or a 2FA code (checkpoint completion) is
        # present; submitting neither is always denied. Secrets are never retained by the acquirer.
        if not password and not twofa_code:
            return InstagramLoginResult(
                InstagramConnectStage.DENIED, LoginFailureCategory.WRONG_CREDENTIALS
            )
        if self._challenge and not twofa_code:
            return InstagramLoginResult(
                InstagramConnectStage.NEED_2FA, LoginFailureCategory.CHALLENGE_REQUIRED
            )
        return InstagramLoginResult(
            InstagramConnectStage.CONNECTED,
            LoginFailureCategory.NONE,
            session_bytes=_FAKE_SESSION,
        )


__all__ = ["FakeInstagramSessionAcquirer"]
