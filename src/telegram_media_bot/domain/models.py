from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NewType

JobId = NewType("JobId", str)


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


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    job_id: JobId
    url: str
    mode: DownloadMode
    output_directory: Path


@dataclass(frozen=True, slots=True)
class DownloadResult:
    job_id: JobId
    media_id: str
    title: str
    source: str
    kind: MediaKind
    file_path: Path
    file_size_bytes: int
