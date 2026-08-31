from __future__ import annotations

import os
import string
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from telegram_media_bot.domain.errors import ConfigurationError
from telegram_media_bot.domain.models import DownloadMode, Mp4NativeFallback


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


class LocalBotApiMigrationSection(StrictModel):
    auto_logout_from_cloud: Literal[False] = False
    state_file: Path = Field(
        default_factory=lambda: Path("./data/state/telegram-api-migration.json")
    )


class LocalBotApiSection(StrictModel):
    enabled: bool = False
    mode: Literal["managed", "external"] = "external"
    executable: Path | None = None
    api_id: int | None = Field(default=None, ge=0)
    api_hash: SecretStr | None = None
    host: str = "127.0.0.1"
    port: int = Field(default=8081, ge=1, le=65535)
    local_mode: bool = True
    working_directory: Path = Field(default_factory=lambda: Path("./data/telegram-bot-api"))
    temp_directory: Path = Field(default_factory=lambda: Path("./data/telegram-bot-api/temp"))
    log_file: Path = Field(
        default_factory=lambda: Path("./data/telegram-bot-api/telegram-bot-api.log")
    )
    verbosity: int = Field(default=2, ge=0, le=10)
    auto_start: bool = True
    lifecycle_owner: Literal["application", "service"] = "application"
    startup_timeout_seconds: int = Field(default=30, ge=1, le=300)
    shutdown_timeout_seconds: int = Field(default=20, ge=1, le=300)
    migration: LocalBotApiMigrationSection = Field(default_factory=LocalBotApiMigrationSection)

    @model_validator(mode="after")
    def validate_managed_credentials(self) -> LocalBotApiSection:
        if not self.enabled or self.mode != "managed":
            return self
        if self.executable is None:
            raise ValueError("local_bot_api.executable is required in managed mode")
        if not self.api_id:
            raise ValueError("local_bot_api.api_id must be set in managed mode")
        if self.api_hash is None or self.api_hash.get_secret_value() in {"", "CHANGE_ME"}:
            raise ValueError("local_bot_api.api_hash must be set in managed mode")
        return self


class RequiredChannelSection(StrictModel):
    chat_id: int
    title: str = Field(min_length=1, max_length=128)
    join_url: str

    @field_validator("join_url")
    @classmethod
    def validate_join_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname not in {"t.me", "telegram.me"}:
            raise ValueError("join_url must be an absolute Telegram HTTPS URL")
        return value.rstrip("/")


class RequiredChannelsSection(StrictModel):
    enabled: bool = False
    positive_cache_ttl_seconds: int = Field(default=300, ge=1, le=86400)
    negative_cache_ttl_seconds: int = Field(default=30, ge=1, le=3600)
    channels: tuple[RequiredChannelSection, ...] = ()

    @model_validator(mode="after")
    def validate_channels(self) -> RequiredChannelsSection:
        if self.enabled and not self.channels:
            raise ValueError("required_channels.channels cannot be empty when enabled")
        chat_ids = [channel.chat_id for channel in self.channels]
        if len(chat_ids) != len(set(chat_ids)):
            raise ValueError("required channel chat_id values must be unique")
        return self


class TelegramLoggerSection(StrictModel):
    enabled: bool = False
    channels: tuple[int, ...] = ()
    alerts_enabled: bool = False
    submission_mirror_enabled: bool = False
    operator_privacy_attested: bool = False
    privacy_notice_version: str = Field(default="logger-v1", min_length=1, max_length=32)

    @field_validator("privacy_notice_version")
    @classmethod
    def validate_privacy_notice_version(cls, value: str) -> str:
        if not all(
            character.isascii() and (character.isalnum() or character in "_.:-")
            for character in value
        ):
            raise ValueError("telegram.logger.privacy_notice_version must be a safe identifier")
        return value

    @model_validator(mode="after")
    def validate_logger(self) -> TelegramLoggerSection:
        if len(self.channels) != len(set(self.channels)):
            raise ValueError("telegram.logger.channels must be unique")
        if any(chat_id > -1000000000000 for chat_id in self.channels):
            raise ValueError("telegram.logger.channels must use numeric -100... channel IDs")
        return self


class TelegramSection(StrictModel):
    bot_token: SecretStr
    admin_ids: tuple[int, ...] = ()
    support_username: str | None = None
    polling_timeout_seconds: int = Field(default=30, ge=5, le=60)
    upload_as_document: bool = True
    max_upload_size_mb: int = Field(default=49, ge=1, le=1900)
    upload_timeout_seconds: int = Field(default=14400, ge=60, le=86400)
    upload_chunk_size_kb: int = Field(default=1024, ge=64, le=4096)
    upload_heartbeat_interval_seconds: int = Field(default=30, ge=5, le=300)
    caption_template: str = "{title}\nمنبع: {source}\nدریافت‌شده با @{bot_username}"  # noqa: RUF001
    filename_max_length: int = Field(default=96, ge=16, le=180)
    local_api_base_url: str | None = None
    local_api_is_local: bool = False
    local_bot_api: LocalBotApiSection = Field(default_factory=LocalBotApiSection)
    required_channels: RequiredChannelsSection = Field(default_factory=RequiredChannelsSection)
    logger: TelegramLoggerSection = Field(default_factory=TelegramLoggerSection)
    progress_min_interval_seconds: float = Field(default=3.0, ge=1.0, le=60.0)
    progress_min_percent_delta: float = Field(default=5.0, ge=1.0, le=100.0)

    @field_validator("caption_template")
    @classmethod
    def validate_caption_template(cls, value: str) -> str:
        allowed = {"title", "source", "bot_username"}
        fields = {
            field_name
            for _, field_name, _, _ in string.Formatter().parse(value)
            if field_name is not None
        }
        if not fields <= allowed:
            raise ValueError("caption_template only supports {title}, {source}, and {bot_username}")
        if len(value) > 512:
            raise ValueError("caption_template is too long")
        return value

    @model_validator(mode="after")
    def validate_local_api(self) -> TelegramSection:
        if self.local_api_is_local and not self.local_api_base_url:
            raise ValueError("local_api_base_url is required when local_api_is_local is true")
        if self.local_bot_api.enabled and not self.local_api_base_url:
            raise ValueError("local_api_base_url is required when local_bot_api is enabled")
        if (
            self.local_bot_api.enabled
            and self.local_bot_api.mode == "managed"
            and self.local_bot_api.local_mode != self.local_api_is_local
        ):
            raise ValueError(
                "local_api_is_local must match local_bot_api.local_mode in managed mode"
            )
        local_configured = bool(
            self.local_api_base_url and (self.local_api_is_local or self.local_bot_api.enabled)
        )
        if self.max_upload_size_mb > 50 and not local_configured:
            raise ValueError("Cloud Bot API uploads cannot exceed 50 MB")
        if self.max_upload_size_mb > 50 and not self.local_api_is_local:
            raise ValueError("Uploads above 50 MB require local_api_is_local")
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

    def token(self) -> str:
        return self.bot_token.get_secret_value()


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
    best_original: str = "bv*+ba/b"
    video_2160: str = "bv*[height<=2160]+ba/b[height<=2160]"
    video_1440: str = "bv*[height<=1440]+ba/b[height<=1440]"
    video_1080: str
    video_720: str
    video_480: str
    audio_best: str
    audio_mp3: str

    def for_mode(self, mode: DownloadMode) -> str:
        return cast(str, getattr(self, mode.value))


class InstagramSection(StrictModel):
    auto_download: bool = True
    force_mp4: bool = True
    ignore_images: bool = True
    max_videos: int = Field(default=50, ge=1, le=500)
    max_total_size_mb: int = Field(default=4096, ge=1, le=8192)
    #: Batch safeguards for bulk Stories/Highlight downloads (per job, not per item).
    max_stories_per_batch: int = Field(default=100, ge=1, le=500)
    max_highlight_items: int = Field(default=100, ge=1, le=500)


class TranscodeSection(StrictModel):
    enabled: bool = True
    explicit_mp4_enabled: bool = False
    threads: int = Field(default=2, ge=1, le=16)
    max_concurrent: int = Field(default=1, ge=1, le=8)
    timeout_seconds: int = Field(default=1500, ge=30, le=86400)
    progress_interval_seconds: int = Field(default=10, ge=1, le=300)


class WorkspaceCleanupSection(StrictModel):
    cleanup_on_success: bool = True
    cleanup_on_failure: bool = True
    cleanup_on_cancel: bool = True
    cleanup_on_timeout: bool = True


class MediaSection(StrictModel):
    enabled_sources: frozenset[str]
    enabled_modes: tuple[DownloadMode, ...] = tuple(DownloadMode)
    default_mode: DownloadMode = DownloadMode.BEST
    allow_playlists: bool = False
    playlist_max_items: int = Field(default=20, ge=1, le=500)
    max_file_size_mb: int = Field(default=49, ge=1)
    max_source_size_mb: int = Field(default=1024, ge=1, le=8192)
    max_duration_seconds: int = Field(default=14400, ge=1)
    mp4_native_fallback: Mp4NativeFallback = Mp4NativeFallback.LOWER_RESOLUTION
    formats: FormatSection
    instagram: InstagramSection = Field(default_factory=InstagramSection)
    transcode: TranscodeSection = Field(default_factory=TranscodeSection)
    workspace: WorkspaceCleanupSection = Field(default_factory=WorkspaceCleanupSection)

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

    @model_validator(mode="after")
    def validate_source_size_limit(self) -> MediaSection:
        if self.max_source_size_mb < self.max_file_size_mb:
            raise ValueError("max_source_size_mb must be at least max_file_size_mb")
        return self


class MultipartSection(StrictModel):
    enabled: bool = True
    seven_zip_executable: Path = Field(default_factory=lambda: Path("7zz"))
    part_size_mb: int = Field(default=1850, ge=1, le=1900)
    max_total_size_mb: int = Field(default=4096, ge=1, le=8192)
    compression_level: Literal[0] = 0


class YtDlpSection(StrictModel):
    cookies_file: Path | None = None
    proxy_enabled: bool | None = None
    proxy: SecretStr | None = None
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

    @field_validator("proxy")
    @classmethod
    def validate_proxy(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        parsed = urlsplit(value.get_secret_value())
        if (
            parsed.scheme.casefold()
            not in {
                "http",
                "https",
                "socks4",
                "socks4a",
                "socks5",
                "socks5h",
            }
            or not parsed.hostname
        ):
            raise ValueError("proxy must be an absolute HTTP(S) or SOCKS URL")
        return value

    @model_validator(mode="after")
    def validate_proxy_switch(self) -> YtDlpSection:
        if self.proxy_enabled is True and self.proxy is None:
            raise ValueError("proxy is required when proxy_enabled is true")
        return self

    def effective_proxy(self) -> str | None:
        if self.proxy is None or self.proxy_enabled is False:
            return None
        return self.proxy.get_secret_value()


class GalleryDlCookiesSection(StrictModel):
    instagram: Path | None = None
    tiktok: Path | None = None
    twitter: Path | None = None
    pinterest: Path | None = None


class ImageValidationSection(StrictModel):
    max_width: int = Field(default=20000, ge=1, le=100000)
    max_height: int = Field(default=20000, ge=1, le=100000)
    max_pixels: int = Field(default=100_000_000, ge=1_000_000, le=500_000_000)


class GalleryDlSection(StrictModel):
    enabled: bool = True
    timeout_seconds: int = Field(default=600, ge=10, le=86400)
    max_assets_per_job: int = Field(default=30, ge=1, le=100)
    max_total_size_mb: int = Field(default=1024, ge=1, le=8192)
    max_concurrent_processes: int = Field(default=2, ge=1, le=16)
    sleep_request_seconds: float = Field(default=1.0, ge=0, le=60)
    enabled_platforms: frozenset[str] = frozenset({"instagram", "tiktok", "twitter", "pinterest"})
    cookies: GalleryDlCookiesSection = Field(default_factory=GalleryDlCookiesSection)
    images: ImageValidationSection = Field(default_factory=ImageValidationSection)
    album_max_items: int = Field(default=10, ge=2, le=10)
    zip_threshold: int = Field(default=11, ge=2, le=100)

    @field_validator("enabled_platforms")
    @classmethod
    def validate_platforms(cls, values: frozenset[str]) -> frozenset[str]:
        allowed = {"instagram", "tiktok", "twitter", "pinterest"}
        normalized = frozenset(item.strip().casefold() for item in values if item.strip())
        if not normalized <= allowed:
            raise ValueError("gallery_dl.enabled_platforms contains an unsupported platform")
        return normalized

    def cookie_for(self, source: str, canonical_cookie_file: Path | None) -> Path | None:
        if source not in self.enabled_platforms:
            return None
        return canonical_cookie_file


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


class CookieHealthSection(StrictModel):
    enabled: bool = True
    #: Static "expiring soon" threshold (hours before the earliest expiry).
    expiring_soon_hours: float = Field(default=24, ge=1, le=24 * 30)
    #: Reminder cadence for unresolved failure states (minutes).
    reminder_interval_minutes: int = Field(default=180, ge=15, le=10080)
    #: Send one notification when local state recovers to HEALTHY.
    recovery_notifications: bool = True

    @model_validator(mode="before")
    @classmethod
    def ignore_removed_probe_settings(cls, value: object) -> object:
        """Accept old v1.3.4/v1.3.5 probe keys without keeping live-probe behavior."""
        if not isinstance(value, dict):
            return value
        cleaned = dict(value)
        for key in (
            "expiry_watch_interval_minutes",
            "active_probe_interval_minutes",
            "probe_timeout_seconds",
            "probe_concurrency",
            "probes",
        ):
            cleaned.pop(key, None)
        return cleaned


class ObservabilitySection(StrictModel):
    health_host: str = "0.0.0.0"
    health_port: int = Field(default=8080, ge=1, le=65535)
    telegram_readiness_check: bool = True
    metrics_enabled: bool = True


class UpdateOperationsSection(StrictModel):
    prune_old_project_images_after_success: bool = True


class InboundUpdatesSection(StrictModel):
    """Retention and cleanup of the durable Telegram inbound-update inbox."""

    #: Maximum age of a reserved-but-unfinished Telegram status effect before it is quarantined.
    effect_pending_stale_minutes: int = Field(default=10, ge=1, le=1440)

    #: How long COMPLETED update history is kept before bounded purging.
    completed_retention_days: int = Field(default=14, ge=1, le=365)
    #: How long TERMINAL_FAILURE history is kept before bounded purging.
    terminal_failure_retention_days: int = Field(default=30, ge=1, le=365)
    #: Maximum rows deleted per maintenance pass (bounds SQLite write-lock time).
    cleanup_batch_size: int = Field(default=500, ge=1, le=5000)
    #: Unfinished updates older than this are surfaced as stuck (never auto-deleted).
    stuck_after_minutes: int = Field(default=60, ge=1, le=10080)
    #: Retention for completed/uncertain side-effect ledger rows.
    effect_retention_days: int = Field(default=30, ge=1, le=365)


class OperationsSection(StrictModel):
    update: UpdateOperationsSection = Field(default_factory=UpdateOperationsSection)
    inbound_updates: InboundUpdatesSection = Field(default_factory=InboundUpdatesSection)


class RecoverySection(StrictModel):
    """Bounded automatic recovery of explicitly recoverable failed media jobs."""

    cookie_remediation_enabled: bool = True
    app_fix_recovery_enabled: bool = True
    #: Maximum automatic recovery attempts per job (cookie remediation and/or app fix).
    max_recovery_attempts: int = Field(default=2, ge=1, le=10)
    #: Oldest recoverable failed request that is still eligible for automatic recovery.
    max_recoverable_age_days: int = Field(default=7, ge=1, le=365)
    #: Send one concise resume notification when a recovered request restarts (no spam).
    notify_on_resume: bool = True
    #: Maximum cookie-remediation candidates requeued per pass (oldest-first).
    remediation_batch_size: int = Field(default=20, ge=1, le=500)
    #: Maximum app-fix recovery candidates requeued at startup.
    startup_recovery_batch_size: int = Field(default=20, ge=1, le=500)
    #: Maximum recovery-requeue reconciliation candidates per pass.
    reconciliation_batch_size: int = Field(default=50, ge=1, le=1000)
    #: Optional absolute outstanding-queue override. Null derives a threshold from queue.max_jobs.
    queue_pressure_threshold: int | None = Field(default=None, ge=1, le=100000)
    #: Multiplier applied to queue.max_jobs to derive the outstanding-queue pressure threshold.
    #: queue.max_jobs is ARQ worker concurrency, so the threshold is "waves of outstanding work"
    #: relative to how many jobs one worker can run at once.
    queue_backlog_per_worker_slot: int = Field(default=4, ge=1, le=100)
    #: Bounded fairness: never take more than this many candidates per user per batch.
    max_recovery_per_user: int = Field(default=5, ge=1, le=100)

    def effective_queue_pressure_threshold(self, queue_max_jobs: int) -> int:
        """Absolute outstanding-queue threshold: explicit override or concurrency * multiplier."""
        if self.queue_pressure_threshold is not None:
            return self.queue_pressure_threshold
        return max(1, queue_max_jobs * self.queue_backlog_per_worker_slot)


class WebCompanionSection(StrictModel):
    """Separate least-privilege browser/callback boundary (T016), disabled by default.

    The main (bot) settings surface additionally carries the bot-side handoff signing key, because
    the bot mints secure connection links. The companion process uses a purpose-built reduced
    settings model (``bootstrap.companion.CompanionSettings``) that never maps the signing key and
    never maps ``telegram``, so its process objects contain no bot token and no signer.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=8090, ge=1, le=65535)
    #: Optional public HTTPS base URL the bot embeds in generated connection links.
    public_base_url: str | None = None
    #: Browser session lifetime for one completed connection exchange.
    session_max_seconds: int = Field(default=300, ge=60, le=3600)
    #: Bounded in-memory interactive login/2FA flow lifetime.
    interactive_flow_max_seconds: int = Field(default=600, ge=60, le=1800)
    #: Hard cap on concurrent in-memory interactive flows.
    interactive_flow_max_sessions: int = Field(default=100, ge=1, le=10000)
    #: Maximum accepted request-body size in bytes.
    body_limit_bytes: int = Field(default=65536, ge=1024, le=1048576)
    #: Per-request processing cap; clients must complete within this deadline.
    read_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    #: Bound on requests per minute per client IP (bounded, not exported as a metric label).
    rate_limit_per_minute: int = Field(default=60, ge=1, le=100000)
    #: Explicitly trusted reverse-proxy client addresses; others are never trusted for forwarded.
    trusted_proxies: tuple[str, ...] = ()
    #: Acceptable clock-skew window for Ed25519 handoff claims.
    handoff_clock_skew_seconds: int = Field(default=30, ge=0, le=300)
    #: PEM PKCS8 Ed25519 public key the companion uses to verify handoff signatures.
    handoff_verification_key: SecretStr | None = None
    #: Encoded Ed25519 private key the bot uses to sign handoff claims (bot surface only).
    handoff_signing_key: SecretStr | None = None

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("web_companion.public_base_url must be an absolute HTTPS URL")
        return value.rstrip("/")

    @field_validator("trusted_proxies")
    @classmethod
    def validate_trusted_proxies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for entry in values:
            value = entry.strip()
            if not value:
                continue
            _validate_ip_or_cidr(value)
            normalized.append(value)
        return tuple(normalized)

    @model_validator(mode="after")
    def validate_enabled_material(self) -> WebCompanionSection:
        if self.enabled and self.handoff_verification_key is None:
            raise ValueError(
                "web_companion.handoff_verification_key is required when the companion is enabled"
            )
        return self

    def verification_key_bytes(self) -> bytes | None:
        if self.handoff_verification_key is None:
            return None
        return self.handoff_verification_key.get_secret_value().encode("utf-8")

    def signing_key_bytes(self) -> bytes | None:
        if self.handoff_signing_key is None:
            return None
        return self.handoff_signing_key.get_secret_value().encode("utf-8")


class VaultKeyRingSection(StrictModel):
    """Ignored least-privilege key ring for the encrypted Instagram credential vault (T017).

    Keys are 32-byte AES-256 keys supplied as 64 hex characters and never leave configuration,
    logs, SQLite, source, images, or metrics. ``active_key`` is used for new encryptions;
    ``retained_keys`` are decrypt-only for rotation. All keys are optional so installs with user
    credentials disabled start without any key material.
    """

    active_key_id: str = ""
    active_key: SecretStr | None = None
    retained_keys: dict[str, SecretStr] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_ring(self) -> VaultKeyRingSection:
        entries: list[tuple[str, SecretStr]] = []
        if self.active_key is not None:
            if not self.active_key_id:
                raise ValueError("vault.active_key_id is required when vault.active_key is set")
            entries.append((self.active_key_id, self.active_key))
        for key_id, value in self.retained_keys.items():
            if not key_id.strip():
                raise ValueError("vault.retained_keys keys must be non-empty")
            entries.append((key_id, value))
        ids = [key_id for key_id, _ in entries]
        if len(ids) != len(set(ids)):
            raise ValueError("vault key IDs must be unique")
        for key_id, value in entries:
            _validate_aes256_hex(key_id, value.get_secret_value())
        return self

    def has_keys(self) -> bool:
        return self.active_key is not None


class Settings(StrictModel):
    app: AppSection
    telegram: TelegramSection
    redis: RedisSection
    queue: QueueSection
    storage: StorageSection
    media: MediaSection
    multipart: MultipartSection = Field(default_factory=MultipartSection)
    yt_dlp: YtDlpSection
    gallery_dl: GalleryDlSection = Field(default_factory=GalleryDlSection)
    security: SecuritySection
    persistence: PersistenceSection
    observability: ObservabilitySection
    operations: OperationsSection = Field(default_factory=OperationsSection)
    cookie_health: CookieHealthSection = Field(default_factory=CookieHealthSection)
    recovery: RecoverySection = Field(default_factory=RecoverySection)
    web_companion: WebCompanionSection = Field(default_factory=WebCompanionSection)
    vault: VaultKeyRingSection = Field(default_factory=VaultKeyRingSection)

    @model_validator(mode="after")
    def validate_cookie_file_identity(self) -> Settings:
        configured = self._configured_cookie_files()
        identities = {path.expanduser().resolve() for path in configured}
        if len(identities) > 1:
            raise ValueError(
                "yt_dlp.cookies_file and gallery_dl.cookies entries must reference "
                "one canonical cookie file"
            )
        return self

    def effective_cookie_file(self) -> Path | None:
        configured = self._configured_cookie_files()
        return configured[0].expanduser().resolve() if configured else None

    def _configured_cookie_files(self) -> tuple[Path, ...]:
        candidates = (
            self.yt_dlp.cookies_file,
            self.gallery_dl.cookies.instagram,
            self.gallery_dl.cookies.tiktok,
            self.gallery_dl.cookies.twitter,
            self.gallery_dl.cookies.pinterest,
        )
        return tuple(path for path in candidates if path is not None)

    def database_path(self) -> Path:
        return self.storage.state_path() / self.persistence.database_filename

    def validate_runtime(self, *, require_token: bool) -> None:
        if require_token and self.telegram.token() in {"CHANGE_ME", ""}:
            raise ConfigurationError("telegram.bot_token must be set in config.yaml")
        self.storage.downloads_path()
        self.storage.temp_path()
        self.storage.state_path()
        self.database_path()
        local_api = self.telegram.local_bot_api
        if local_api.enabled:
            local_api.migration.state_file.expanduser().resolve()
            local_api.working_directory.expanduser().resolve()
            local_api.temp_directory.expanduser().resolve()
            local_api.log_file.expanduser().resolve()
        if self.media.max_file_size_mb > self.multipart.max_total_size_mb:
            raise ConfigurationError(
                "media.max_file_size_mb cannot exceed multipart.max_total_size_mb"
            )
        if self.media.max_file_size_mb > self.telegram.max_upload_size_mb:
            if not self.multipart.enabled:
                raise ConfigurationError(
                    "multipart must be enabled when media can exceed the direct upload limit"
                )
            if self.multipart.part_size_mb > self.telegram.max_upload_size_mb:
                raise ConfigurationError(
                    "multipart.part_size_mb cannot exceed telegram.max_upload_size_mb"
                )
        if self.gallery_dl.max_total_size_mb > self.multipart.max_total_size_mb:
            raise ConfigurationError(
                "gallery_dl.max_total_size_mb cannot exceed multipart.max_total_size_mb"
            )

    def create_runtime_directories(self) -> None:
        for path in (
            self.storage.downloads_path(),
            self.storage.temp_path(),
            self.storage.state_path(),
        ):
            path.mkdir(parents=True, exist_ok=True)
        local_api = self.telegram.local_bot_api
        if local_api.enabled and local_api.mode == "managed":
            local_api.working_directory.expanduser().resolve().mkdir(parents=True, exist_ok=True)
            local_api.temp_directory.expanduser().resolve().mkdir(parents=True, exist_ok=True)
            local_api.log_file.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        if local_api.enabled:
            local_api.migration.state_file.expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )


def _validate_aes256_hex(key_id: str, value: str) -> None:
    cleaned = value.strip()
    if len(cleaned) != 64 or any(char not in "0123456789abcdefABCDEF" for char in cleaned):
        raise ValueError(f"vault key {key_id!r} must be 32 bytes as 64 hex characters")


def _validate_ip_or_cidr(value: str) -> None:
    from ipaddress import (
        AddressValueError,
        NetmaskValueError,
        ip_address,
        ip_network,
    )

    if "/" in value:
        try:
            ip_network(value, strict=False)
        except (AddressValueError, NetmaskValueError, ValueError) as exc:
            raise ValueError(f"Invalid IP/CIDR value: {value}") from exc
        return
    try:
        ip_address(value)
    except (AddressValueError, ValueError) as exc:
        raise ValueError(f"Invalid IP address value: {value}") from exc


def default_config_path() -> Path:
    return Path(os.environ.get("APP_CONFIG_PATH", "config.yaml"))


def load_settings(path: Path | str | None = None, *, require_token: bool = False) -> Settings:
    config_path = (Path(path) if path is not None else default_config_path()).expanduser().resolve()
    try:
        with config_path.open("r", encoding="utf-8") as file:
            raw = yaml.safe_load(file)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Configuration file not found: {config_path}") from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise ConfigurationError(f"Invalid YAML configuration{location}") from exc

    if not isinstance(raw, dict):
        raise ConfigurationError("Configuration root must be a mapping")
    _resolve_local_api_paths(raw, config_path.parent)

    try:
        settings = Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(str(exc)) from exc

    settings.validate_runtime(require_token=require_token)
    return settings


def _resolve_local_api_paths(raw: dict[str, object], config_directory: Path) -> None:
    ytdlp = raw.get("yt_dlp")
    if isinstance(ytdlp, dict):
        cookies = ytdlp.get("cookies_file")
        if isinstance(cookies, str) and cookies and not Path(cookies).expanduser().is_absolute():
            ytdlp["cookies_file"] = str((config_directory / cookies).resolve())
    gallery = raw.get("gallery_dl")
    if isinstance(gallery, dict):
        cookies = gallery.get("cookies")
        if isinstance(cookies, dict):
            for key in ("instagram", "tiktok", "twitter", "pinterest"):
                value = cookies.get(key)
                if isinstance(value, str) and value and not Path(value).expanduser().is_absolute():
                    cookies[key] = str((config_directory / value).resolve())
    multipart = raw.get("multipart")
    if isinstance(multipart, dict):
        value = multipart.get("seven_zip_executable")
        if (
            isinstance(value, str)
            and value
            and not Path(value).expanduser().is_absolute()
            and Path(value).parent != Path(".")
        ):
            multipart["seven_zip_executable"] = str((config_directory / value).resolve())
    telegram = raw.get("telegram")
    if not isinstance(telegram, dict):
        return
    local_api = telegram.get("local_bot_api")
    if not isinstance(local_api, dict):
        return
    for key in ("executable", "working_directory", "temp_directory", "log_file"):
        value = local_api.get(key)
        if isinstance(value, str) and value and not Path(value).expanduser().is_absolute():
            local_api[key] = str((config_directory / value).resolve())
    migration = local_api.get("migration")
    if isinstance(migration, dict):
        value = migration.get("state_file")
        if isinstance(value, str) and value and not Path(value).expanduser().is_absolute():
            migration["state_file"] = str((config_directory / value).resolve())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings(require_token=True)
