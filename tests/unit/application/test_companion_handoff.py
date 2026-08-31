"""Exactly-once companion handoff exchange tests (T016)."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram_media_bot.application.services.handoff import (
    CompanionHandoffService,
    HandoffLinkService,
)
from telegram_media_bot.domain.web_companion import (
    HandoffPurpose,
    HandoffVerificationOutcome,
)
from telegram_media_bot.infrastructure.persistence.sqlite_handoff import (
    SqliteHandoffNonceRepository,
)
from telegram_media_bot.infrastructure.security.handoff import (
    Ed25519HandoffSigner,
    Ed25519HandoffVerifier,
)


def _service(
    tmp_path: Path, *, skew: int = 30
) -> tuple[CompanionHandoffService, HandoffLinkService]:
    _signer, private = Ed25519HandoffSigner.generate()
    verifier = Ed25519HandoffVerifier.from_private_encoded(private, max_clock_skew_seconds=skew)
    signer2 = Ed25519HandoffSigner.from_encoded(private)
    repo = SqliteHandoffNonceRepository(tmp_path / "handoff.sqlite3")
    repo.initialize()
    link = HandoffLinkService(signer2, lifetime=timedelta(minutes=5))
    service = CompanionHandoffService(verifier=verifier, nonce_repository=repo)
    return service, link


def test_exchange_success_and_replay(tmp_path: Path) -> None:
    service, link = _service(tmp_path)
    token = link.create(purpose=HandoffPurpose.INSTAGRAM_CONNECT, owner_user_id=7)
    first = service.exchange(token, purpose=HandoffPurpose.INSTAGRAM_CONNECT)
    assert first.outcome is HandoffVerificationOutcome.VERIFIED
    replay = service.exchange(token, purpose=HandoffPurpose.INSTAGRAM_CONNECT)
    assert replay.outcome is HandoffVerificationOutcome.REPLAYED


def test_expired_token_rejected_without_consuming(tmp_path: Path) -> None:
    _signer, private = Ed25519HandoffSigner.generate()
    verifier = Ed25519HandoffVerifier.from_private_encoded(private)
    repo = SqliteHandoffNonceRepository(tmp_path / "handoff.sqlite3")
    repo.initialize()
    link = HandoffLinkService(
        Ed25519HandoffSigner.from_encoded(private), lifetime=timedelta(minutes=5)
    )
    token = link.create(purpose=HandoffPurpose.INSTAGRAM_CONNECT, owner_user_id=7)
    now = datetime.now(UTC) + timedelta(minutes=30)
    service = CompanionHandoffService(verifier=verifier, nonce_repository=repo)
    result = service.exchange(token, purpose=HandoffPurpose.INSTAGRAM_CONNECT, now=now)
    assert result.outcome is HandoffVerificationOutcome.EXPIRED


def test_concurrent_exchange_consumed_once(tmp_path: Path) -> None:
    service, link = _service(tmp_path)
    token = link.create(purpose=HandoffPurpose.INSTAGRAM_CONNECT, owner_user_id=7)

    async def run() -> list[HandoffVerificationOutcome]:
        results = await asyncio.gather(
            *[
                asyncio.to_thread(service.exchange, token, HandoffPurpose.INSTAGRAM_CONNECT)
                for _ in range(8)
            ]
        )
        return [r.outcome for r in results]

    outcomes = asyncio.run(run())
    assert outcomes.count(HandoffVerificationOutcome.VERIFIED) == 1
    assert outcomes.count(HandoffVerificationOutcome.REPLAYED) == 7


def test_nonce_repo_purge(tmp_path: Path) -> None:
    repo = SqliteHandoffNonceRepository(tmp_path / "handoff.sqlite3")
    repo.initialize()
    now = datetime.now(UTC)
    assert repo.reserve_once(
        nonce_hash="hash-old",
        purpose=HandoffPurpose.INSTAGRAM_CONNECT,
        owner_user_id=1,
        expires_at=now - timedelta(hours=2),
        now=now,
    )
    assert repo.reserve_once(
        nonce_hash="hash-fresh",
        purpose=HandoffPurpose.INSTAGRAM_CONNECT,
        owner_user_id=1,
        expires_at=now + timedelta(hours=2),
        now=now,
    )
    removed = repo.purge_expired(now=now, before=now - timedelta(hours=1))
    assert removed == 1
    with sqlite3.connect(tmp_path / "handoff.sqlite3") as connection:
        remaining = connection.execute(
            "SELECT nonce_hash FROM handoff_nonce_consumptions"
        ).fetchall()
    assert {row[0] for row in remaining} == {"hash-fresh"}
