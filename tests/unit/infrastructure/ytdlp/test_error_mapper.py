import pytest

from telegram_media_bot.domain.errors import (
    AuthenticationRequiredError,
    DownloadFailedError,
    GeoRestrictedError,
    MediaTooLargeError,
    MediaUnavailableError,
    PlaylistNotAllowedError,
    PostProcessingError,
    RateLimitedError,
)
from telegram_media_bot.infrastructure.ytdlp.error_mapper import map_ytdlp_error


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Login required; provide cookies", AuthenticationRequiredError),
        ("This video is geo restricted in your country", GeoRestrictedError),
        ("HTTP Error 429: Too Many Requests", RateLimitedError),
        ("File is larger than max-filesize", MediaTooLargeError),
        ("Playlist not allowed", PlaylistNotAllowedError),
        ("ffmpeg postprocess failed", PostProcessingError),
        ("This media is unavailable", MediaUnavailableError),
        ("Requested format is not available", MediaUnavailableError),
        ("Unexpected upstream error", DownloadFailedError),
    ],
)
def test_maps_upstream_errors(message: str, expected: type[Exception]) -> None:
    assert isinstance(map_ytdlp_error(Exception(message)), expected)
