from __future__ import annotations

import pytest

from telegram_media_bot.application.services.audit_sanitizer import (
    UnsafeAuditPayloadError,
    sanitize_audit_caption,
    sanitize_audit_message,
)


def test_caption_preserves_literal_markup_unicode_and_line_breaks() -> None:
    caption = "توضیح <b>متن</b> & **عنوان** 😀\n\nخط دوم\n🤖 @DownloadKadeBot"
    assert sanitize_audit_caption(caption) == caption


def test_caption_normalizes_horizontal_whitespace_without_flattening_lines() -> None:
    assert sanitize_audit_caption("  متن\t  اول\r\n\r\nخط\u00a0 دوم  ") == "متن اول\n\nخط دوم"


@pytest.mark.parametrize("caption", ["", " \t "])
def test_known_empty_caption_is_valid(caption: str) -> None:
    assert sanitize_audit_caption(caption) == ""


def test_caption_retains_full_description_for_transport_budgeting() -> None:
    caption = "شرح 😀\n" * 500 + "پایان"
    assert sanitize_audit_caption(caption) == caption


@pytest.mark.parametrize(
    ("caption", "secret"),
    [
        ("عنوان\nAuthorization: Bearer synthetic-secret\nend", "synthetic-secret"),
        ("عنوان\nCookie: sessionid=first-secret; csrftoken=second-secret", "second-secret"),
        ("عنوان\npassword=synthetic-password\nend", "synthetic-password"),
        ("عنوان\n123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_123\nend", "ABCDEFGHIJKLMNOPQRSTUVWXYZ_123"),
        (
            "عنوان\nhttps://proxy-user:proxy-pass@proxy.example\nend",
            "proxy-pass",
        ),  # pragma: allowlist secret
        ('عنوان\n{"payment_secret": "synthetic-payment"}', "synthetic-payment"),
    ],
)
def test_caption_redacts_secrets_before_persistence(caption: str, secret: str) -> None:
    sanitized = sanitize_audit_caption(caption)
    assert secret not in sanitized
    assert "redacted" in sanitized
    assert sanitized.startswith("عنوان\n")
    assert sanitize_audit_caption(sanitized) == sanitized


@pytest.mark.parametrize(
    "caption",
    [
        'عنوان\nTraceback (most recent call last):\n  File "secret.py"',
        r"عنوان C:\Users\operator\vault.key",
        "عنوان /run/secrets/vault",
        "عنوان\n.instagram.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tsecret\nend",
    ],
)
def test_caption_rejects_entire_unsafe_structured_payload(caption: str) -> None:
    with pytest.raises(UnsafeAuditPayloadError):
        sanitize_audit_caption(caption)


def test_message_sanitizer_retains_nonempty_flattened_bounded_contract() -> None:
    assert sanitize_audit_message(" متن\tیک\n\nخط دوم ") == "متن یک خط دوم"
    assert sanitize_audit_message("x" * 2500) == "x" * 2000
    with pytest.raises(UnsafeAuditPayloadError, match="empty"):
        sanitize_audit_message(" \n\t ")
