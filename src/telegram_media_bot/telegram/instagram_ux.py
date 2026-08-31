"""Sanitized Telegram Instagram-connection presentation helpers (T018).

Pure rendering used by the connection commands and the T023 `/vip` dashboard. No username, cookie,
ciphertext, password, 2FA value, or upstream error text ever appears in any rendered string.
"""

from __future__ import annotations

from telegram_media_bot.domain.instagram_credentials import (
    InstagramCredentialState,
    SafeCredentialView,
)
from telegram_media_bot.telegram.texts import (
    INSTAGRAM_CONNECT_PROMPT_TEXT,
    INSTAGRAM_CONNECT_STATUS_FREE_NOTICE,
    INSTAGRAM_DISCONNECTED_TEXT,
    INSTAGRAM_NOT_AVAILABLE_TEXT,
)

_STATE_LABELS: dict[InstagramCredentialState, str] = {
    InstagramCredentialState.CONNECTED: "متصل",
    InstagramCredentialState.EXPIRED: "منقضی‌شده",
    InstagramCredentialState.CHALLENGE_REQUIRED: "نیازمند تأیید دومرحله‌ای",
    InstagramCredentialState.REVOKED: "ابطال‌شده",
    InstagramCredentialState.DISCONNECTED: "جدا شده",
}


def render_connection_status(view: SafeCredentialView | None) -> str:
    """Render a safe Persian status line for a credential view (or a not-connected notice)."""
    if view is None:
        return "وضعیت: متصل نیست"
    label = _STATE_LABELS.get(view.state, view.state.value)
    parts = [f"وضعیت: {label}"]
    if view.last_verified_at is not None:
        parts.append(f"آخرین تأیید: {view.last_verified_at.isoformat()}")
    if view.state is InstagramCredentialState.CONNECTED:
        parts.append(INSTAGRAM_CONNECT_STATUS_FREE_NOTICE)
    return "\n".join(parts)


def render_connect_prompt(connect_link: str | None) -> str:
    """Render a safe connect prompt, or an unavailable notice when no link can be minted."""
    if not connect_link:
        return INSTAGRAM_NOT_AVAILABLE_TEXT
    return INSTAGRAM_CONNECT_PROMPT_TEXT + f"\n\n{connect_link}"


def render_disconnect_confirmation() -> str:
    return INSTAGRAM_DISCONNECTED_TEXT


def render_instagram_unavailable() -> str:
    return INSTAGRAM_NOT_AVAILABLE_TEXT


def render_vip_dashboard(
    *,
    active: bool,
    authorized_until: str | None,
    plans: tuple[str, ...],
    credential: SafeCredentialView | None,
    payment_available: bool,
) -> str:
    """Render the owner-bound `/vip` overview without exposing provider or secret data."""
    status = "فعال" if active else "رایگان/غیرفعال"
    lines = [f"⭐ VIP: {status}"]
    if authorized_until:
        lines.append(f"اعتبار تا: {authorized_until}")
    lines.append(render_connection_status(credential))
    if plans and payment_available:
        lines.append("پلن‌های فعال: " + "، ".join(plans))
    elif not payment_available:
        lines.append("خرید VIP در حال حاضر در دسترس نیست.")
    else:
        lines.append("پلن فعالی برای خرید ثبت نشده است.")
    return "\n".join(lines)


__all__ = [
    "render_connect_prompt",
    "render_connection_status",
    "render_disconnect_confirmation",
    "render_instagram_unavailable",
    "render_vip_dashboard",
]
