import os

import pytest

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.infrastructure.ytdlp.engine import YtDlpEngine

_SOURCE_FIXTURES = (
    ("youtube", "CONTRACT_YOUTUBE_URL"),
    ("soundcloud", "CONTRACT_SOUNDCLOUD_URL"),
    ("instagram", "CONTRACT_INSTAGRAM_URL"),
    ("twitter", "CONTRACT_TWITTER_URL"),
    ("pinterest", "CONTRACT_PINTEREST_URL"),
    ("tiktok", "CONTRACT_TIKTOK_URL"),
)


@pytest.mark.contract
@pytest.mark.parametrize(("expected_source", "environment_key"), _SOURCE_FIXTURES)
def test_operator_supplied_public_url_inspection(
    settings: Settings, expected_source: str, environment_key: str
) -> None:
    if os.environ.get("RUN_CONTRACT_TESTS") != "1":
        pytest.skip("Set RUN_CONTRACT_TESTS=1 to enable external contract tests")
    url = os.environ.get(environment_key)
    if not url:
        pytest.skip(f"Set {environment_key} to an operator-maintained safe public fixture")

    info = YtDlpEngine(settings).inspect(url)
    assert info.media_id
    assert info.title
    assert info.source == expected_source
