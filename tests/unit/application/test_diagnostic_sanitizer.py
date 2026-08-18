from __future__ import annotations

import pytest

from telegram_media_bot.application.services.diagnostic_sanitizer import (
    redact_secrets,
    sanitize_exception_message,
    sanitize_url,
)


def test_sanitize_url_keeps_scheme_host_and_safe_path() -> None:
    assert sanitize_url("https://example.com/video/abc123") == "https://example.com/video/abc123"


def test_sanitize_url_strips_query_parameters() -> None:
    url = "https://cdn.example.com/media.mp4?sig=abc123&token=secret&Expires=1700000000"
    assert sanitize_url(url) == "https://cdn.example.com/media.mp4"


def test_sanitize_url_keeps_explicitly_safe_parameters() -> None:
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30&list=PLabc&token=secret"
    assert sanitize_url(url) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30&list=PLabc"


def test_sanitize_url_never_reproduces_credentials() -> None:
    url = "https://user:password@example.com/video"
    result = sanitize_url(url)
    assert "user" not in result
    assert "password" not in result


def test_sanitize_url_rejects_non_http() -> None:
    assert sanitize_url("file:///etc/passwd") == "<invalid-url>"


@pytest.mark.parametrize(
    ("message", "forbidden"),
    [
        ("sessionid=IGSuperSecretCookieValue123", "IGSuperSecretCookieValue123"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload", "eyJhbGciOiJIUzI1NiJ9.payload"),
        ("api_hash=abcdef0123456789abcdef0123456789", "abcdef0123456789abcdef0123456789"),
        ("proxy=http://user:supersecret@proxy.example:8080", "supersecret"),
        ("token=abc1234567890", "abc1234567890"),
        ("access_token=xyzwxyzxyzxyzxyzxyz", "xyzwxyzxyzxyzxyzxyz"),
        ("password=CorrectHorseBatteryStaple123", "CorrectHorseBatteryStaple123"),
        ("x-api-key: 1234567890abcdef1234567890abcdef", "1234567890abcdef1234567890abcdef"),
        (
            "bot token 123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij",
            "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij",
        ),
    ],
)
def test_synthetic_secret_bearing_messages_are_redacted(message: str, forbidden: str) -> None:
    redacted = redact_secrets(message)
    assert forbidden not in redacted


def test_signed_cdn_query_secrets_are_redacted_in_text() -> None:
    text = "download from https://cdn.example.com/media.mp4?X-Amz-Signature=abc&token=zzz"
    redacted = redact_secrets(text)
    assert "X-Amz-Signature=abc" not in redacted
    assert "token=zzz" not in redacted


def test_sanitize_exception_message_returns_none_for_empty() -> None:
    assert sanitize_exception_message("") is None
    assert sanitize_exception_message("   ") is None


def test_sanitize_exception_message_bounds_and_cleans() -> None:
    message = "error\0with\x00control chars" + "x" * 2000
    result = sanitize_exception_message(message)
    assert result is not None
    assert len(result) <= 512
    assert "\x00" not in result


def test_sanitize_exception_message_redacts_secrets() -> None:
    result = sanitize_exception_message("HTTP 403 for sessionid=SecretCookieValue99999")
    assert result is not None
    assert "SecretCookieValue99999" not in result
    assert "403" in result
