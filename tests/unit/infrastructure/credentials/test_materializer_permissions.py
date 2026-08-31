"""Materializer permission and containment tests (T017)."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

import pytest

from telegram_media_bot.application.services.credential_vault import CredentialVault
from telegram_media_bot.infrastructure.credentials.key_ring import CredentialCryptor, VaultKeyRing
from telegram_media_bot.infrastructure.credentials.materializer import RestrictedCookieMaterializer
from telegram_media_bot.infrastructure.persistence.sqlite_instagram_credentials import (
    SqliteInstagramCredentialRepository,
)


def _build(
    tmp_path: Path,
) -> tuple[SqliteInstagramCredentialRepository, CredentialVault, RestrictedCookieMaterializer]:
    repo = SqliteInstagramCredentialRepository(tmp_path / "creds.sqlite3")
    repo.initialize()
    ring = VaultKeyRing.from_hex_material(
        active_key_id="k1", active_key=secrets.token_bytes(32).hex()
    )
    cryptor = CredentialCryptor(ring)
    vault = CredentialVault(repo, cryptor)
    materializer = RestrictedCookieMaterializer(repo, cryptor)
    return repo, vault, materializer


def test_plaintext_restrictive_permissions_on_posix(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permission contract is enforced on POSIX hosts only")
    _repo, vault, materializer = _build(tmp_path)
    vault.store_session(7, b"session-bytes")
    workspace = tmp_path / "job"
    with materializer.open(owner_user_id=7, workspace=workspace) as path:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600


def test_materializer_file_contained_in_workspace(tmp_path: Path) -> None:
    _repo, vault, materializer = _build(tmp_path)
    vault.store_session(7, b"session-bytes")
    workspace = tmp_path / "job"
    with materializer.open(owner_user_id=7, workspace=workspace) as path:
        assert path.parent == workspace.resolve()
        assert path.name == "cookies.txt"


def test_materializer_rejects_adversarial_filename(tmp_path: Path) -> None:
    _repo, _vault, _m = _build(tmp_path)
    with pytest.raises(ValueError):
        RestrictedCookieMaterializer(
            _repo,
            CredentialCryptor(
                VaultKeyRing.from_hex_material(
                    active_key_id="k", active_key=secrets.token_bytes(32).hex()
                )
            ),
            filename="../escape.txt",
        )
