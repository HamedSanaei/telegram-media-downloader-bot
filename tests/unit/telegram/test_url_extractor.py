from telegram_media_bot.telegram.url_extractor import extract_first_url


def test_extracts_first_http_url() -> None:
    assert extract_first_url("watch https://example.com/a now") == "https://example.com/a"


def test_returns_none_without_url() -> None:
    assert extract_first_url("hello") is None
