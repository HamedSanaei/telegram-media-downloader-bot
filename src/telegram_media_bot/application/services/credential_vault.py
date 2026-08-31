"""Owner-bound encrypted Instagram credential vault service (T017).

Coordinates safe lifecycle: first connect, re-connect (generation++), expiry/challenge markers,
disconnect/revoke (immediate ciphertext erase), and key-rotation re-encryption. Only sanitized
views/events leave this service; plaintext and keys exist only inside the vault adapter.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime

from telegram_media_bot.domain.errors import (
    CredentialNotFoundError,
)
from telegram_media_bot.domain.instagram_credentials import (
    CREDENTIAL_PROVIDER,
    CredentialEvent,
    CredentialEventKind,
    InstagramCredential,
    InstagramCredentialState,
    SafeCredentialView,
    aad_for,
    new_credential_id,
)
from telegram_media_bot.infrastructure.credentials.key_ring import CredentialCryptor
from telegram_media_bot.infrastructure.persistence.sqlite_instagram_credentials import (
    SqliteInstagramCredentialRepository,
)


def _now() -> datetime:
    return datetime.now(UTC)


class CredentialVault:
    def __init__(self, repository: SqliteInstagramCredentialRepository, cryptor: CredentialCryptor):
        self._repository = repository
        self._cryptor = cryptor

    def store_session(self, owner_user_id: int, plaintext_bytes: bytes) -> SafeCredentialView:
        """Store/refresh an encrypted session, incrementing generation.

        ``plaintext_bytes`` is the user's Instagram session cookie material (Netscape content).
        It is encrypted with a random nonce under the active key bound to owner/generation and
        never persisted in plaintext.
        """
        existing = self._repository.get_credential_for_owner(owner_user_id)
        was_reconnect = existing is not None
        credential_id = existing.credential_id if existing is not None else new_credential_id()
        generation = (existing.generation if existing is not None else 0) + 1
        now = _now()
        aad = aad_for(
            provider=CREDENTIAL_PROVIDER,
            credential_id=credential_id,
            owner_user_id=owner_user_id,
            generation=generation,
        )
        envelope = self._cryptor.encrypt(plaintext_bytes, aad=aad)
        credential = InstagramCredential(
            credential_id=credential_id,
            provider=CREDENTIAL_PROVIDER,
            owner_user_id=owner_user_id,
            state=InstagramCredentialState.CONNECTED,
            generation=generation,
            envelope=envelope,
            last_verified_at=now,
            last_success_at=now,
            last_failure_category=None,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )
        self._repository.save_credential(credential)
        kind = CredentialEventKind.RECONNECTED if was_reconnect else CredentialEventKind.CONNECTED
        self._repository.append_event(
            CredentialEvent(
                event_id=str(uuid.uuid4()),
                credential_id=credential_id,
                owner_user_id=owner_user_id,
                kind=kind,
                generation=generation,
                created_at=now,
                actor_role="user",
            )
        )
        return self._to_view(credential)

    def get_view(self, owner_user_id: int) -> SafeCredentialView | None:
        credential = self._repository.get_credential_for_owner(owner_user_id)
        return self._to_view(credential) if credential is not None else None

    def set_state_category(
        self,
        owner_user_id: int,
        state: InstagramCredentialState,
        *,
        failure_category: str | None = None,
    ) -> SafeCredentialView:
        credential = self._require(owner_user_id)
        now = _now()
        updated = _with(credential, state=state, updated_at=now)
        if failure_category:
            updated = replace(updated, last_failure_category=failure_category)
        self._repository.save_credential(updated)
        kind = _event_kind_for_state(state)
        if kind is not None:
            self._repository.append_event(
                CredentialEvent(
                    event_id=str(uuid.uuid4()),
                    credential_id=credential.credential_id,
                    owner_user_id=owner_user_id,
                    kind=kind,
                    generation=credential.generation,
                    created_at=now,
                    actor_role="user",
                )
            )
        return self._to_view(updated)

    def disconnect(self, owner_user_id: int) -> SafeCredentialView:
        credential = self._require(owner_user_id)
        now = _now()
        updated = InstagramCredential(
            credential_id=credential.credential_id,
            provider=credential.provider,
            owner_user_id=credential.owner_user_id,
            state=InstagramCredentialState.DISCONNECTED,
            generation=credential.generation,
            envelope=None,
            last_verified_at=credential.last_verified_at,
            last_success_at=credential.last_success_at,
            last_failure_category=credential.last_failure_category,
            created_at=credential.created_at,
            updated_at=now,
        )
        self._repository.save_credential(updated)
        self._repository.append_event(
            CredentialEvent(
                event_id=str(uuid.uuid4()),
                credential_id=credential.credential_id,
                owner_user_id=owner_user_id,
                kind=CredentialEventKind.DISCONNECTED,
                generation=credential.generation,
                created_at=now,
                actor_role="user",
            )
        )
        return self._to_view(updated)

    def revoke(self, owner_user_id: int, *, actor_role: str) -> SafeCredentialView:
        credential = self._require(owner_user_id)
        now = _now()
        updated = InstagramCredential(
            credential_id=credential.credential_id,
            provider=credential.provider,
            owner_user_id=credential.owner_user_id,
            state=InstagramCredentialState.REVOKED,
            generation=credential.generation,
            envelope=None,
            last_verified_at=credential.last_verified_at,
            last_success_at=credential.last_success_at,
            last_failure_category=credential.last_failure_category,
            created_at=credential.created_at,
            updated_at=now,
        )
        self._repository.save_credential(updated)
        self._repository.append_event(
            CredentialEvent(
                event_id=str(uuid.uuid4()),
                credential_id=credential.credential_id,
                owner_user_id=owner_user_id,
                kind=CredentialEventKind.REVOKED,
                generation=credential.generation,
                created_at=now,
                actor_role=actor_role,
            )
        )
        return self._to_view(updated)

    def rotate(self, owner_user_id: int) -> SafeCredentialView:
        """Re-encrypt the stored session under the current active key (operator rotation)."""
        credential = self._require(owner_user_id)
        if credential.envelope is None:
            raise CredentialNotFoundError("credential holds no ciphertext to rotate")
        aad = aad_for(
            provider=credential.provider,
            credential_id=credential.credential_id,
            owner_user_id=credential.owner_user_id,
            generation=credential.generation,
        )
        plaintext = self._cryptor.decrypt(credential.envelope, aad=aad)
        new_envelope = self._cryptor.encrypt(plaintext, aad=aad)
        now = _now()
        updated = replace(credential, envelope=new_envelope, updated_at=now)
        self._repository.save_credential(updated)
        self._repository.append_event(
            CredentialEvent(
                event_id=str(uuid.uuid4()),
                credential_id=credential.credential_id,
                owner_user_id=owner_user_id,
                kind=CredentialEventKind.ROTATED,
                generation=credential.generation,
                created_at=now,
                actor_role="admin",
            )
        )
        return self._to_view(updated)

    def _require(self, owner_user_id: int) -> InstagramCredential:
        credential = self._repository.get_credential_for_owner(owner_user_id)
        if credential is None:
            raise CredentialNotFoundError("no credential exists for the owner")
        return credential

    @staticmethod
    def _to_view(credential: InstagramCredential) -> SafeCredentialView:
        return SafeCredentialView(
            state=credential.state,
            generation=credential.generation,
            last_verified_at=credential.last_verified_at,
            last_success_at=credential.last_success_at,
            last_failure_category=credential.last_failure_category,
        )


def _with(
    credential: InstagramCredential, *, state: InstagramCredentialState, updated_at: datetime
) -> InstagramCredential:
    return replace(credential, state=state, updated_at=updated_at)


def _event_kind_for_state(state: InstagramCredentialState) -> CredentialEventKind | None:
    mapping = {
        InstagramCredentialState.EXPIRED: CredentialEventKind.EXPIRED,
        InstagramCredentialState.CHALLENGE_REQUIRED: CredentialEventKind.CHALLENGE_REQUIRED,
    }
    return mapping.get(state)


__all__ = ["CredentialVault"]
