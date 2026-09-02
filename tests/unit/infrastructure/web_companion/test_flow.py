"""Companion transient Instagram connection flow tests (T018)."""

from __future__ import annotations

import secrets
from datetime import timedelta
from pathlib import Path

from telegram_media_bot.application.services.credential_vault import CredentialVault
from telegram_media_bot.application.services.handoff import HandoffLinkService
from telegram_media_bot.application.services.instagram_connection import InstagramConnectionService
from telegram_media_bot.domain.instagram_credentials import InstagramCredentialState
from telegram_media_bot.domain.web_companion import InstagramConnectStage
from telegram_media_bot.infrastructure.credentials.key_ring import CredentialCryptor, VaultKeyRing
from telegram_media_bot.infrastructure.instagram_login.fake import FakeInstagramSessionAcquirer
from telegram_media_bot.infrastructure.persistence.sqlite_instagram_credentials import (
    SqliteInstagramCredentialRepository,
)
from telegram_media_bot.infrastructure.security.handoff import Ed25519HandoffSigner
from telegram_media_bot.infrastructure.web_companion.flow import CompanionInstagramConnectionFlow


def _flow(
    tmp_path: Path, *, challenge: bool = False, reject: bool = False
) -> tuple[SqliteInstagramCredentialRepository, CredentialVault, CompanionInstagramConnectionFlow]:
    repo = SqliteInstagramCredentialRepository(tmp_path / "creds.sqlite3")
    repo.initialize()
    ring = VaultKeyRing.from_hex_material(
        active_key_id="k", active_key=secrets.token_bytes(32).hex()
    )
    vault = CredentialVault(repo, CredentialCryptor(ring))
    _s, private = Ed25519HandoffSigner.generate()
    link = HandoffLinkService(
        Ed25519HandoffSigner.from_encoded(private), lifetime=timedelta(minutes=5)
    )
    connection = InstagramConnectionService(
        link_service=link,
        connect_base_url="https://connect.example.test",
        vault=vault,
        acquirer=FakeInstagramSessionAcquirer(challenge_required=challenge, reject_always=reject),
    )
    flow = CompanionInstagramConnectionFlow(connection, max_age_seconds=600, max_sessions=10)
    return repo, vault, flow


async def test_first_empty_step_prompts_credentials(tmp_path: Path) -> None:
    _repo, _vault, flow = _flow(tmp_path)
    result = await flow.step(owner_user_id=7, session_id="s1", input_value=None)
    assert result.stage is InstagramConnectStage.NEED_CREDENTIALS


async def test_password_connects_and_stores(tmp_path: Path) -> None:
    _repo, vault, flow = _flow(tmp_path)
    result = await flow.step(
        owner_user_id=7, session_id="s1", input_value={"username": "user", "password": "pw"}
    )
    assert result.stage is InstagramConnectStage.CONNECTED
    view = vault.get_view(7)
    assert view is not None and view.state is InstagramCredentialState.CONNECTED


async def test_challenge_then_2fa(tmp_path: Path) -> None:
    _repo, vault, flow = _flow(tmp_path, challenge=True)
    prompted = await flow.step(
        owner_user_id=7, session_id="s1", input_value={"username": "user", "password": "pw"}
    )
    assert prompted.stage is InstagramConnectStage.NEED_2FA
    done = await flow.step(owner_user_id=7, session_id="s1", input_value={"code": "123456"})
    assert done.stage is InstagramConnectStage.CONNECTED
    view = vault.get_view(7)
    assert view is not None and view.state is InstagramCredentialState.CONNECTED


async def test_denied_stage(tmp_path: Path) -> None:
    _repo, _vault, flow = _flow(tmp_path, reject=True)
    result = await flow.step(
        owner_user_id=7, session_id="s1", input_value={"username": "user", "password": "pw"}
    )
    assert result.stage is InstagramConnectStage.DENIED


async def test_wrong_owner_cannot_store(tmp_path: Path) -> None:
    _repo, vault, flow = _flow(tmp_path)
    result = await flow.step(
        owner_user_id=7, session_id="s1", input_value={"username": "user", "password": "pw"}
    )
    assert result.stage is InstagramConnectStage.CONNECTED
    # A different owner id cannot see the stored session.
    assert vault.get_view(999) is None
