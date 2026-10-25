"""Release metadata and import-time package version must agree."""

from importlib.metadata import version

from telegram_media_bot import __version__
from telegram_media_bot.versions import versions_equal


def test_distribution_matches_runtime_version() -> None:
    assert versions_equal(version("telegram-media-downloader-bot"), __version__)
