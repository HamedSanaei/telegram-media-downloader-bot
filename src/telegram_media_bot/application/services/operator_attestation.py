"""Operator public-only attestation service (ADR-034, T019)."""

from __future__ import annotations

from datetime import UTC, datetime

from telegram_media_bot.domain.credential_resolution import (
    PublicOnlyAttestation,
    operator_ig_records_verifier,
)
from telegram_media_bot.domain.errors import (
    OperatorAttestationStaleError,
    OperatorUnattestedError,
)
from telegram_media_bot.infrastructure.persistence.sqlite_operator_attestation import (
    SqliteOperatorAttestationRepository,
)


class OperatorPublicAttestationService:
    """Attest/invalidate the dedicated zero-follow operator Instagram account.

    An explicit operator action records an attestation bound to a keyed verifier of the canonical
    file's Instagram records. Any later replacement/tamper of those records makes the attestation
    stale, and operator-backed Instagram routing fails closed (ADR-034).
    """

    def __init__(
        self,
        repository: SqliteOperatorAttestationRepository,
        *,
        attestation_key: bytes | None = None,
    ) -> None:
        self._repository = repository
        self._key = attestation_key

    def attest(
        self,
        *,
        instagram_records: tuple[str, ...],
        actor_role: str,
        following_count: int | None,
        identity_verified: bool,
        now: datetime | None = None,
    ) -> PublicOnlyAttestation:
        """Persist an attestation only after an explicit zero-follow identity check."""
        if not identity_verified or following_count != 0:
            raise OperatorUnattestedError(
                "operator Instagram identity or zero-follow status could not be verified"
            )
        current = self._repository.get_current()
        generation = (current.operator_generation if current is not None else 0) + 1
        verifier = operator_ig_records_verifier(instagram_records, key=self._key)
        attestation = PublicOnlyAttestation(
            operator_generation=generation,
            attested_at=now or datetime.now(UTC),
            actor_role=actor_role,
            keyed_verifier=verifier,
        )
        self._repository.save_attestation(attestation)
        return attestation

    def require_valid(self, *, instagram_records: tuple[str, ...]) -> PublicOnlyAttestation:
        current = self._repository.get_current()
        if current is None:
            raise OperatorUnattestedError("operator Instagram account is not attested as public")
        verifier = operator_ig_records_verifier(instagram_records, key=self._key)
        if verifier != current.keyed_verifier:
            raise OperatorAttestationStaleError(
                "operator Instagram cookie records changed after attestation"
            )
        return current

    def current_generation(self) -> int | None:
        current = self._repository.get_current()
        return current.operator_generation if current is not None else None

    def is_valid(self, *, instagram_records: tuple[str, ...]) -> bool:
        try:
            self.require_valid(instagram_records=instagram_records)
            return True
        except OperatorUnattestedError, OperatorAttestationStaleError:
            return False


__all__ = ["OperatorPublicAttestationService"]
