from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import NewType

JobId = NewType("JobId", str)
SelectionToken = NewType("SelectionToken", str)


class DownloadMode(StrEnum):
    BEST = "best"
    BEST_ORIGINAL = "best_original"
    VIDEO_2160 = "video_2160"
    VIDEO_1440 = "video_1440"
    VIDEO_1080 = "video_1080"
    VIDEO_720 = "video_720"
    VIDEO_480 = "video_480"
    AUDIO_BEST = "audio_best"
    AUDIO_MP3 = "audio_mp3"
    IMAGE_ORIGINAL = "image_original"
    IMAGES_ORIGINAL = "images_original"
    ALL_ORIGINAL_MEDIA = "all_original_media"
    IMAGES_ONLY = "images_only"
    VIDEOS_ONLY = "videos_only"
    VIDEO_ORIGINAL = "video_original"
    IMAGES_ZIP = "images_zip"
    YOUTUBE_THUMBNAIL = "youtube_thumbnail"
    SOUNDCLOUD_ARTWORK = "soundcloud_artwork"
    #: Bulk collection: every active Story of an Instagram account, in source order.
    INSTAGRAM_ALL_STORIES = "instagram_all_stories"
    #: Bulk collection: every media item inside one Instagram Highlight, in source order.
    INSTAGRAM_HIGHLIGHT = "instagram_highlight"


#: Collection modes are delivered as per-item batches with a final summary, and their
#: aggregate size is bounded by the collection limit rather than the single-file limit.
COLLECTION_MODES = frozenset(
    {
        DownloadMode.INSTAGRAM_ALL_STORIES,
        DownloadMode.INSTAGRAM_HIGHLIGHT,
    }
)


class OutputContainer(StrEnum):
    MP4 = "mp4"
    WEBM = "webm"
    MP3 = "mp3"


class ContainerPolicy(StrEnum):
    NATIVE_ONLY = "native_only"
    GUARANTEED = "guaranteed"
    EXPLICIT_TRANSCODE = "explicit_transcode"


class MediaProcessingKind(StrEnum):
    DIRECT = "direct"
    REMUX = "remux"
    TRANSCODE = "transcode"


class Mp4NativeFallback(StrEnum):
    LOWER_RESOLUTION = "lower_resolution"
    FAIL = "fail"


class NativeVideoCodec(StrEnum):
    AV1 = "av1"
    H264 = "h264"
    VP9 = "vp9"


def normalize_container_policy(
    mode: DownloadMode,
    policy: ContainerPolicy,
) -> ContainerPolicy:
    """Keep original-quality downloads outside every codec transcoding contract."""
    if mode is DownloadMode.BEST_ORIGINAL:
        return ContainerPolicy.NATIVE_ONLY
    return policy


class MediaKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    PLAYLIST = "playlist"
    UNKNOWN = "unknown"


class SizeConfidence(StrEnum):
    EXACT = "exact"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class DeliveryStage(StrEnum):
    PACKAGING = "packaging"
    UPLOADING = "uploading"
    FINALIZING = "finalizing"


class JobKind(StrEnum):
    INSPECTION = "inspection"
    DOWNLOAD = "download"
    HIGHLIGHT_TRAY = "highlight_tray"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    DELIVERING = "delivering"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DELIVERY_UNCERTAIN = "delivery_uncertain"

    @property
    def terminal(self) -> bool:
        return self in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.DELIVERY_UNCERTAIN,
        }


class RecoveryDecision(StrEnum):
    REQUEUE_ABANDONED = "requeue_abandoned"
    QUARANTINE_DELIVERY = "quarantine_delivery"
    SKIP_CANCELLED = "skip_cancelled"


class QueueJobStatus(StrEnum):
    QUEUED = "queued"
    DEFERRED = "deferred"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


class ErrorCategory(StrEnum):
    AUTHENTICATION = "authentication"
    CANCELLED = "cancelled"
    DELIVERY = "delivery"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    FORMAT_UNAVAILABLE = "format_unavailable"
    GEO_RESTRICTED = "geo_restricted"
    INTERNAL = "internal"
    INVALID_URL = "invalid_url"
    MEDIA_UNAVAILABLE = "media_unavailable"
    PLAYLIST = "playlist"
    POST_PROCESSING = "post_processing"
    RATE_LIMITED = "rate_limited"
    SOURCE_DISABLED = "source_disabled"
    TOO_LARGE = "too_large"
    GALLERY_UNAVAILABLE = "gallery_unavailable"
    GALLERY_OUTPUT_CHANGED = "gallery_output_changed"
    INVALID_IMAGE = "invalid_image"
    COLLECTION_TOO_LARGE = "collection_too_large"
    UNSUPPORTED_GALLERY_URL = "unsupported_gallery_url"
    GALLERY_COOKIES_EXPIRED = "gallery_cookies_expired"
    GALLERY_EXTRACTION = "gallery_extraction"
    #: Local application-environment failure (workspace/filesystem/runtime), never remote.
    LOCAL_RUNTIME = "local_runtime"


class DeliveryMethod(StrEnum):
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"
    PHOTO = "photo"


class ImageDeliveryMode(StrEnum):
    PHOTO = "photo"
    DOCUMENT = "document"


class DeliveryProvider(StrEnum):
    BOT_API = "bot_api"
    MULTIPART = "multipart"


class DeliveryItemStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class MediaInfo:
    media_id: str
    title: str
    source: str
    kind: MediaKind
    webpage_url: str
    uploader: str | None = None
    duration_seconds: int | None = None
    thumbnail_url: str | None = None
    item_count: int | None = None
    estimated_size_bytes: int | None = None
    format_options: tuple[MediaFormatOption, ...] = ()
    assets: tuple[MediaAsset, ...] = ()


@dataclass(frozen=True, slots=True)
class MediaAsset:
    index: int
    asset_id: str
    kind: MediaKind
    extension: str
    mime_type: str | None
    source_post_id: str
    provider: str
    width: int | None = None
    height: int | None = None
    duration_seconds: int | None = None
    size_bytes: int | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class MediaFormatOption:
    mode: DownloadMode
    container: OutputContainer | None = None
    container_policy: ContainerPolicy = ContainerPolicy.NATIVE_ONLY
    requires_transcode: bool = False
    processing_kind: MediaProcessingKind = MediaProcessingKind.DIRECT
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    is_hdr: bool = False
    size_bytes: int | None = None
    size_confidence: SizeConfidence = SizeConfidence.UNKNOWN
    selection_reason: str | None = None
    fallback_reason: str | None = None
    selected_format_ids: tuple[str, ...] = ()
    video_codec: str | None = None
    audio_codec: str | None = None
    dynamic_range: str | None = None
    video_size_bytes: int | None = None
    audio_size_bytes: int | None = None
    quality_score: float | None = None


@dataclass(frozen=True, slots=True)
class NativeOptionView:
    option_id: str
    mode: DownloadMode
    container: OutputContainer
    actual_width: int | None
    actual_height: int | None
    actual_fps: float | None
    video_codec: str | None
    audio_codec: str | None
    dynamic_range: str | None
    size_bytes: int | None
    size_is_approximate: bool
    quality_score: float | None
    selected_format_ids: tuple[str, ...]
    transcode_required: bool
    processing_kind: MediaProcessingKind
    display_label: str


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    job_id: JobId
    url: str
    mode: DownloadMode
    output_directory: Path
    temp_directory: Path | None = None
    container: OutputContainer | None = None
    container_policy: ContainerPolicy = ContainerPolicy.NATIVE_ONLY
    native_video_codec: NativeVideoCodec | None = None
    selected_format_ids: tuple[str, ...] = ()
    allow_collection: bool = False
    image_delivery_mode: ImageDeliveryMode | None = None
    #: Per-job batch safeguard (collection modes only); caps the number of assets.
    max_assets: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "container_policy",
            normalize_container_policy(self.mode, self.container_policy),
        )


@dataclass(frozen=True, slots=True)
class DownloadArtifact:
    file_path: Path
    file_size_bytes: int
    kind: MediaKind
    mime_type: str | None = None
    title: str | None = None
    inline_video_streamable: bool = False
    source_index: int | None = None


@dataclass(frozen=True, slots=True)
class DownloadResult:
    job_id: JobId
    media_id: str
    title: str
    source: str
    kind: MediaKind
    file_path: Path
    file_size_bytes: int
    duration_seconds: int | None = None
    mime_type: str | None = None
    artifacts: tuple[DownloadArtifact, ...] = ()
    inline_video_streamable: bool = False
    image_delivery_mode: ImageDeliveryMode | None = None

    @property
    def delivery_artifacts(self) -> tuple[DownloadArtifact, ...]:
        if self.artifacts:
            return self.artifacts
        return (
            DownloadArtifact(
                file_path=self.file_path,
                file_size_bytes=self.file_size_bytes,
                kind=self.kind,
                mime_type=self.mime_type,
                title=self.title,
                inline_video_streamable=self.inline_video_streamable,
                source_index=1,
            ),
        )

    @property
    def total_file_size_bytes(self) -> int:
        return sum(item.file_size_bytes for item in self.delivery_artifacts)


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    job_id: JobId
    status: str
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed_bytes_per_second: float | None = None
    eta_seconds: int | None = None

    @property
    def percent(self) -> float | None:
        if not self.total_bytes or self.total_bytes <= 0:
            return None
        return min(100.0, max(0.0, self.downloaded_bytes * 100 / self.total_bytes))


@dataclass(frozen=True, slots=True)
class DeliveryProgressEvent:
    job_id: JobId
    stage: DeliveryStage
    transferred_bytes: int = 0
    total_bytes: int | None = None
    item_ordinal: int = 1
    item_count: int = 1
    item_transferred_bytes: int = 0
    item_size_bytes: int | None = None
    elapsed_seconds: float = 0.0

    @property
    def percent(self) -> float | None:
        if not self.total_bytes or self.total_bytes <= 0:
            return None
        return min(100.0, max(0.0, self.transferred_bytes * 100 / self.total_bytes))

    @property
    def item_percent(self) -> float | None:
        if not self.item_size_bytes or self.item_size_bytes <= 0:
            return None
        return min(
            100.0,
            max(0.0, self.item_transferred_bytes * 100 / self.item_size_bytes),
        )


@dataclass(frozen=True, slots=True)
class SelectionRecord:
    token: SelectionToken
    owner_user_id: int
    chat_id: int
    media: MediaInfo
    allowed_modes: tuple[DownloadMode, ...]
    created_at: datetime
    expires_at: datetime

    @property
    def expired(self) -> bool:
        return self.expires_at <= datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class HighlightItem:
    highlight_id: str
    title: str
    item_count: int


@dataclass(frozen=True, slots=True)
class HighlightTrayRecord:
    """One browsable Instagram highlight tray, stored so callback state stays opaque."""

    token: SelectionToken
    owner_user_id: int
    chat_id: int
    username: str
    highlights: tuple[HighlightItem, ...]
    created_at: datetime
    expires_at: datetime

    @property
    def expired(self) -> bool:
        return self.expires_at <= datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: JobId
    kind: JobKind
    status: JobStatus
    chat_id: int
    user_id: int
    url: str
    mode: DownloadMode | None
    idempotency_key: str
    created_at: datetime
    updated_at: datetime
    container: OutputContainer | None = None
    container_policy: ContainerPolicy = ContainerPolicy.NATIVE_ONLY
    native_video_codec: NativeVideoCodec | None = None
    selected_format_ids: tuple[str, ...] = ()
    image_delivery_mode: ImageDeliveryMode | None = None
    status_message_id: int | None = None
    source: str | None = None
    error_category: ErrorCategory | None = None
    error_summary: str | None = None
    cancel_requested: bool = False
    delivery_file_id: str | None = None
    delivery_file_unique_id: str | None = None
    attempt: int = 0
    #: Original URL classification captured at creation (for example "profile", "story").
    url_classification: str | None = None


@dataclass(frozen=True, slots=True)
class JobCancellationResult:
    accepted: bool
    previous_status: JobStatus | None
    final_status: JobStatus | None
    already_cancelled: bool = False


@dataclass(frozen=True, slots=True)
class JobAbortResult:
    previous_status: QueueJobStatus
    final_status: QueueJobStatus
    abort_requested: bool
    redis_keys_removed: int


@dataclass(frozen=True, slots=True)
class JobRecoveryRecord:
    job: JobRecord
    previous_status: JobStatus
    decision: RecoveryDecision


@dataclass(frozen=True, slots=True)
class UserProfile:
    user_id: int
    private_chat_id: int | None
    username: str | None
    first_name: str
    last_name: str | None
    language_code: str | None
    is_premium: bool | None


@dataclass(frozen=True, slots=True)
class RequiredChannel:
    chat_id: int
    title: str
    join_url: str


@dataclass(frozen=True, slots=True)
class DeliveryItemReceipt:
    method: DeliveryMethod
    message_id: int
    file_id: str
    file_unique_id: str
    provider: DeliveryProvider = DeliveryProvider.BOT_API
    ordinal: int = 1


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    items: tuple[DeliveryItemReceipt, ...]

    def __init__(
        self,
        method: DeliveryMethod | None = None,
        message_id: int | None = None,
        file_id: str | None = None,
        file_unique_id: str | None = None,
        *,
        items: tuple[DeliveryItemReceipt, ...] | None = None,
    ) -> None:
        if items is None:
            if method is None or message_id is None or file_id is None or file_unique_id is None:
                raise ValueError("A delivery receipt requires an item or legacy receipt fields")
            items = (
                DeliveryItemReceipt(
                    method=method,
                    message_id=message_id,
                    file_id=file_id,
                    file_unique_id=file_unique_id,
                ),
            )
        if not items:
            raise ValueError("A delivery receipt must contain at least one item")
        object.__setattr__(self, "items", items)

    @property
    def primary(self) -> DeliveryItemReceipt:
        return self.items[0]

    @property
    def method(self) -> DeliveryMethod:
        return self.primary.method

    @property
    def message_id(self) -> int:
        return self.primary.message_id

    @property
    def file_id(self) -> str:
        return self.primary.file_id

    @property
    def file_unique_id(self) -> str:
        return self.primary.file_unique_id


@dataclass(frozen=True, slots=True)
class DeliveryItemRecord:
    job_id: JobId
    ordinal: int
    provider: DeliveryProvider
    status: DeliveryItemStatus
    method: DeliveryMethod
    recipient_message_id: int | None = None
    file_id: str | None = None
    file_unique_id: str | None = None


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    name: str
    healthy: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class HealthReport:
    checks: tuple[ComponentHealth, ...]
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def healthy(self) -> bool:
        return all(check.healthy for check in self.checks)


@dataclass(frozen=True, slots=True)
class JobCounts:
    queued: int = 0
    running: int = 0
    retrying: int = 0
    failed: int = 0
