from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from telegram_media_bot.domain.errors import ConfigurationError
from telegram_media_bot.domain.models import DownloadMode


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AppSection(StrictModel):
    environment: Literal["development", "test", "production"] = "production"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    language: str = "fa"
    timezone: str = "Asia/Tehran"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {value}") from exc
        return value


class TelegramSection(StrictModel):
    bot_token: str = Field(min_length=1)
    admin_ids: tuple[int, ...] = ()
    support_username: str | None = None
    polling_timeout_seconds: int = Field(default=30, ge=5, le=60)
    upload_as_document: bool = True


class RedisSection(StrictModel):
    url: str = "redis://redis:6379/0"
    queue_name: str = "media-downloads"


class QueueSection(StrictModel):
    max_jobs: int = Field(default=3, ge=1, le=100)
    job_timeout_seconds: int = Field(default=1800, ge=30)
    max_tries: int = Field(default=2, ge=1, le=10)
    keep_result_seconds: int = Field(default=3600, ge=0)


class StorageSection(StrictModel):
    root_directory: Path = Path("/data")
    downloads_directory: Path = Path("downloads")
    temp_directory: Path = Path("temp")
    state_directory: Path = Path("state")
    delete_after_upload: bool = True

    def downloads_path(self) -> Path:
        return self._under_root(self.downloads_directory)

    def temp_path(self) -> Path:
        return self._under_root(self.temp_directory)

    def state_path(self) -> Path:
        return self._under_root(self.state_directory)

    def _under_root(self, child: Path) -> Path:
        root = self.root_directory.expanduser().resolve()
        target = child.expanduser()
        if not target.is_absolute():
            target = root / target
        resolved = target.resolve()
        if not resolved.is_relative_to(root):
            raise ConfigurationError(f"Storage path escapes root: {child}")
        return resolved


class FormatSection(StrictModel):
    best: str
    video_1080: str
    video_720: str
    video_480: str
    audio_best: str
    audio_mp3: str

    def for_mode(self, mode: DownloadMode) -> str:
        return cast(str, getattr(self, mode.value))


class MediaSection(StrictModel):
    enabled_sources: frozenset[str]
    default_mode: DownloadMode = DownloadMode.BEST
    allow_playlists: bool = False
    playlist_max_items: int = Field(default=20, ge=1, le=500)
    max_file_size_mb: int = Field(default=49, ge=1)
    max_duration_seconds: int = Field(default=14400, ge=1)
    formats: FormatSection

    @field_validator("enabled_sources")
    @classmethod
    def normalize_sources(cls, values: frozenset[str]) -> frozenset[str]:
        normalized = frozenset(item.strip().casefold() for item in values if item.strip())
        if not normalized:
            raise ValueError("At least one source must be enabled")
        return normalized


class YtDlpSection(StrictModel):
    cookies_file: Path | None = None
    proxy: str | None = None
    socket_timeout_seconds: int = Field(default=30, ge=1)
    retries: int = Field(default=5, ge=0)
    fragment_retries: int = Field(default=10, ge=0)
    concurrent_fragments: int = Field(default=4, ge=1, le=32)
    extractor_retries: int = Field(default=3, ge=0)
    restrict_filenames: bool = True
    write_thumbnail: bool = False
    embed_metadata: bool = True
    embed_thumbnail: bool = False
    audio_format: str = "mp3"
    audio_quality: str = "192"
    user_agent: str | None = None


class SecuritySection(StrictModel):
    allowed_user_ids: frozenset[int] = frozenset()
    blocked_user_ids: frozenset[int] = frozenset()
    requests_per_minute: int = Field(default=5, ge=1, le=1000)
    reject_private_network_urls: bool = True

    @model_validator(mode="after")
    def validate_user_sets(self) -> "SecuritySection":
        overlap = self.allowed_user_ids & self.blocked_user_ids
        if overlap:
            raise ValueError(f"Users cannot be both allowed and blocked: {sorted(overlap)}")
        return self


class Settings(StrictModel):
    app: AppSection
    telegram: TelegramSection
    redis: RedisSection
    queue: QueueSection
    storage: StorageSection
    media: MediaSection
    yt_dlp: YtDlpSection
    security: SecuritySection

    def validate_runtime(self, *, require_token: bool) -> None:
        if require_token and self.telegram.bot_token in {"CHANGE_ME", ""}:
            raise ConfigurationError("telegram.bot_token must be set in config.yaml")
        self.storage.downloads_path()
        self.storage.temp_path()
        self.storage.state_path()

    def create_runtime_directories(self) -> None:
        for path in (
            self.storage.downloads_path(),
            self.storage.temp_path(),
            self.storage.state_path(),
        ):
            path.mkdir(parents=True, exist_ok=True)


def default_config_path() -> Path:
    return Path(os.environ.get("APP_CONFIG_PATH", "config.yaml"))


def load_settings(path: Path | str | None = None, *, require_token: bool = False) -> Settings:
    config_path = Path(path) if path is not None else default_config_path()
    try:
        with config_path.open("r", encoding="utf-8") as file:
            raw = yaml.safe_load(file)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Configuration file not found: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML in {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigurationError("Configuration root must be a mapping")

    try:
        settings = Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(str(exc)) from exc

    settings.validate_runtime(require_token=require_token)
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings(require_token=True)
