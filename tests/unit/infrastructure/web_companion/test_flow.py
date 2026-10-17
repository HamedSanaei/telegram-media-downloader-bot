"""Companion transient Instagram connection flow tests (T018)."""

from __future__ import annotations

import asyncio
import secrets
import threading
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

import pytest

from telegram_media_bot.application.services.credential_vault import CredentialVault
from telegram_media_bot.application.services.handoff import HandoffLinkService
from telegram_media_bot.application.services.instagram_connection import InstagramConnectionService
from telegram_media_bot.domain.instagram_connection import InstagramLoginResult
from telegram_media_bot.domain.instagram_credentials import InstagramCredentialState
from telegram_media_bot.domain.web_companion import InstagramConnectStage
from telegram_media_bot.infrastructure.credentials.key_ring import CredentialCryptor, VaultKeyRing
from telegram_media_bot.infrastructure.instagram_login.fake import FakeInstagramSessionAcquirer
from telegram_media_bot.infrastructure.persistence.sqlite_instagram_credentials import (
    SqliteInstagramCredentialRepository,
)
from telegram_media_bot.infrastructure.security.handoff import Ed25519HandoffSigner
from telegram_media_bot.infrastructure.web_companion.flow import CompanionInstagramConnectionFlow


def _flow(
    tmp_path: Path, *, challenge: bool = False, reject: bool = False
) -> tuple[SqliteInstagramCredentialRepository, CredentialVault, CompanionInstagramConnectionFlow]:
    repo = SqliteInstagramCredentialRepository(tmp_path / "creds.sqlite3")
    repo.initialize()
    ring = VaultKeyRing.from_hex_material(
        active_key_id="k", active_key=secrets.token_bytes(32).hex()
    )
    vault = CredentialVault(repo, CredentialCryptor(ring))
    _s, private = Ed25519HandoffSigner.generate()
    link = HandoffLinkService(
        Ed25519HandoffSigner.from_encoded(private), lifetime=timedelta(minutes=5)
    )
    connection = InstagramConnectionService(
        link_service=link,
        connect_base_url="https://connect.example.test",
        vault=vault,
        acquirer=FakeInstagramSessionAcquirer(challenge_required=challenge, reject_always=reject),
    )
    flow = CompanionInstagramConnectionFlow(connection, max_age_seconds=600, max_sessions=10)
    return repo, vault, flow


async def test_first_empty_step_prompts_credentials(tmp_path: Path) -> None:
    _repo, _vault, flow = _flow(tmp_path)
    result = await flow.step(owner_user_id=7, session_id="s1", input_value=None)
    assert result.stage is InstagramConnectStage.NEED_CREDENTIALS


async def test_password_connects_and_stores(tmp_path: Path) -> None:
    _repo, vault, flow = _flow(tmp_path)
    result = await flow.step(
        owner_user_id=7, session_id="s1", input_value={"username": "user", "password": "pw"}
    )
    assert result.stage is InstagramConnectStage.CONNECTED
    view = vault.get_view(7)
    assert view is not None and view.state is InstagramCredentialState.CONNECTED


async def test_challenge_then_2fa(tmp_path: Path) -> None:
    _repo, vault, flow = _flow(tmp_path, challenge=True)
    prompted = await flow.step(
        owner_user_id=7, session_id="s1", input_value={"username": "user", "password": "pw"}
    )
    assert prompted.stage is InstagramConnectStage.NEED_2FA
    done = await flow.step(owner_user_id=7, session_id="s1", input_value={"code": "123456"})
    assert done.stage is InstagramConnectStage.CONNECTED
    view = vault.get_view(7)
    assert view is not None and view.state is InstagramCredentialState.CONNECTED


async def test_denied_stage(tmp_path: Path) -> None:
    _repo, _vault, flow = _flow(tmp_path, reject=True)
    result = await flow.step(
        owner_user_id=7, session_id="s1", input_value={"username": "user", "password": "pw"}
    )
    assert result.stage is InstagramConnectStage.DENIED


async def test_wrong_owner_cannot_store(tmp_path: Path) -> None:
    _repo, vault, flow = _flow(tmp_path)
    result = await flow.step(
        owner_user_id=7, session_id="s1", input_value={"username": "user", "password": "pw"}
    )
    assert result.stage is InstagramConnectStage.CONNECTED
    # A different owner id cannot see the stored session.
    assert vault.get_view(999) is None


async def test_empty_prompt_then_credentials_can_continue(tmp_path: Path) -> None:
    _repo, vault, flow = _flow(tmp_path)
    assert (
        await flow.step(owner_user_id=7, session_id="s", input_value=None)
    ).stage is InstagramConnectStage.NEED_CREDENTIALS
    connected = await flow.step(
        owner_user_id=7, session_id="s", input_value={"username": "user", "password": "pw"}
    )
    assert connected.stage is InstagramConnectStage.CONNECTED
    assert vault.get_view(7) is not None


async def test_pending_attempt_is_owner_bound(tmp_path: Path) -> None:
    _repo, vault, flow = _flow(tmp_path)
    await flow.step(owner_user_id=7, session_id="s", input_value=None)
    denied = await flow.step(
        owner_user_id=999, session_id="s", input_value={"username": "user", "password": "pw"}
    )
    assert denied.stage is InstagramConnectStage.DENIED
    assert vault.get_view(999) is None


async def test_connected_without_session_bytes_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo, vault, flow = _flow(tmp_path)
    monkeypatch.setattr(
        flow._connection,
        "acquire_login",
        lambda **kwargs: InstagramLoginResult(stage=InstagramConnectStage.CONNECTED),
    )
    result = await flow.step(
        owner_user_id=7, session_id="s", input_value={"username": "user", "password": "pw"}
    )
    assert result.stage is InstagramConnectStage.DENIED
    assert vault.get_view(7) is None


async def test_cancelled_provider_thread_cannot_store_a_late_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo, vault, flow = _flow(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    acquire = flow._connection.acquire_login

    def blocked(**kwargs: str | None) -> InstagramLoginResult:
        entered.set()
        assert release.wait(5)
        result = acquire(**kwargs)
        finished.set()
        return result

    monkeypatch.setattr(flow._connection, "acquire_login", blocked)
    task = asyncio.create_task(
        flow.step(
            owner_user_id=7, session_id="s", input_value={"username": "user", "password": "pw"}
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    assert vault.get_view(7) is None
