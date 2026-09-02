"""Instagram transient login/session-acquisition port (T018).

A replaceable infrastructure adapter performs the actual provider login and returns a normalized
project-owned result. The default composition registers a deterministic fake for tests and operator
use; a real upstream adapter is operator-supplied behind this port and must fail closed. Secrets
are bounded plain strings passed by the companion flow and discarded by the caller after use.
"""

from __future__ import annotations

from typing import Protocol

from telegram_media_bot.domain.instagram_connection import InstagramLoginResult


class InstagramSessionAcquirer(Protocol):
    """Transient login session acquirer (infrastructure, replaceable, fail-closed)."""

    def step(
        self,
        *,
        username: str | None,
        password: str | None,
        twofa_code: str | None,
    ) -> InstagramLoginResult:
        """Advance the login: submit the login identifier and/or a 2FA/checkpoint code.

        All three values are transient bounded strings: the caller releases them after the call
        and nothing here may persist, log, or label them. Returns a normalized result;
        ``session_bytes`` carries the user-session cookie material exactly once and is encrypted
        into the vault by the caller before returning.
        """
