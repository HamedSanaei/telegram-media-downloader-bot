class MediaBotError(Exception):
    """Base class for controlled project errors."""


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
    pass


class MediaUnavailableError(MediaBotError):
    pass


class MediaTooLargeError(MediaBotError):
    pass


class PlaylistNotAllowedError(MediaBotError):
    pass


class DownloadFailedError(MediaBotError):
    pass


class PostProcessingError(MediaBotError):
    pass
