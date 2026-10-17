"""Instagram connection service composition factory (T018)."""

from __future__ import annotations

from datetime import timedelta

from telegram_media_bot.application.services.credential_vault import CredentialVault
from telegram_media_bot.application.services.handoff import HandoffLinkService
from telegram_media_bot.application.services.instagram_connection import (
    InstagramConnectionService,
)
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.infrastructure.credentials.key_ring import (
    CredentialCryptor,
    VaultKeyRing,
)
from telegram_media_bot.infrastructure.instagram_login.real import RealInstagramSessionAcquirer
from telegram_media_bot.infrastructure.persistence.sqlite_instagram_credentials import (
    SqliteInstagramCredentialRepository,
)
from telegram_media_bot.infrastructure.security.handoff import (
    Ed25519HandoffSigner,
    Ed25519HandoffVerifier,
    HandoffCryptoError,
)

_DEFAULT_HANDOFF_LIFETIME_MINUTES = 5


def instagram_connection_issues(settings: Settings) -> tuple[str, ...]:
    """Configuration-only readiness, with safe operator reasons and no network login."""
    issues: list[str] = []
    web = settings.web_companion
    if not web.enabled:
        issues.append("web_companion_disabled")
    if not web.public_base_url:
        issues.append("https_origin_missing")
    if not settings.vault.has_keys():
        issues.append("vault_key_missing")
    private = web.handoff_signing_key
    public = web.handoff_verification_key
    if private is None:
        issues.append("signing_key_missing")
    if public is None:
        issues.append("verification_key_missing")
    if private is not None and public is not None:
        try:
            expected = Ed25519HandoffVerifier.from_private_encoded(
                private.get_secret_value()
            ).public_key_pem()
            actual = Ed25519HandoffVerifier.from_public_pem(
                public.get_secret_value().encode("utf-8")
            ).public_key_pem()
            if actual != expected:
                issues.append("handoff_key_mismatch")
        except HandoffCryptoError:
            issues.append("handoff_key_invalid")
    return tuple(issues)


def build_instagram_connection_service(settings: Settings) -> InstagramConnectionService | None:
    """Compose the connection service, or None when user credentials/companion are not enabled."""
    if instagram_connection_issues(settings):
        return None
    signing_bytes = settings.web_companion.signing_key_bytes()
    if signing_bytes is None:
        return None

    repo = SqliteInstagramCredentialRepository(settings.database_path())
    repo.initialize()
    ring = VaultKeyRing.from_config(settings.vault)
    vault = CredentialVault(repo, CredentialCryptor(ring))

    try:
        signer = Ed25519HandoffSigner.from_encoded(signing_bytes.decode("ascii"))
    except HandoffCryptoError, UnicodeDecodeError, ValueError:
        return None
    link_service = HandoffLinkService(
        signer, lifetime=timedelta(minutes=_DEFAULT_HANDOFF_LIFETIME_MINUTES)
    )
    return InstagramConnectionService(
        link_service=link_service,
        connect_base_url=settings.web_companion.public_base_url or "",
        vault=vault,
        acquirer=RealInstagramSessionAcquirer(),
        acquirer_factory=RealInstagramSessionAcquirer,
    )


__all__ = ["build_instagram_connection_service"]
