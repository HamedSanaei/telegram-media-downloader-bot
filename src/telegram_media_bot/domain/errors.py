from telegram_media_bot.domain.failures import FailureStage
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
        #: Pipeline-stage hint (FailureStage) attached by the failing adapter when it knows
        #: where the failure happened. Workers honor specialized exception-class stages first
        #: and use this hint only when no better classification exists.
        self.failure_stage: FailureStage | None = None


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


class LocalRuntimeError(MediaBotError):
    """A local application-environment failure, never a remote media failure.

    Unwritable temporary workspaces, permission problems, and exhausted local storage must
    not masquerade as provider download failures: they are infrastructure conditions the
    operator has to resolve locally, so they are terminal and non-retryable.
    """

    retryable = False

    def __init__(
        self,
        message: str = "",
        *,
        os_errno: int | None = None,
        source: str | None = None,
        http_status: int | None = None,
        adapter: str | None = None,
        extractor: str | None = None,
    ) -> None:
        super().__init__(
            message,
            source=source,
            http_status=http_status,
            adapter=adapter,
            extractor=extractor,
        )
        #: Errno of the underlying OS error when one was observed; safe numeric evidence
        #: that keeps diagnostics useful without exposing filesystem paths or secrets.
        self.os_errno = os_errno


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


class DeliveryUncertainError(DeliveryError):
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


class EntitlementDeniedError(MediaBotError):
    """Base class for controlled entitlement denials (non-retryable)."""


class EntitlementInactiveError(EntitlementDeniedError):
    """The user has no subscription/entitlement history at all."""


class EntitlementExpiredError(EntitlementDeniedError):
    """All valid paid time has elapsed before the protected request's acceptance instant."""


class EntitlementCancelledError(EntitlementDeniedError):
    """The user's subscription is cancelled and no longer authorizes new requests."""


class EntitlementNoValidGrantError(EntitlementDeniedError):
    """Every grant was reversed/satisfied and no valid paid time remains."""


class EntitlementCapabilityMissingError(EntitlementDeniedError):
    """The user has active time but the requested capability is not covered by those grants."""


class EntitlementBackendError(MediaBotError):
    """The entitlement backend is unavailable; authorization FAILS CLOSED (never Free/VIP)."""

    retryable = True


class DuplicateEntitlementGrantError(EntitlementDeniedError):
    """A grant with the same (source_type, source_reference) already exists."""


class EntitlementGrantNotFoundError(MediaBotError):
    """The referenced grant does not exist or was already purged externally."""


class PaymentError(MediaBotError):
    """Base class for controlled billing/payment failures."""


class PaymentOrderNotFoundError(PaymentError):
    """The referenced payment order does not exist."""


class PaymentOrderExpiredError(PaymentError):
    """The payment order is past its deterministic UTC expiry."""


class PaymentAmountMismatchError(PaymentError):
    """Provider-verified amount disagrees with the order snapshot; fails closed."""


class PaymentCurrencyMismatchError(PaymentError):
    """Provider-verified currency disagrees with the order snapshot; fails closed."""


class PaymentProviderMismatchError(PaymentError):
    """Provider-verified provider identity disagrees with the order's provider."""


class PaymentOrderMismatchError(PaymentError):
    """Provider-verified order reference/mapping disagrees with the expected order."""


class PaymentTransactionNotClaimedError(PaymentError):
    """The expected provider transaction was not previously claimed/persisted."""


class PaymentTransactionReplayError(PaymentError):
    """The same provider transaction was already processed economically."""


class PaymentAlreadyConfirmedError(PaymentError):
    """The payment order is already in a paid/confirmed state."""


class PaymentAlreadyRefundedError(PaymentError):
    """The payment was already refunded/reversed."""


class InvalidPaymentTransitionError(PaymentError):
    """A payment order state transition is not allowed."""


class PaymentBackendError(PaymentError):
    """The payment backend is unavailable; billing FAILS CLOSED (never grants)."""

    retryable = True


class ProviderNotRegisteredError(PaymentError):
    """No gateway adapter is registered for the requested provider."""


class CheckoutUnavailableError(PaymentError):
    """The provider could not create an external checkout (safe, non-economic)."""


class CredentialError(MediaBotError):
    """Base class for controlled owner-bound Instagram credential failures (T017+)."""


class CredentialNotFoundError(CredentialError):
    """No credential row exists for the owner (or it has no ciphertext)."""


class CredentialWrongOwnerError(CredentialError):
    """A caller attempted to access another user's credential."""


class CredentialGenerationMismatchError(CredentialError):
    """The requested generation does not match the current row generation."""


class CredentialRevokedError(CredentialError):
    """The credential was revoked and is no longer usable."""


class CredentialDisconnectedError(CredentialError):
    """The credential is disconnected and holds no ciphertext."""


class CredentialExpiredError(CredentialError):
    """The credential session expired and requires re-login."""


class CredentialChallengeRequiredError(CredentialError):
    """Instagram requires a checkpoint/2FA challenge before the session is usable."""


class CredentialLeaseBusyError(CredentialError):
    """Another job currently holds the expiring lease for this credential/generation."""


class CredentialLeaseNotFoundError(CredentialError):
    """The supplied lease is not held/active."""


class CredentialDecryptError(CredentialError):
    """Ciphertext could not be authenticated/decrypted for this owner/generation/key."""


class CredentialKeyMissingError(CredentialError):
    """The envelope's key ID is not in the available key ring (key rotation gap)."""


class CredentialMaterializationError(CredentialError):
    """Local plaintext materialization into the job workspace failed."""


class CredentialResolutionError(MediaBotError):
    """Base class for controlled credential-resolution failures (T019+)."""


class CredentialUnavailableError(CredentialResolutionError):
    """No usable credential can currently be resolved for the requested policy."""


class OperatorAttestationError(MediaBotError):
    """Base class for public-only operator attestation failures (ADR-034, T019+)."""


class OperatorUnattestedError(OperatorAttestationError):
    """The operator Instagram account has not been attested as public-only, or attestation is invalid."""


class OperatorAttestationStaleError(OperatorAttestationError):
    """The Instagram cookie records changed after attestation; attestation no longer applies."""
