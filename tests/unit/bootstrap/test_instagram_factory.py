"""Instagram connection service factory tests (T018)."""

from __future__ import annotations

import secrets

from pydantic import SecretStr

from telegram_media_bot.bootstrap.config import Settings, VaultKeyRingSection
from telegram_media_bot.bootstrap.instagram import build_instagram_connection_service
from telegram_media_bot.infrastructure.security.handoff import Ed25519HandoffSigner


def test_factory_returns_none_without_vault_keys(settings: Settings) -> None:
    assert build_instagram_connection_service(settings) is None


def test_factory_returns_none_without_signing_key(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "vault": VaultKeyRingSection.model_validate(
                {"active_key_id": "k", "active_key": secrets.token_bytes(32).hex()}
            )
        }
    )
    assert build_instagram_connection_service(configured) is None


def test_factory_builds_service_with_vault_and_signing_material(settings: Settings) -> None:
    _signer, private = Ed25519HandoffSigner.generate()
    companion = settings.web_companion.model_copy(
        update={
            "handoff_signing_key": SecretStr(private),
            "public_base_url": "https://connect.example.test",
        }
    )
    configured = settings.model_copy(
        update={
            "vault": VaultKeyRingSection.model_validate(
                {"active_key_id": "k", "active_key": secrets.token_bytes(32).hex()}
            ),
            "web_companion": companion,
        }
    )
    service = build_instagram_connection_service(configured)
    assert service is not None
    link = service.create_connect_link(7)
    assert link.startswith("https://connect.example.test/instagram/connect#handoff=")
