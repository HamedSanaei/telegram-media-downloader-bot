"""Instagram connection service tests (T018)."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram_media_bot.application.services.credential_vault import CredentialVault
from telegram_media_bot.application.services.handoff import HandoffLinkService
from telegram_media_bot.application.services.instagram_connection import InstagramConnectionService
from telegram_media_bot.domain.instagram_credentials import InstagramCredentialState
from telegram_media_bot.infrastructure.credentials.key_ring import CredentialCryptor, VaultKeyRing
from telegram_media_bot.infrastructure.instagram_login.fake import FakeInstagramSessionAcquirer
from telegram_media_bot.infrastructure.persistence.sqlite_instagram_credentials import (
    SqliteInstagramCredentialRepository,
)
from telegram_media_bot.infrastructure.security.handoff import (
    Ed25519HandoffSigner,
    Ed25519HandoffVerifier,
)


def _build(
    tmp_path: Path, *, challenge: bool = False, reject: bool = False
) -> tuple[SqliteInstagramCredentialRepository, CredentialVault, InstagramConnectionService, str]:
    repo = SqliteInstagramCredentialRepository(tmp_path / "creds.sqlite3")
    repo.initialize()
    ring = VaultKeyRing.from_hex_material(
        active_key_id="k", active_key=secrets.token_bytes(32).hex()
    )
    vault = CredentialVault(repo, CredentialCryptor(ring))
    _signer, private = Ed25519HandoffSigner.generate()
    link = HandoffLinkService(
        Ed25519HandoffSigner.from_encoded(private), lifetime=timedelta(minutes=5)
    )
    service = InstagramConnectionService(
        link_service=link,
        connect_base_url="https://connect.example.test",
        vault=vault,
        acquirer=FakeInstagramSessionAcquirer(challenge_required=challenge, reject_always=reject),
    )
    return repo, vault, service, private


def test_connect_link_contains_signed_handoff_fragment(tmp_path: Path) -> None:
    _repo, _vault, service, private = _build(tmp_path)
    link = service.create_connect_link(7)
    assert link.startswith("https://connect.example.test/instagram/connect#handoff=")
    token = link.rsplit("=", 1)[1]
    verifier = Ed25519HandoffVerifier.from_private_encoded(private)
    assert verifier.verify(token, now=datetime.now(UTC)).verified


def test_submit_login_stores_encrypted_session(tmp_path: Path) -> None:
    _repo, vault, service, _private = _build(tmp_path)
    result = service.submit_login(7, password="safe-password")  # pragma: allowlist secret
    assert result.connected
    view = vault.get_view(7)
    assert view is not None and view.state is InstagramCredentialState.CONNECTED
    assert view.generation == 1


def test_password_twofa_never_durable(tmp_path: Path) -> None:
    repo, _vault, service, _private = _build(tmp_path, challenge=True)
    service.submit_login(
        7,
        password="topsecret-pw",  # pragma: allowlist secret
        twofa_code="123456",  # pragma: allowlist secret
    )
    raw = repo.get_credential_for_owner(7)
    assert raw is not None and raw.envelope is not None
    # The envelope is ciphertext; the DB rows never contain the plaintext secret.
    with sqlite3.connect(tmp_path / "creds.sqlite3") as connection:
        dumped = "".join(
            str(row) for row in connection.execute("SELECT * FROM instagram_credentials")
        )
        dumped += "".join(
            str(row) for row in connection.execute("SELECT * FROM instagram_credential_events")
        )
    assert "topsecret-pw" not in dumped
    assert "123456" not in dumped


def test_twofa_challenge_then_connected(tmp_path: Path) -> None:
    _repo, vault, service, _private = _build(tmp_path, challenge=True)
    first = service.submit_login(7, password="pw")  # pragma: allowlist secret
    assert first.stage.value == "need_2fa"
    second = service.submit_login(7, password="pw", twofa_code="654321")  # pragma: allowlist secret
    assert second.connected
    current = vault.get_view(7)
    assert current is not None and current.state is InstagramCredentialState.CONNECTED


def test_deny_result(tmp_path: Path) -> None:
    _repo, _vault, service, _private = _build(tmp_path, reject=True)
    result = service.submit_login(7, password="anything")  # pragma: allowlist secret
    assert result.stage.value == "denied"


def test_empty_password_denied(tmp_path: Path) -> None:
    _repo, _vault, service, _private = _build(tmp_path)
    result = service.submit_login(7)
    assert result.stage.value == "denied"


def test_disconnect(tmp_path: Path) -> None:
    _repo, vault, service, _private = _build(tmp_path)
    service.submit_login(7, password="pw")  # pragma: allowlist secret
    view = service.disconnect(7)
    assert view.state is InstagramCredentialState.DISCONNECTED
    current = vault.get_view(7)
    assert current is not None and current.state is InstagramCredentialState.DISCONNECTED
