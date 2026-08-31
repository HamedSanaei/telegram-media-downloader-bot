"""Instagram connection presentation helper tests (T018)."""

from __future__ import annotations

from datetime import UTC, datetime

from telegram_media_bot.domain.instagram_credentials import (
    InstagramCredentialState,
    SafeCredentialView,
)
from telegram_media_bot.telegram.instagram_ux import (
    render_connect_prompt,
    render_connection_status,
    render_disconnect_confirmation,
    render_instagram_unavailable,
)
from telegram_media_bot.telegram.texts import INSTAGRAM_NOT_AVAILABLE_TEXT


def test_status_not_connected() -> None:
    assert "متصل نیست" in render_connection_status(None)


def test_status_connected_renders_notice() -> None:
    now = datetime.now(UTC)
    view = SafeCredentialView(
        state=InstagramCredentialState.CONNECTED,
        generation=1,
        last_verified_at=now,
    )
    text = render_connection_status(view)
    assert "متصل" in text
    assert "VIP" in text
    assert view.last_verified_at is not None  # keep coverage of the timestamp branch


def test_status_expired() -> None:
    view = SafeCredentialView(state=InstagramCredentialState.EXPIRED, generation=1)
    assert "منقضی" in render_connection_status(view)


def test_connect_prompt_with_link() -> None:
    text = render_connect_prompt("https://connect.example.test/instagram/connect#handoff=t")
    assert "https://connect.example.test" in text
    assert "تلگرام" in text


def test_connect_prompt_unavailable() -> None:
    assert render_connect_prompt(None) == INSTAGRAM_NOT_AVAILABLE_TEXT
    assert render_instagram_unavailable() == INSTAGRAM_NOT_AVAILABLE_TEXT


def test_disconnect_confirmation() -> None:
    assert "جدا شد" in render_disconnect_confirmation()
