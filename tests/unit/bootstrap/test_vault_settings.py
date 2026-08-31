"""Vault key-ring configuration tests (T017)."""

from __future__ import annotations

import secrets

import pytest
from pydantic import ValidationError

from telegram_media_bot.bootstrap.config import VaultKeyRingSection


def _hex() -> str:
    return secrets.token_bytes(32).hex()


def test_empty_ring_valid() -> None:
    section = VaultKeyRingSection()
    assert not section.has_keys()
    assert VaultKeyRingSection.model_validate({}).active_key is None


def test_valid_active_key() -> None:
    section = VaultKeyRingSection.model_validate({"active_key_id": "k1", "active_key": _hex()})
    assert section.has_keys()


def test_active_key_requires_id() -> None:
    with pytest.raises(ValidationError):
        VaultKeyRingSection.model_validate({"active_key": _hex()})


def test_invalid_key_length_rejected() -> None:
    with pytest.raises(ValidationError):
        VaultKeyRingSection.model_validate({"active_key_id": "k1", "active_key": "abcd"})


def test_duplicate_key_ids_rejected() -> None:
    key = _hex()
    with pytest.raises(ValidationError):
        VaultKeyRingSection.model_validate(
            {"active_key_id": "k1", "active_key": key, "retained_keys": {"k1": key}}
        )


def test_retained_decrypt_only_keys_accepted() -> None:
    section = VaultKeyRingSection.model_validate(
        {
            "active_key_id": "k2",
            "active_key": _hex(),
            "retained_keys": {"k1": _hex()},
        }
    )
    assert len(section.retained_keys) == 1
