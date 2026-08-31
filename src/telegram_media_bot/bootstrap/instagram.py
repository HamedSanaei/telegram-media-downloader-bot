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
from telegram_media_bot.infrastructure.instagram_login.fake import FakeInstagramSessionAcquirer
from telegram_media_bot.infrastructure.persistence.sqlite_instagram_credentials import (
    SqliteInstagramCredentialRepository,
)
from telegram_media_bot.infrastructure.security.handoff import (
    Ed25519HandoffSigner,
    HandoffCryptoError,
)

_DEFAULT_HANDOFF_LIFETIME_MINUTES = 5


def build_instagram_connection_service(settings: Settings) -> InstagramConnectionService | None:
    """Compose the connection service, or None when user credentials/companion are not enabled."""
    if not settings.vault.has_keys():
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
        acquirer=FakeInstagramSessionAcquirer(),
    )


__all__ = ["build_instagram_connection_service"]
