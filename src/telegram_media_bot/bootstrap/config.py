from __future__ import annotations

import os
import string
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit
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
    max_upload_size_mb: int = Field(default=49, ge=1, le=2048)
    caption_template: str = "{title}\nمنبع: {source}"
    filename_max_length: int = Field(default=96, ge=16, le=180)
    local_api_base_url: str | None = None
    local_api_is_local: bool = False
    progress_min_interval_seconds: float = Field(default=3.0, ge=1.0, le=60.0)
    progress_min_percent_delta: float = Field(default=5.0, ge=1.0, le=100.0)

    @field_validator("caption_template")
    @classmethod
    def validate_caption_template(cls, value: str) -> str:
        allowed = {"title", "source"}
        fields = {
            field_name
            for _, field_name, _, _ in string.Formatter().parse(value)
            if field_name is not None
        }
        if not fields <= allowed:
            raise ValueError("caption_template only supports {title} and {source}")
        if len(value) > 512:
            raise ValueError("caption_template is too long")
        return value

    @model_validator(mode="after")
    def validate_local_api(self) -> TelegramSection:
        if self.local_api_is_local and not self.local_api_base_url:
            raise ValueError("local_api_base_url is required when local_api_is_local is true")
        return self

    @field_validator("local_api_base_url")
    @classmethod
    def validate_local_api_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("local_api_base_url must be an absolute HTTP(S) URL")
        return value.rstrip("/")


class RedisSection(StrictModel):
    url: str = "redis://redis:6379/0"
    queue_name: str = "media-downloads"


class QueueSection(StrictModel):
    max_jobs: int = Field(default=3, ge=1, le=100)
    job_timeout_seconds: int = Field(default=1800, ge=30)
    max_tries: int = Field(default=2, ge=1, le=10)
    keep_result_seconds: int = Field(default=3600, ge=0)
    retry_delay_seconds: int = Field(default=15, ge=1, le=3600)


class StorageSection(StrictModel):
    root_directory: Path = Field(
        default_factory=lambda: Path("/data"), json_schema_extra={"default": "/data"}
    )
    downloads_directory: Path = Field(
        default_factory=lambda: Path("downloads"), json_schema_extra={"default": "downloads"}
    )
    temp_directory: Path = Field(
        default_factory=lambda: Path("temp"), json_schema_extra={"default": "temp"}
    )
    state_directory: Path = Field(
        default_factory=lambda: Path("state"), json_schema_extra={"default": "state"}
    )
    delete_after_upload: bool = True
    orphan_grace_seconds: int = Field(default=300, ge=30)
    job_retention_days: int = Field(default=30, ge=1, le=3650)

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
    enabled_modes: tuple[DownloadMode, ...] = tuple(DownloadMode)
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

    @field_validator("enabled_modes")
    @classmethod
    def validate_enabled_modes(cls, values: tuple[DownloadMode, ...]) -> tuple[DownloadMode, ...]:
        if not values:
            raise ValueError("At least one semantic download mode must be enabled")
        if len(values) != len(set(values)):
            raise ValueError("enabled_modes must not contain duplicates")
        if DownloadMode.BEST not in values:
            raise ValueError("enabled_modes must include best as a universal fallback")
        return values


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
    javascript_runtime: Literal["deno", "node", "bun", "quickjs"] = "deno"


class SecuritySection(StrictModel):
    allowed_user_ids: frozenset[int] = frozenset()
    blocked_user_ids: frozenset[int] = frozenset()
    requests_per_minute: int = Field(default=5, ge=1, le=1000)
    reject_private_network_urls: bool = True

    @model_validator(mode="after")
    def validate_user_sets(self) -> SecuritySection:
        overlap = self.allowed_user_ids & self.blocked_user_ids
        if overlap:
            raise ValueError(f"Users cannot be both allowed and blocked: {sorted(overlap)}")
        return self


class PersistenceSection(StrictModel):
    database_filename: str = "jobs.sqlite3"
    selection_ttl_seconds: int = Field(default=600, ge=60, le=86400)
    cleanup_interval_seconds: int = Field(default=60, ge=30, le=86400)

    @field_validator("database_filename")
    @classmethod
    def validate_database_filename(cls, value: str) -> str:
        candidate = Path(value)
        if candidate.name != value or candidate.suffix not in {".sqlite", ".sqlite3", ".db"}:
            raise ValueError("database_filename must be a plain SQLite filename")
        return value


class ObservabilitySection(StrictModel):
    health_host: str = "0.0.0.0"
    health_port: int = Field(default=8080, ge=1, le=65535)
    telegram_readiness_check: bool = True
    metrics_enabled: bool = True


class Settings(StrictModel):
    app: AppSection
    telegram: TelegramSection
    redis: RedisSection
    queue: QueueSection
    storage: StorageSection
    media: MediaSection
    yt_dlp: YtDlpSection
    security: SecuritySection
    persistence: PersistenceSection
    observability: ObservabilitySection

    def database_path(self) -> Path:
        return self.storage.state_path() / self.persistence.database_filename

    def validate_runtime(self, *, require_token: bool) -> None:
        if require_token and self.telegram.bot_token in {"CHANGE_ME", ""}:
            raise ConfigurationError("telegram.bot_token must be set in config.yaml")
        self.storage.downloads_path()
        self.storage.temp_path()
        self.storage.state_path()
        self.database_path()

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
