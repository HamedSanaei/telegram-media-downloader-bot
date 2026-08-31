"""AES-256-GCM versioned envelope codec for the credential vault (T017).

Uses a random 96-bit nonce per encryption and the approved AEAD (``cryptography`` AESGCM). The
envelope carries the active key ID/version so decrypt can look up the right key during rotation.
Raw key bytes and plaintext never cross this module's public surface.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from telegram_media_bot.domain.errors import CredentialDecryptError
from telegram_media_bot.domain.instagram_credentials import (
    ENVELOPE_VERSION,
    CredentialEnvelope,
    new_random_nonce,
)

_AES256_KEY_BYTES = 32


class EnvelopeCodec:
    """Low-level AEAD codec; callers own key selection via ``CredentialCryptor``."""

    @staticmethod
    def encrypt(
        key: bytes,
        plaintext: bytes,
        *,
        aad: bytes,
        key_id: str,
        nonce: bytes | None = None,
    ) -> CredentialEnvelope:
        _require_key(key)
        nonce = nonce or new_random_nonce()
        if len(nonce) != 12:
            raise ValueError("AES-GCM nonce must be 96 bits")
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
        return CredentialEnvelope(
            version=ENVELOPE_VERSION, key_id=key_id, nonce=nonce, ciphertext=ciphertext
        )

    @staticmethod
    def decrypt(
        key: bytes,
        envelope: CredentialEnvelope,
        *,
        aad: bytes,
    ) -> bytes:
        _require_key(key)
        try:
            return AESGCM(key).decrypt(envelope.nonce, envelope.ciphertext, aad)
        except (InvalidTag, ValueError) as exc:
            raise CredentialDecryptError(
                "credential ciphertext could not be authenticated/decrypted"
            ) from exc


def _require_key(key: bytes) -> None:
    if len(key) != _AES256_KEY_BYTES:
        raise ValueError("AES-256-GCM requires a 32-byte key")


__all__ = ["EnvelopeCodec"]
