from telegram_media_bot.domain.errors import (
    AuthenticationRequiredError,
    BatchDeliveryFailedError,
    DeliveryError,
    DeliveryTooLargeError,
    DeliveryUncertainError,
    GalleryDlCookiesExpiredError,
    GeoRestrictedError,
    InstagramCookiesUnavailableError,
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


def error_category(exc: BaseException) -> ErrorCategory:
    if isinstance(exc, GalleryDlCookiesExpiredError):
        return ErrorCategory.GALLERY_COOKIES_EXPIRED
    if isinstance(exc, (AuthenticationRequiredError, InstagramCookiesUnavailableError)):
        return ErrorCategory.AUTHENTICATION
    if isinstance(exc, JobCancelledError):
        return ErrorCategory.CANCELLED
    if isinstance(exc, LocalRuntimeError):
        return ErrorCategory.LOCAL_RUNTIME
    if isinstance(exc, DeliveryUncertainError):
        return ErrorCategory.DELIVERY_UNCERTAIN
    if isinstance(exc, (DeliveryError, DeliveryTooLargeError, BatchDeliveryFailedError)):
        return ErrorCategory.DELIVERY
    if isinstance(exc, GeoRestrictedError):
        return ErrorCategory.GEO_RESTRICTED
    if isinstance(exc, InvalidUrlError):
        return ErrorCategory.INVALID_URL
    if isinstance(exc, NativeFormatUnavailableError):
        return ErrorCategory.FORMAT_UNAVAILABLE
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
