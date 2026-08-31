"""Instagram account connection/login-result domain model (T018).

Framework-free results for the transient web login/session-acquisition flow. Secret inputs (a
password or a 2FA/checkpoint code) are bounded strings that exist only in the companion's
transient memory and are discarded after each use; they never enter a durable command/event. The
only durable outcome is an encrypted vault store (T017) or a sanitized lifecycle state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from telegram_media_bot.domain.web_companion import InstagramConnectStage


class LoginFailureCategory(StrEnum):
    """Safe, stable categories for the transient login flow (no upstream text)."""

    NONE = "none"
    WRONG_CREDENTIALS = "wrong_credentials"
    CHALLENGE_REQUIRED = "challenge_required"
    SESSION_REJECTED = "session_rejected"
    NOT_AVAILABLE = "not_available"


@dataclass(frozen=True, slots=True)
class InstagramLoginResult:
    """Result of one transient login step. ``session_bytes`` is set only on CONNECTED."""

    stage: InstagramConnectStage
    failure: LoginFailureCategory = LoginFailureCategory.NONE
    session_bytes: bytes | None = None

    @property
    def connected(self) -> bool:
        return self.stage is InstagramConnectStage.CONNECTED


__all__ = [
    "InstagramLoginResult",
    "LoginFailureCategory",
]
