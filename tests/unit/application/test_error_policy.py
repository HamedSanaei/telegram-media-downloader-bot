import pytest

from telegram_media_bot.application.services.error_policy import error_category
from telegram_media_bot.domain.errors import (
    AuthenticationRequiredError,
    DeliveryError,
    DeliveryUncertainError,
    GeoRestrictedError,
    InvalidUrlError,
    JobCancelledError,
    LocalRuntimeError,
    MediaTooLargeError,
    MediaUnavailableError,
    NativeFormatUnavailableError,
    PlaylistNotAllowedError,
    PostProcessingError,
    RateLimitedError,
    UnsupportedSourceError,
)
from telegram_media_bot.domain.models import ErrorCategory


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (AuthenticationRequiredError(), ErrorCategory.AUTHENTICATION),
        (JobCancelledError(), ErrorCategory.CANCELLED),
        (LocalRuntimeError("workspace failure"), ErrorCategory.LOCAL_RUNTIME),
        (DeliveryError(), ErrorCategory.DELIVERY),
        (DeliveryUncertainError(), ErrorCategory.DELIVERY_UNCERTAIN),
        (GeoRestrictedError(), ErrorCategory.GEO_RESTRICTED),
        (InvalidUrlError(), ErrorCategory.INVALID_URL),
        (MediaUnavailableError(), ErrorCategory.MEDIA_UNAVAILABLE),
        (NativeFormatUnavailableError(), ErrorCategory.FORMAT_UNAVAILABLE),
        (PlaylistNotAllowedError(), ErrorCategory.PLAYLIST),
        (PostProcessingError(), ErrorCategory.POST_PROCESSING),
        (RateLimitedError(), ErrorCategory.RATE_LIMITED),
        (UnsupportedSourceError(), ErrorCategory.SOURCE_DISABLED),
        (MediaTooLargeError(), ErrorCategory.TOO_LARGE),
        (RuntimeError(), ErrorCategory.INTERNAL),
    ],
)
def test_errors_have_stable_operator_categories(
    error: BaseException, category: ErrorCategory
) -> None:
    assert error_category(error) is category
