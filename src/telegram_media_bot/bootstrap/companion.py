"""Companion process composition root (T016).

The companion is an optional, disabled-by-default ``aiohttp.web`` process with least privilege:
its settings model deliberately maps no Telegram section and no handoff signing key, so the
process objects never contain a bot token or signer. It shares only the WAL database path for the
one-time nonce store and holds the handoff verification public key.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from aiohttp import web
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from telegram_media_bot.application.services.handoff import CompanionHandoffService
from telegram_media_bot.bootstrap.config import _validate_ip_or_cidr
from telegram_media_bot.domain.errors import ConfigurationError
from telegram_media_bot.domain.web_companion import (
    InstagramConnectResult,
    InstagramConnectStage,
    PaymentCallbackOutcome,
)
from telegram_media_bot.infrastructure.persistence.sqlite_handoff import (
    SqliteHandoffNonceRepository,
)
from telegram_media_bot.infrastructure.security.handoff import (
    Ed25519HandoffVerifier,
    HandoffCryptoError,
)
from telegram_media_bot.infrastructure.web_companion.app import CompanionWebApp

#: Exact set of ``web_companion`` YAML keys the companion may consume. Deliberately excludes
#: ``handoff_signing_key`` (bot surface) and every ``telegram`` key (bot token).
_ALLOWED_COMPANION_KEYS = frozenset(
    {
        "enabled",
        "host",
        "port",
        "session_max_seconds",
        "interactive_flow_max_seconds",
        "interactive_flow_max_sessions",
        "body_limit_bytes",
        "read_timeout_seconds",
        "rate_limit_per_minute",
        "trusted_proxies",
        "handoff_clock_skew_seconds",
        "handoff_verification_key",
    }
)


class CompanionSettings(BaseModel):
    """Reduced, least-privilege companion configuration (no bot token, no signer)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=8090, ge=1, le=65535)
    session_max_seconds: int = Field(default=300, ge=60, le=3600)
    interactive_flow_max_seconds: int = Field(default=600, ge=60, le=1800)
    interactive_flow_max_sessions: int = Field(default=100, ge=1, le=10000)
    body_limit_bytes: int = Field(default=65536, ge=1024, le=1048576)
    read_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=100000)
    trusted_proxies: tuple[str, ...] = ()
    handoff_clock_skew_seconds: int = Field(default=30, ge=0, le=300)
    handoff_verification_key: SecretStr | None = None
    #: Derived, never operator-supplied: shared WAL path for the nonce store.
    database_path: Path | None = None

    @field_validator("trusted_proxies")
    @classmethod
    def validate_trusted_proxies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for entry in values:
            if entry.strip():
                _validate_ip_or_cidr(entry)
        return tuple(entry.strip() for entry in values if entry.strip())

    @model_validator(mode="after")
    def validate_enabled_material(self) -> CompanionSettings:
        if self.enabled and self.handoff_verification_key is None:
            raise ValueError("web_companion.handoff_verification_key is required for the companion")
        return self

    @model_validator(mode="after")
    def validate_signer_absence(self) -> CompanionSettings:
        # Compile-time/model-level guard: it must be impossible to construct a companion that holds
        # the bot-side signing key. There is no such field on this model; this validator documents
        # and enforces the invariant via attribute absence at the type level.
        for forbidden in ("bot_token", "handoff_signing_key", "telegram"):
            if hasattr(self, forbidden):
                raise AssertionError(f"companion settings must not expose {forbidden}")
        return self


class DisabledInstagramConnectionFlow:
    async def step(
        self,
        *,
        owner_user_id: int,
        session_id: str,
        input_value: str | None,
    ) -> InstagramConnectResult:
        del owner_user_id, session_id, input_value
        return InstagramConnectResult(stage=InstagramConnectStage.NOT_AVAILABLE, message="")


class EmptyProviderCallbackRegistry:
    """No payment provider adapter is registered (T024 blocked). Returns None always."""

    def verifier_for(self, provider_id: str):  # type: ignore[no-untyped-def]
        del provider_id
        return None


class UnavailablePaymentCallbackProcessor:
    async def process(self, *, provider_id: str, provider_payload: bytes) -> PaymentCallbackOutcome:
        del provider_id, provider_payload
        return PaymentCallbackOutcome.NOT_AVAILABLE


def load_companion_settings(path: Path | str | None = None) -> CompanionSettings:
    from telegram_media_bot.bootstrap.config import default_config_path

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

    web = raw.get("web_companion")
    subset: dict[str, object] = {}
    if isinstance(web, dict):
        for key in _ALLOWED_COMPANION_KEYS:
            if key in web:
                subset[key] = web[key]

    subset["database_path"] = _resolve_database_path(raw, config_path.parent)
    try:
        return CompanionSettings.model_validate(subset)
    except ValidationError as exc:
        raise ConfigurationError(str(exc)) from exc


def _resolve_database_path(raw: dict[str, object], config_directory: Path) -> Path:
    storage = raw.get("storage")
    storage_map = storage if isinstance(storage, dict) else {}
    root = Path(str(storage_map.get("root_directory", "/data")))
    if not root.is_absolute():
        root = (config_directory / root).resolve()
    else:
        root = root.expanduser().resolve()
    state_child = Path(str(storage_map.get("state_directory", "state")))
    if not state_child.is_absolute():
        state_child = root / state_child
    state_child = state_child.expanduser().resolve()
    persistence = raw.get("persistence")
    persistence_map = persistence if isinstance(persistence, dict) else {}
    filename = str(persistence_map.get("database_filename", "jobs.sqlite3"))
    return (state_child / filename).resolve()


def build_companion_app(settings: CompanionSettings) -> web.Application:
    """Compose and build the companion aiohttp application (deterministic, no network binding)."""
    if not settings.enabled:
        raise ConfigurationError("web_companion companion is disabled in configuration")
    if settings.handoff_verification_key is None:
        raise ConfigurationError("web_companion handoff verification key is required")
    if settings.database_path is None:
        raise ConfigurationError("companion database path is not configured")
    verifier = _verifier_from_settings(settings)
    nonce_repo = SqliteHandoffNonceRepository(settings.database_path)
    nonce_repo.initialize()
    service = CompanionHandoffService(verifier=verifier, nonce_repository=nonce_repo)
    app = CompanionWebApp(
        host=settings.host,
        port=settings.port,
        session_max_seconds=settings.session_max_seconds,
        interactive_flow_max_seconds=settings.interactive_flow_max_seconds,
        interactive_flow_max_sessions=settings.interactive_flow_max_sessions,
        body_limit_bytes=settings.body_limit_bytes,
        read_timeout_seconds=settings.read_timeout_seconds,
        rate_limit_per_minute=settings.rate_limit_per_minute,
        trusted_proxies=settings.trusted_proxies,
        handoff_exchange=service.exchange,
        flow=DisabledInstagramConnectionFlow(),
        provider_registry=EmptyProviderCallbackRegistry(),
        payment_processor=UnavailablePaymentCallbackProcessor(),
    )
    return app.build()


def _verifier_from_settings(settings: CompanionSettings) -> Ed25519HandoffVerifier:
    if settings.handoff_verification_key is None:
        raise ConfigurationError("web_companion handoff verification key is required")
    key_bytes = settings.handoff_verification_key.get_secret_value().encode("utf-8")
    try:
        return Ed25519HandoffVerifier.from_public_pem(
            key_bytes, max_clock_skew_seconds=settings.handoff_clock_skew_seconds
        )
    except HandoffCryptoError as exc:
        raise ConfigurationError("Invalid web_companion handoff verification key") from exc


__all__ = [
    "CompanionSettings",
    "build_companion_app",
    "load_companion_settings",
]
