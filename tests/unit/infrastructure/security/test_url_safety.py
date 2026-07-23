import socket

import pytest

from telegram_media_bot.domain.errors import InvalidUrlError, UnsafeUrlError
from telegram_media_bot.infrastructure.security.url_safety import PublicUrlValidator


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/media",
        "http://[::1]/media",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.1/media",
        "http://metadata.google.internal/computeMetadata/v1",
        "file:///etc/passwd",
    ],
)
def test_rejects_local_private_and_metadata_urls(url: str) -> None:
    expected = InvalidUrlError if url.startswith("file:") else UnsafeUrlError
    with pytest.raises(expected):
        PublicUrlValidator().validate(url)


def test_rejects_mixed_public_private_dns_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(UnsafeUrlError):
        PublicUrlValidator().validate("https://example.com/media")


def test_normalizes_public_url_and_removes_fragment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    assert (
        PublicUrlValidator().validate(" HTTPS://Example.COM/media?q=1#fragment ")
        == "https://example.com/media?q=1"
    )


def test_rejects_credentials_before_dns() -> None:
    with pytest.raises(InvalidUrlError):
        PublicUrlValidator().validate(
            "https://user:password@example.com/media"  # pragma: allowlist secret
        )


def test_rejects_invalid_port_before_dns() -> None:
    with pytest.raises(InvalidUrlError):
        PublicUrlValidator().validate("https://example.com:99999/media")
