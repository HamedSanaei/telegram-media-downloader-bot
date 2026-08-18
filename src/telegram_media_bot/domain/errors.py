from telegram_media_bot.domain.models import RequiredChannel


class MediaBotError(Exception):
    """Base class for controlled project errors."""

    retryable = False

    def __init__(
        self,
        message: str = "",
        *,
        source: str | None = None,
        http_status: int | None = None,
        adapter: str | None = None,
        extractor: str | None = None,
    ) -> None:
        super().__init__(message)
        #: Provider attribution attached once an adapter has processed the request, so a
        #: terminal job failure never reports an unknown source after the fact.
        self.source = source
        #: Upstream HTTP status when the adapter could observe one; never invented.
        self.http_status = http_status
        #: Engine adapter attribution (for example "gallery-dl" or "yt-dlp").
        self.adapter = adapter
        #: Extractor/subcategory attribution when the adapter can observe one.
        self.extractor = extractor
        #: Adapter fallback chain when the router fell back to another engine.
        self.fallback_chain: tuple[str, ...] | None = None
        self.fallback_reason: str | None = None


class ConfigurationError(MediaBotError):
    pass


class InvalidUrlError(MediaBotError):
    pass


class UnsupportedSourceError(MediaBotError):
    pass


class AuthenticationRequiredError(MediaBotError):
    pass


class GeoRestrictedError(MediaBotError):
    pass


class RateLimitedError(MediaBotError):
    retryable = True


class MediaUnavailableError(MediaBotError):
    pass


class NativeFormatUnavailableError(MediaUnavailableError):
    """The requested native codec/container plan is not offered by the source."""


class MediaTooLargeError(MediaBotError):
    pass


class PlaylistNotAllowedError(MediaBotError):
    pass


class DownloadFailedError(MediaBotError):
    retryable = True


class PostProcessingError(MediaBotError):
    pass


class TranscodeRejectedError(PostProcessingError):
    """A requested heavy conversion was rejected before FFmpeg started."""


class AccessDeniedError(MediaBotError):
    pass


class MembershipRequiredError(AccessDeniedError):
    def __init__(self, channels: tuple[RequiredChannel, ...]) -> None:
        super().__init__("Membership in all configured channels is required")
        self.channels = channels


class UserRateLimitError(MediaBotError):
    pass


class PolicyBackendError(MediaBotError):
    retryable = True


class UnsafeUrlError(InvalidUrlError):
    pass


class SelectionExpiredError(MediaBotError):
    pass


class SelectionOwnershipError(MediaBotError):
    pass


class JobNotFoundError(MediaBotError):
    pass


class JobCancelledError(MediaBotError):
    pass


class DeliveryError(MediaBotError):
    retryable = True


class DeliveryTooLargeError(MediaBotError):
    pass


class DeliveryUncertainError(MediaBotError):
    pass


class BatchDeliveryFailedError(MediaBotError):
    """Every item of a collection delivery failed; the batch summary was already sent."""


class LocalBotApiError(MediaBotError):
    pass


class PersistenceError(MediaBotError):
    retryable = True


class UsageChartFontError(MediaBotError):
    """The bundled usage-chart font is missing, invalid, or incomplete."""


class GalleryDlUnavailableError(MediaBotError):
    """The isolated gallery-dl executable is missing or unusable."""


class GalleryDlUnsupportedUrlError(MediaBotError):
    pass


class GalleryDlAuthenticationRequiredError(AuthenticationRequiredError):
    pass


class GalleryDlCookiesExpiredError(AuthenticationRequiredError):
    """Configured source cookies were rejected or have expired."""


class InstagramCookiesUnavailableError(MediaBotError):
    """Instagram cookie health definitively blocks an authenticated bulk collection job."""


class GalleryDlRateLimitedError(RateLimitedError):
    pass


class GalleryDlExtractionError(DownloadFailedError):
    pass


class GalleryDlOutputChangedError(MediaBotError):
    pass


class GalleryDlNoImagesError(GalleryDlUnsupportedUrlError):
    """Inspection succeeded but the single post has no image assets."""


class ImageValidationError(MediaBotError):
    pass


class CollectionTooLargeError(MediaTooLargeError):
    pass


class ImageFormatUnsupportedError(ImageValidationError):
    pass


class CookieManagementError(MediaBotError):
    """Base class for controlled cookie-management failures."""


class InvalidCookieFileError(CookieManagementError):
    pass


class EmptyCookieFileError(InvalidCookieFileError):
    pass


class UnsupportedCookieDomainsError(InvalidCookieFileError):
    pass


class CookieFileTooLargeError(InvalidCookieFileError):
    pass


class CookieStoreUnavailableError(CookieManagementError):
    pass


class CookieStoreWriteError(CookieManagementError):
    pass
