"""AEAD envelope and key-ring tests (T017)."""

from __future__ import annotations

import secrets

import pytest

from telegram_media_bot.domain.errors import CredentialDecryptError, CredentialKeyMissingError
from telegram_media_bot.domain.instagram_credentials import CredentialEnvelope, aad_for
from telegram_media_bot.infrastructure.credentials.envelope import EnvelopeCodec
from telegram_media_bot.infrastructure.credentials.key_ring import CredentialCryptor, VaultKeyRing


def _key_hex() -> str:
    return secrets.token_bytes(32).hex()


def _aad() -> bytes:
    return aad_for(provider="instagram", credential_id="c1", owner_user_id=7, generation=1)


def test_round_trip() -> None:
    key = secrets.token_bytes(32)
    envelope = EnvelopeCodec.encrypt(key, b"session-bytes", aad=_aad(), key_id="k1")
    assert EnvelopeCodec.decrypt(key, envelope, aad=_aad()) == b"session-bytes"


def test_nonce_is_random_per_encryption() -> None:
    key = secrets.token_bytes(32)
    env1 = EnvelopeCodec.encrypt(key, b"data", aad=_aad(), key_id="k1")
    env2 = EnvelopeCodec.encrypt(key, b"data", aad=_aad(), key_id="k1")
    assert env1.nonce != env2.nonce
    assert env1.ciphertext != env2.ciphertext


def test_aad_tamper_rejected() -> None:
    key = secrets.token_bytes(32)
    envelope = EnvelopeCodec.encrypt(key, b"data", aad=_aad(), key_id="k1")
    wrong = aad_for(provider="instagram", credential_id="OTHER", owner_user_id=7, generation=1)
    with pytest.raises(CredentialDecryptError):
        EnvelopeCodec.decrypt(key, envelope, aad=wrong)


def test_corrupt_ciphertext_rejected() -> None:
    key = secrets.token_bytes(32)
    envelope = EnvelopeCodec.encrypt(key, b"data" * 8, aad=_aad(), key_id="k1")
    flipped = (
        envelope.ciphertext[:-2]
        + bytes([envelope.ciphertext[-2] ^ 0xFF])
        + envelope.ciphertext[-1:]
    )
    bad = envelope.__class__(
        version=envelope.version, key_id=envelope.key_id, nonce=envelope.nonce, ciphertext=flipped
    )
    with pytest.raises(CredentialDecryptError):
        EnvelopeCodec.decrypt(key, bad, aad=_aad())


def test_wrong_key_rejected() -> None:
    env = EnvelopeCodec.encrypt(secrets.token_bytes(32), b"data", aad=_aad(), key_id="k1")
    with pytest.raises(CredentialDecryptError):
        EnvelopeCodec.decrypt(secrets.token_bytes(32), env, aad=_aad())


def test_key_ring_encrypt_and_rotation_decrypt() -> None:
    k1, k2 = _key_hex(), _key_hex()
    ring1 = VaultKeyRing.from_hex_material(active_key_id="k1", active_key=k1)
    cryptor1 = CredentialCryptor(ring1)
    envelope = cryptor1.encrypt(b"vault-bytes", aad=_aad())
    assert envelope.key_id == "k1"

    # Rotation: k2 becomes active, k1 retained for decrypt-only.
    ring2 = VaultKeyRing.from_hex_material(
        active_key_id="k2", active_key=k2, retained_keys={"k1": k1}
    )
    cryptor2 = CredentialCryptor(ring2)
    assert cryptor2.decrypt(envelope, aad=_aad()) == b"vault-bytes"
    refreshed = cryptor2.encrypt(b"vault-bytes", aad=_aad())
    assert refreshed.key_id == "k2"


def test_unknown_key_raises_key_missing() -> None:
    key = secrets.token_bytes(32)
    ring = VaultKeyRing.from_hex_material(active_key_id="now", active_key=key.hex())
    envelope = EnvelopeCodec.encrypt(key, b"data", aad=_aad(), key_id="vanished")
    with pytest.raises(CredentialKeyMissingError):
        CredentialCryptor(ring).decrypt(envelope, aad=_aad())


def test_envelope_serialization_round_trip() -> None:
    key = secrets.token_bytes(32)
    envelope = EnvelopeCodec.encrypt(key, b"data", aad=_aad(), key_id="k1")
    parsed = CredentialEnvelope.parse(envelope.serialized())
    assert parsed == envelope
