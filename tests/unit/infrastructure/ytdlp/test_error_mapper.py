import errno

import pytest

from telegram_media_bot.application.services.error_policy import error_category
from telegram_media_bot.domain.errors import (
    AuthenticationRequiredError,
    DownloadFailedError,
    GeoRestrictedError,
    InvalidUrlError,
    LocalRuntimeError,
    MediaTooLargeError,
    MediaUnavailableError,
    PlaylistNotAllowedError,
    PostProcessingError,
    RateLimitedError,
)
from telegram_media_bot.domain.models import ErrorCategory
from telegram_media_bot.infrastructure.ytdlp.error_mapper import map_ytdlp_error


class _YtDlpWrapperError(Exception):
    """Mirror of yt-dlp's DownloadError cause-chain shape (``exc_info`` tuple)."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"unable to download format: {cause}")
        self.exc_info = (type(cause), cause, None)


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
        ("Unsupported URL: https://example.invalid", InvalidUrlError),
        ("Unexpected upstream error", DownloadFailedError),
    ],
)
def test_maps_upstream_errors(message: str, expected: type[Exception]) -> None:
    assert isinstance(map_ytdlp_error(Exception(message)), expected)


def test_read_only_filesystem_is_not_a_generic_download_failure() -> None:
    cause = OSError(errno.EROFS, "Read-only file system", "/app/tmp1qe12lbf.tmp")
    exc = _YtDlpWrapperError(cause)

    mapped = map_ytdlp_error(exc)

    assert isinstance(mapped, LocalRuntimeError)
    assert not isinstance(mapped, DownloadFailedError)
    assert mapped.retryable is False
    assert mapped.os_errno == errno.EROFS
    assert "[Errno 30]" in str(mapped)
    assert "read-only" in str(mapped).casefold()
    # Safe diagnostics never reproduce arbitrary absolute paths.
    assert "/app" not in str(mapped)
    assert "tmp1qe12lbf" not in str(mapped)
    assert error_category(mapped) is ErrorCategory.LOCAL_RUNTIME


def test_direct_oserror_read_only_filesystem_maps_locally() -> None:
    mapped = map_ytdlp_error(OSError(errno.EROFS, "Read-only file system"))

    assert isinstance(mapped, LocalRuntimeError)
    assert mapped.os_errno == errno.EROFS


@pytest.mark.parametrize("permission_error", [True, False])
def test_permission_failures_are_meaningful_local_runtime_errors(
    permission_error: bool,
) -> None:
    exc: OSError = (
        PermissionError("Permission denied")
        if permission_error
        else OSError(errno.EACCES, "Permission denied")
    )

    mapped = map_ytdlp_error(exc)

    assert isinstance(mapped, LocalRuntimeError)
    assert "Permission denied" in str(mapped)
    assert mapped.retryable is False
    assert error_category(mapped) is ErrorCategory.LOCAL_RUNTIME


def test_exhausted_storage_is_meaningful_local_runtime_error() -> None:
    mapped = map_ytdlp_error(OSError(errno.ENOSPC, "No space left on device"))

    assert isinstance(mapped, LocalRuntimeError)
    assert "storage is full" in str(mapped).casefold()
    assert mapped.retryable is False


def test_missing_local_path_maps_to_local_runtime_not_remote_failure() -> None:
    mapped = map_ytdlp_error(FileNotFoundError(errno.ENOENT, "No such file or directory"))

    assert isinstance(mapped, LocalRuntimeError)
    assert "FileNotFoundError" in str(mapped)
    assert "[Errno 2]" in str(mapped)


@pytest.mark.parametrize(
    "remote_failure",
    [
        TimeoutError("The read operation timed out"),
        OSError(errno.ETIMEDOUT, "Connection timed out"),
        ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"),
        OSError(errno.ECONNREFUSED, "Connection refused"),
    ],
)
def test_network_shaped_oserrors_stay_remote_failures(remote_failure: Exception) -> None:
    """Remote/network conditions keep their retryable provider-failure classification."""

    mapped = map_ytdlp_error(_YtDlpWrapperError(remote_failure))

    assert not isinstance(mapped, LocalRuntimeError)
    assert isinstance(mapped, DownloadFailedError)
    assert mapped.retryable is True


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Login required; provide cookies", AuthenticationRequiredError),
        ("HTTP Error 429: Too Many Requests", RateLimitedError),
        ("This media is unavailable", MediaUnavailableError),
    ],
)
def test_existing_remote_mappings_are_unchanged(message: str, expected: type[Exception]) -> None:
    mapped = map_ytdlp_error(Exception(message))

    assert isinstance(mapped, expected)
    assert not isinstance(mapped, LocalRuntimeError)


def test_wrapped_http_status_is_preserved_on_remote_failures() -> None:
    class HttpError(Exception):
        code = 503

    mapped = map_ytdlp_error(_YtDlpWrapperError(HttpError("HTTP Error 503: unavailable")))

    assert isinstance(mapped, MediaUnavailableError)
    assert mapped.http_status == 503
