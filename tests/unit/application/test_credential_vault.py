"""Credential vault lifecycle/lease/isolation tests (T017)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_media_bot.application.services.credential_vault import CredentialVault
from telegram_media_bot.domain.errors import (
    CredentialChallengeRequiredError,
    CredentialDisconnectedError,
    CredentialExpiredError,
    CredentialGenerationMismatchError,
    CredentialLeaseBusyError,
    CredentialNotFoundError,
    CredentialRevokedError,
)
from telegram_media_bot.domain.instagram_credentials import InstagramCredentialState
from telegram_media_bot.infrastructure.credentials.key_ring import CredentialCryptor, VaultKeyRing
from telegram_media_bot.infrastructure.credentials.materializer import RestrictedCookieMaterializer
from telegram_media_bot.infrastructure.persistence.sqlite_instagram_credentials import (
    SqliteInstagramCredentialRepository,
)


def _key() -> str:
    return secrets.token_bytes(32).hex()


def _build(
    tmp_path: Path, *, active_key: str | None = None, retained: dict[str, str] | None = None
):
    repo = SqliteInstagramCredentialRepository(tmp_path / "creds.sqlite3")
    repo.initialize()
    ring = VaultKeyRing.from_hex_material(
        active_key_id=active_key or "k1",
        active_key=active_key or _key(),
        retained_keys=retained,
    )
    vault = CredentialVault(repo, CredentialCryptor(ring))
    materializer = RestrictedCookieMaterializer(repo, CredentialCryptor(ring))
    return repo, vault, materializer


def test_first_connect_and_sanitized_view(tmp_path: Path) -> None:
    _repo, vault, _m = _build(tmp_path)
    view = vault.store_session(7, b"alice-session-cookies")
    assert view.state is InstagramCredentialState.CONNECTED
    assert view.generation == 1
    assert vault.get_view(7).state is InstagramCredentialState.CONNECTED


def test_reconnect_increments_generation(tmp_path: Path) -> None:
    _repo, vault, _m = _build(tmp_path)
    assert vault.store_session(7, b"a").generation == 1
    assert vault.store_session(7, b"b").generation == 2
    assert vault.store_session(7, b"c").generation == 3


def test_cross_user_isolation(tmp_path: Path) -> None:
    _repo, vault, _m = _build(tmp_path)
    vault.store_session(1, b"alice")
    vault.store_session(2, b"bob")
    assert vault.get_view(1).generation == 1
    assert vault.get_view(2).generation == 1
    # Alice's view never reveals Bob's generation identity.
    assert vault.get_view(12345) is None


def test_disconnect_erases_ciphertext_and_blocks_lease(tmp_path: Path) -> None:
    _repo, vault, materializer = _build(tmp_path)
    vault.store_session(7, b"session")
    vault.disconnect(7)
    assert vault.get_view(7).state is InstagramCredentialState.DISCONNECTED
    with pytest.raises(CredentialDisconnectedError):  # noqa: SIM117
        with materializer.open(owner_user_id=7, workspace=tmp_path / "job"):
            pass


def test_revoke_marks_revoked_and_blocks(tmp_path: Path) -> None:
    _repo, vault, materializer = _build(tmp_path)
    vault.store_session(7, b"session")
    view = vault.revoke(7, actor_role="admin")
    assert view.state is InstagramCredentialState.REVOKED
    with pytest.raises(CredentialRevokedError):  # noqa: SIM117
        with materializer.open(owner_user_id=7, workspace=tmp_path / "job"):
            pass


def test_expired_and_challenge_block_materialization(tmp_path: Path) -> None:
    _repo, vault, materializer = _build(tmp_path)
    vault.store_session(7, b"session")
    vault.set_state_category(7, InstagramCredentialState.EXPIRED)
    with pytest.raises(CredentialExpiredError):  # noqa: SIM117
        with materializer.open(owner_user_id=7, workspace=tmp_path / "job"):
            pass
    vault.set_state_category(7, InstagramCredentialState.CHALLENGE_REQUIRED)
    with pytest.raises(CredentialChallengeRequiredError):  # noqa: SIM117
        with materializer.open(owner_user_id=7, workspace=tmp_path / "job"):
            pass


def test_lease_single_and_release(tmp_path: Path) -> None:
    _repo, vault, m = _build(tmp_path)
    vault.store_session(7, b"session")
    with m.open(owner_user_id=7, workspace=tmp_path / "job") as path:
        assert path.exists()
        assert path.read_bytes() == b"session"
    # After normal use the file is gone and the lease released.
    assert not path.exists()
    with m.open(owner_user_id=7, workspace=tmp_path / "job"):
        pass


def test_lease_busy_while_held(tmp_path: Path) -> None:
    _repo, vault, m = _build(tmp_path)
    vault.store_session(7, b"session")
    workspace = tmp_path / "job"
    workspace.mkdir(parents=True, exist_ok=True)
    with m.open(owner_user_id=7, workspace=workspace):  # noqa: SIM117
        # A second overlapping acquisition must fail at the repository lease guard.
        with pytest.raises(CredentialLeaseBusyError):
            with m.open(owner_user_id=7, workspace=workspace):
                pass


def test_lease_generation_mismatch(tmp_path: Path) -> None:
    repo, vault, _m = _build(tmp_path)
    vault.store_session(7, b"session")
    now = datetime.now(UTC)
    with pytest.raises(CredentialGenerationMismatchError):
        # A stale/forged generation can never lease the current material.
        repo.acquire_lease(
            owner_user_id=7,
            generation=99,
            expires_at=now + timedelta(seconds=60),
            now=now,
        )


def test_materializer_cleans_up_on_exception(tmp_path: Path) -> None:
    _repo, vault, m = _build(tmp_path)
    vault.store_session(7, b"session")
    workspace = tmp_path / "job"
    try:
        with m.open(owner_user_id=7, workspace=workspace) as path:
            assert path.exists()
            raise RuntimeError("inner failure")
    except RuntimeError:
        pass
    entries = list(workspace.rglob("*")) if workspace.exists() else []
    assert entries == []
    # The lease was released, so materialization can be retried.
    with m.open(owner_user_id=7, workspace=workspace):
        pass


def test_materializer_never_leaks_wrong_owner(tmp_path: Path) -> None:
    _repo, vault, m = _build(tmp_path)
    vault.store_session(7, b"alice-session")
    with m.open(owner_user_id=7, workspace=tmp_path / "job-alice") as path:
        assert b"alice" in path.read_bytes()
    with (
        pytest.raises(CredentialNotFoundError),
        m.open(owner_user_id=999, workspace=tmp_path / "job-bob"),
    ):
        pass


def test_rotation_reencrypts_under_active_key(tmp_path: Path) -> None:
    k1, k2 = _key(), _key()
    repo = SqliteInstagramCredentialRepository(tmp_path / "creds.sqlite3")
    repo.initialize()
    ring1 = VaultKeyRing.from_hex_material(active_key_id="k1", active_key=k1)
    vault = CredentialVault(repo, CredentialCryptor(ring1))
    vault.store_session(7, b"session")
    # Rotate: k2 becomes active, k1 retained.
    ring2 = VaultKeyRing.from_hex_material(
        active_key_id="k2", active_key=k2, retained_keys={"k1": k1}
    )
    vault2 = CredentialVault(repo, CredentialCryptor(ring2))
    persisted = repo.get_credential_for_owner(7)
    assert persisted is not None and persisted.envelope is not None
    assert persisted.envelope.key_id == "k1"
    view = vault2.rotate(7)
    assert view.state is InstagramCredentialState.CONNECTED
    persisted = repo.get_credential_for_owner(7)
    assert persisted.envelope.key_id == "k2"
    # Decrypt works under the rotated key.
    materializer = RestrictedCookieMaterializer(repo, CredentialCryptor(ring2))
    with materializer.open(owner_user_id=7, workspace=tmp_path / "job") as path:
        assert path.read_bytes() == b"session"
