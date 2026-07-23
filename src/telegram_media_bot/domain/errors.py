class MediaBotError(Exception):
    """Base class for controlled project errors."""

    retryable = False


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


class MediaTooLargeError(MediaBotError):
    pass


class PlaylistNotAllowedError(MediaBotError):
    pass


class DownloadFailedError(MediaBotError):
    retryable = True


class PostProcessingError(MediaBotError):
    pass


class AccessDeniedError(MediaBotError):
    pass


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


class PersistenceError(MediaBotError):
    retryable = True
