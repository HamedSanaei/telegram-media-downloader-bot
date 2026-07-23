from telegram_media_bot.domain.errors import (
    AuthenticationRequiredError,
    DeliveryError,
    DeliveryTooLargeError,
    DeliveryUncertainError,
    GeoRestrictedError,
    InvalidUrlError,
    JobCancelledError,
    MediaTooLargeError,
    MediaUnavailableError,
    PlaylistNotAllowedError,
    PostProcessingError,
    RateLimitedError,
    UnsupportedSourceError,
)
from telegram_media_bot.domain.models import ErrorCategory


def error_category(exc: BaseException) -> ErrorCategory:
    if isinstance(exc, AuthenticationRequiredError):
        return ErrorCategory.AUTHENTICATION
    if isinstance(exc, JobCancelledError):
        return ErrorCategory.CANCELLED
    if isinstance(exc, DeliveryUncertainError):
        return ErrorCategory.DELIVERY_UNCERTAIN
    if isinstance(exc, (DeliveryError, DeliveryTooLargeError)):
        return ErrorCategory.DELIVERY
    if isinstance(exc, GeoRestrictedError):
        return ErrorCategory.GEO_RESTRICTED
    if isinstance(exc, InvalidUrlError):
        return ErrorCategory.INVALID_URL
    if isinstance(exc, MediaUnavailableError):
        return ErrorCategory.MEDIA_UNAVAILABLE
    if isinstance(exc, PlaylistNotAllowedError):
        return ErrorCategory.PLAYLIST
    if isinstance(exc, PostProcessingError):
        return ErrorCategory.POST_PROCESSING
    if isinstance(exc, RateLimitedError):
        return ErrorCategory.RATE_LIMITED
    if isinstance(exc, UnsupportedSourceError):
        return ErrorCategory.SOURCE_DISABLED
    if isinstance(exc, MediaTooLargeError):
        return ErrorCategory.TOO_LARGE
    return ErrorCategory.INTERNAL
