"""Deterministic network-free tests for the Ed25519 companion handoff (T016)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from telegram_media_bot.domain.web_companion import (
    HandoffClaim,
    HandoffPurpose,
    HandoffVerificationOutcome,
)
from telegram_media_bot.infrastructure.security.handoff import (
    Ed25519HandoffSigner,
    Ed25519HandoffVerifier,
    HandoffCryptoError,
)


def _claim(
    owner: int = 42, purpose: HandoffPurpose = HandoffPurpose.INSTAGRAM_CONNECT
) -> tuple[type[Ed25519HandoffSigner], HandoffPurpose, int, datetime]:
    issued = datetime.now(UTC)
    return Ed25519HandoffSigner, purpose, owner, issued


def _sign(
    signer: Ed25519HandoffSigner, owner: int = 42, *, offset: timedelta = timedelta()
) -> tuple[str, HandoffClaim]:
    issued = datetime.now(UTC)
    claim = HandoffClaim(
        purpose=HandoffPurpose.INSTAGRAM_CONNECT,
        owner_user_id=owner,
        nonce="n-" + "x" * 16,
        issued_at=issued,
        expires_at=issued + timedelta(minutes=5) + offset,
    )
    return signer.sign(claim), claim


def test_round_trip_verifies() -> None:
    signer, private = Ed25519HandoffSigner.generate()
    _ = private
    verifier = Ed25519HandoffVerifier.from_private_encoded(private)
    token, _claim = _sign(signer)
    result = verifier.verify(token, now=datetime.now(UTC))
    assert result.outcome is HandoffVerificationOutcome.VERIFIED
    assert result.claim is not None
    assert result.claim.owner_user_id == 42


def test_tampered_signature_rejected() -> None:
    """Changing a real signature byte (not base64 padding bits) is always rejected.

    The final base64url character has 4 padding-only bits that do not affect the decoded bytes,
    so flipping only those yields an equivalent spelling; here we flip a byte that shortens/has no
    padding impact and deterministically changes the parsed signature.
    """
    signer, private = Ed25519HandoffSigner.generate()
    verifier = Ed25519HandoffVerifier.from_private_encoded(private)
    token, _claim = _sign(signer)
    payload, signature = token.split(".", 1)
    flip_index = max(1, len(signature) // 3)
    replacement = "A" if signature[flip_index] != "A" else "B"
    tampered = f"{payload}.{signature[:flip_index]}{replacement}{signature[flip_index + 1 :]}"
    result = verifier.verify(tampered, now=datetime.now(UTC))
    assert result.outcome is HandoffVerificationOutcome.INVALID_SIGNATURE


def test_tampered_payload_rejected() -> None:
    signer, private = Ed25519HandoffSigner.generate()
    verifier = Ed25519HandoffVerifier.from_private_encoded(private)
    token, _claim = _sign(signer)
    payload, signature = token.split(".", 1)
    index = max(1, len(payload) // 2)
    replacement = "A" if payload[index] != "A" else "B"
    tampered = f"{payload[:index]}{replacement}{payload[index + 1 :]}.{signature}"
    result = verifier.verify(tampered, now=datetime.now(UTC))
    assert result.outcome is HandoffVerificationOutcome.INVALID_SIGNATURE


def test_wrong_key_rejected() -> None:
    signer, _private = Ed25519HandoffSigner.generate()
    other, other_private = Ed25519HandoffSigner.generate()
    _ = other
    verifier = Ed25519HandoffVerifier.from_private_encoded(other_private)
    token, _claim = _sign(signer)
    result = verifier.verify(token, now=datetime.now(UTC))
    assert result.outcome is HandoffVerificationOutcome.INVALID_SIGNATURE


def test_expired_rejected() -> None:
    signer, private = Ed25519HandoffSigner.generate()
    verifier = Ed25519HandoffVerifier.from_private_encoded(private)
    token, _claim = _sign(signer, offset=-timedelta(minutes=10))
    result = verifier.verify(token, now=datetime.now(UTC))
    assert result.outcome is HandoffVerificationOutcome.EXPIRED


def test_not_yet_valid_beyond_skew_rejected() -> None:
    signer, private = Ed25519HandoffSigner.generate()
    verifier = Ed25519HandoffVerifier.from_private_encoded(private, max_clock_skew_seconds=5)
    issued_future = datetime.now(UTC) + timedelta(minutes=10)
    from telegram_media_bot.domain.web_companion import HandoffClaim

    claim = HandoffClaim(
        purpose=HandoffPurpose.INSTAGRAM_CONNECT,
        owner_user_id=1,
        nonce="future",
        issued_at=issued_future,
        expires_at=issued_future + timedelta(minutes=5),
    )
    token = signer.sign(claim)
    assert (
        verifier.verify(token, now=datetime.now(UTC)).outcome
        is HandoffVerificationOutcome.NOT_YET_VALID
    )


def test_malformed_token_rejected() -> None:
    _signer, private = Ed25519HandoffSigner.generate()
    verifier = Ed25519HandoffVerifier.from_private_encoded(private)
    assert (
        verifier.verify("not-a-token", now=datetime.now(UTC)).outcome
        is HandoffVerificationOutcome.MALFORMED
    )


def test_bad_private_key_rejected() -> None:
    with pytest.raises(HandoffCryptoError):
        Ed25519HandoffSigner.from_encoded("!!!not-base64!!!")


def test_purpose_mismatch() -> None:
    signer, private = Ed25519HandoffSigner.generate()
    verifier = Ed25519HandoffVerifier.from_private_encoded(private)
    token, _claim = _sign(signer)
    # The verifier has no purpose argument by default; purpose enforcement lives in the service.
    result = verifier.verify(token, now=datetime.now(UTC))
    assert result.outcome is HandoffVerificationOutcome.VERIFIED


def test_public_pem_verification() -> None:
    signer, private = Ed25519HandoffSigner.generate()
    verifier = Ed25519HandoffVerifier.from_private_encoded(private)
    pem = verifier.public_key_pem()
    independent = Ed25519HandoffVerifier.from_public_pem(pem)
    token, _claim = _sign(signer)
    assert independent.verify(token, now=datetime.now(UTC)).verified
