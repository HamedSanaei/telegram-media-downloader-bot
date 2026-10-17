"""Explicit operator provisioning: preserve existing keys and write a token-free web profile."""

from __future__ import annotations

import os
import secrets
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from telegram_media_bot.bootstrap.companion import (
    _ALLOWED_COMPANION_KEYS,
    CompanionSettings,
    load_companion_settings,
)
from telegram_media_bot.bootstrap.config import Settings, WebCompanionSection
from telegram_media_bot.bootstrap.instagram import instagram_connection_issues
from telegram_media_bot.domain.errors import ConfigurationError
from telegram_media_bot.infrastructure.security.handoff import (
    Ed25519HandoffSigner,
    Ed25519HandoffVerifier,
    HandoffCryptoError,
)


def companion_profile_path(config_path: Path) -> Path:
    return config_path.with_name("companion.local.yaml")


def instagram_profile_issues(settings: Settings, profile: Path) -> tuple[str, ...]:
    """Check the restricted projection too; existence alone is not readiness."""
    if not profile.is_file():
        return ("restricted_companion_profile_missing",)
    try:
        raw = _read(profile)
        web = raw.get("web_companion", {})
        if set(raw) - {"storage", "persistence", "web_companion", "vault"} or (
            not isinstance(web, dict) or "handoff_signing_key" in web
        ):
            return ("restricted_companion_profile_contains_forbidden_keys",)
        reduced = load_companion_settings(profile)
        if (
            not reduced.enabled
            or reduced.vault != settings.vault
            or reduced.handoff_verification_key != settings.web_companion.handoff_verification_key
            or reduced.database_path != settings.database_path().resolve()
        ):
            return ("restricted_companion_profile_mismatch",)
    except ConfigurationError, ValidationError, OSError:
        return ("restricted_companion_profile_invalid",)
    return ()


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError("Setup requires an existing regular YAML configuration")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError("Cannot read setup configuration") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("Configuration root must be a mapping")
    return raw


@contextmanager
def _setup_lock(path: Path) -> Iterator[None]:
    lock = path.with_name(".instagram-setup.lock")
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ConfigurationError(
            "Instagram setup is already running; inspect the setup lock before retrying"
        ) from exc
    try:
        os.close(descriptor)
        yield
    finally:
        lock.unlink(missing_ok=True)


def _atomic_write(path: Path, content: bytes, reference: Path) -> None:
    metadata = reference.stat()
    descriptor, name = tempfile.mkstemp(prefix=".instagram-setup-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
                if hasattr(os, "fchown"):
                    os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def configure_instagram_connection(
    config_path: Path,
    *,
    public_base_url: str,
) -> Path | None:
    """Provision explicitly, atomically per file, with private backups and rollback on failure.

    Existing encryption/signing keys are never rotated. This configures local files only;
    DNS, TLS ingress, service start and bot/worker restart remain explicit deployment steps.
    """
    if config_path.is_symlink():
        raise ConfigurationError("Setup does not replace configuration symlinks")
    config_path = config_path.expanduser().resolve()
    profile = companion_profile_path(config_path)
    if profile.is_symlink() or profile.resolve() == config_path or not profile.parent.is_dir():
        raise ConfigurationError("Companion profile must be a separate regular local YAML file")
    profile = profile.resolve()
    with _setup_lock(config_path):
        return _configure(config_path, profile, public_base_url)


def _configure(config_path: Path, profile: Path, public_base_url: str) -> Path | None:
    raw = _read(config_path)
    try:
        WebCompanionSection(public_base_url=public_base_url)
        Settings.model_validate(raw)
    except ValidationError:
        raise ConfigurationError("Invalid setup configuration or HTTPS origin") from None
    if profile.exists():
        existing = _read(profile)
        if set(existing) - {"storage", "persistence", "web_companion", "vault"}:
            raise ConfigurationError("Refusing to replace a non-Instagram companion profile")
    web = raw.setdefault("web_companion", {})
    vault = raw.setdefault("vault", {})
    private = web.get("handoff_signing_key")
    public = web.get("handoff_verification_key")
    try:
        if private:
            verifier = Ed25519HandoffVerifier.from_private_encoded(str(private))
        elif public:
            raise ConfigurationError(
                "Existing verification key has no signer; restore its matching private key"
            )
        else:
            _, private = Ed25519HandoffSigner.generate()
            verifier = Ed25519HandoffVerifier.from_private_encoded(private)
        expected = verifier.public_key_pem().decode("ascii")
        if (
            public
            and Ed25519HandoffVerifier.from_public_pem(str(public).encode()).public_key_pem()
            != verifier.public_key_pem()
        ):
            raise ConfigurationError("Existing signing and verification keys do not match")
    except HandoffCryptoError:
        raise ConfigurationError(
            "Existing handoff key material is invalid; restore it instead of rotating"
        ) from None
    web.update(
        enabled=True,
        public_base_url=public_base_url.rstrip("/"),
        handoff_signing_key=private,
        handoff_verification_key=expected,
    )
    if not vault.get("active_key"):
        if vault.get("retained_keys") or vault.get("active_key_id"):
            raise ConfigurationError(
                "Restore/select an active vault key; setup never replaces a retained key ring"
            )
        # A lost configuration key must not be mistaken for a fresh installation.
        before_settings = Settings.model_validate(raw)
        database = before_settings.database_path()
        if database.exists():
            try:
                with closing(
                    sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
                ) as connection:
                    present = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='instagram_credentials'"
                    ).fetchone()
                    encrypted = (
                        present
                        and connection.execute(
                            "SELECT 1 FROM instagram_credentials WHERE envelope_json IS NOT NULL LIMIT 1"
                        ).fetchone()
                    )
                    if encrypted:
                        raise ConfigurationError(
                            "Encrypted sessions already exist; restore the original vault key"
                        )
            except sqlite3.Error:
                raise ConfigurationError(
                    "Cannot verify existing credential storage; setup refused"
                ) from None
        vault.update(active_key_id="instagram-v1", active_key=secrets.token_hex(32))
    try:
        settings = Settings.model_validate(raw)
    except ValidationError:
        raise ConfigurationError("Generated configuration did not pass validation") from None
    if instagram_connection_issues(settings):
        raise ConfigurationError("Personal Instagram connection prerequisites remain incomplete")
    web_profile = {key: value for key, value in web.items() if key in _ALLOWED_COMPANION_KEYS}
    web_profile.update(host="0.0.0.0", port=8090)
    reduced = {
        "web_companion": web_profile,
        "vault": vault,
        "storage": {
            "root_directory": str(settings.storage.root_directory.resolve()),
            "state_directory": str(settings.storage.state_directory),
        },
        "persistence": {"database_filename": settings.persistence.database_filename},
    }
    CompanionSettings.model_validate({**web_profile, "vault": vault})
    main_bytes = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode("utf-8")
    profile_bytes = yaml.safe_dump(reduced, allow_unicode=True, sort_keys=False).encode("utf-8")
    original_main = config_path.read_bytes()
    original_profile = profile.read_bytes() if profile.exists() else None
    if original_main == main_bytes and original_profile == profile_bytes:
        return None
    backup = (
        config_path.parent
        / "backups"
        / (datetime.now(UTC).strftime("instagram-setup-%Y%m%dT%H%M%SZ-") + secrets.token_hex(4))
    )
    backup.mkdir(parents=True, mode=0o700)
    _atomic_write(backup / "config.yaml", original_main, config_path)
    if original_profile is not None:
        _atomic_write(backup / "companion.local.yaml", original_profile, profile)
    # Write the restricted profile first; the main file enables link minting last.
    published = False
    try:
        _atomic_write(profile, profile_bytes, profile if profile.exists() else config_path)
        published = True
        if config_path.read_bytes() != original_main:
            raise ConfigurationError("Configuration changed during setup; refusing to overwrite it")
        _atomic_write(config_path, main_bytes, config_path)
    except Exception:
        if published and profile.read_bytes() == profile_bytes:
            if original_profile is None:
                profile.unlink()
            else:
                _atomic_write(profile, original_profile, profile)
        raise ConfigurationError(
            "Setup failed; original configuration and private backup preserved"
        ) from None
    return backup
