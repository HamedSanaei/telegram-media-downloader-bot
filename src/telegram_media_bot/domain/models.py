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
    VIDEO_1080 = "video_1080"
    VIDEO_720 = "video_720"
    VIDEO_480 = "video_480"
    AUDIO_BEST = "audio_best"
    AUDIO_MP3 = "audio_mp3"


class MediaKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    PLAYLIST = "playlist"
    UNKNOWN = "unknown"


class JobKind(StrEnum):
    INSPECTION = "inspection"
    DOWNLOAD = "download"


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


class ErrorCategory(StrEnum):
    AUTHENTICATION = "authentication"
    CANCELLED = "cancelled"
    DELIVERY = "delivery"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    GEO_RESTRICTED = "geo_restricted"
    INTERNAL = "internal"
    INVALID_URL = "invalid_url"
    MEDIA_UNAVAILABLE = "media_unavailable"
    PLAYLIST = "playlist"
    POST_PROCESSING = "post_processing"
    RATE_LIMITED = "rate_limited"
    SOURCE_DISABLED = "source_disabled"
    TOO_LARGE = "too_large"


class DeliveryMethod(StrEnum):
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"


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


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    job_id: JobId
    url: str
    mode: DownloadMode
    output_directory: Path
    temp_directory: Path | None = None


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
    status_message_id: int | None = None
    source: str | None = None
    error_category: ErrorCategory | None = None
    error_summary: str | None = None
    cancel_requested: bool = False
    delivery_file_id: str | None = None
    delivery_file_unique_id: str | None = None
    attempt: int = 0


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    method: DeliveryMethod
    message_id: int
    file_id: str
    file_unique_id: str


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
