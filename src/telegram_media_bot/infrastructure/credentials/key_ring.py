"""Operator key ring and high-level envelope cryptor for the credential vault (T017)."""

from __future__ import annotations

from telegram_media_bot.bootstrap.config import VaultKeyRingSection
from telegram_media_bot.domain.errors import CredentialKeyMissingError
from telegram_media_bot.domain.instagram_credentials import CredentialEnvelope
from telegram_media_bot.infrastructure.credentials.envelope import EnvelopeCodec


def _from_hex(value: str) -> bytes:
    return bytes.fromhex(value.strip())


class VaultKeyRing:
    """Holds one active encryption key and retained decrypt-only keys (never durable here)."""

    def __init__(
        self,
        *,
        active_key_id: str,
        active_key: bytes | None,
        retained_keys: dict[str, bytes],
    ) -> None:
        self._active_key_id = active_key_id
        self._active_key = active_key
        self._retained_keys = dict(retained_keys)
        all_keys = [key for key in [active_key_id, *retained_keys] if key]
        if len(all_keys) != len(set(all_keys)):
            raise ValueError("vault key IDs must be unique")

    @classmethod
    def from_config(cls, section: VaultKeyRingSection) -> VaultKeyRing:
        active: bytes | None = None
        if section.active_key is not None:
            active = _from_hex(section.active_key.get_secret_value())
        retained = {
            key_id: _from_hex(value.get_secret_value())
            for key_id, value in section.retained_keys.items()
        }
        return cls(
            active_key_id=section.active_key_id,
            active_key=active,
            retained_keys=retained,
        )

    @classmethod
    def from_hex_material(
        cls, *, active_key_id: str, active_key: str, retained_keys: dict[str, str] | None = None
    ) -> VaultKeyRing:
        return cls(
            active_key_id=active_key_id,
            active_key=_from_hex(active_key),
            retained_keys={
                key_id: _from_hex(value) for key_id, value in (retained_keys or {}).items()
            },
        )

    def active_key(self) -> tuple[str, bytes] | None:
        return (self._active_key_id, self._active_key) if self._active_key is not None else None

    def key_by_id(self, key_id: str) -> bytes | None:
        if key_id == self._active_key_id and self._active_key is not None:
            return self._active_key
        return self._retained_keys.get(key_id)

    def key_ids(self) -> tuple[str, ...]:
        ids = list(self._retained_keys)
        if self._active_key is not None:
            ids.append(self._active_key_id)
        return tuple(dict.fromkeys(ids))


class CredentialCryptor:
    """Application-facing envelope cryptor wired to the key ring + codec."""

    def __init__(self, key_ring: VaultKeyRing, codec: EnvelopeCodec | None = None) -> None:
        self._key_ring = key_ring
        self._codec = codec or EnvelopeCodec()

    def encrypt(self, plaintext: bytes, *, aad: bytes) -> CredentialEnvelope:
        active = self._key_ring.active_key()
        if active is None:
            raise CredentialKeyMissingError("no active vault encryption key is configured")
        key_id, key = active
        return self._codec.encrypt(key, plaintext, aad=aad, key_id=key_id)

    def decrypt(self, envelope: CredentialEnvelope, *, aad: bytes) -> bytes:
        key = self._key_ring.key_by_id(envelope.key_id)
        if key is None:
            raise CredentialKeyMissingError(
                "vault key for envelope is not available in the key ring"
            )
        return self._codec.decrypt(key, envelope, aad=aad)


__all__ = ["CredentialCryptor", "VaultKeyRing"]
