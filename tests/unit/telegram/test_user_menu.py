from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar, cast
from unittest.mock import Mock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.methods.base import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, ReplyKeyboardMarkup, Update, User

import telegram_media_bot.telegram.handlers as handlers_module
from telegram_media_bot.application.services.access_policy import AccessPolicyService
from telegram_media_bot.application.services.credential_vault import CredentialVault
from telegram_media_bot.application.services.entitlements import EntitlementService
from telegram_media_bot.application.services.handoff import HandoffLinkService
from telegram_media_bot.application.services.instagram_connection import InstagramConnectionService
from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.application.services.private_media_policy import authorize_private_request
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.credential_resolution import ContentAccessScope, CredentialPolicy
from telegram_media_bot.domain.instagram_credentials import InstagramCredentialState
from telegram_media_bot.domain.models import (
    DownloadMode,
    JobId,
    JobStatus,
    MediaAsset,
    MediaFormatOption,
    MediaInfo,
    MediaKind,
    SelectionRecord,
    SelectionToken,
)
from telegram_media_bot.domain.subscriptions import (
    Capability,
    EntitlementGrant,
    GrantId,
    PlanId,
    SubscriptionPlan,
)
from telegram_media_bot.infrastructure.credentials.key_ring import CredentialCryptor, VaultKeyRing
from telegram_media_bot.infrastructure.instagram_login.fake import FakeInstagramSessionAcquirer
from telegram_media_bot.infrastructure.persistence.sqlite_instagram_credentials import (
    SqliteInstagramCredentialRepository,
)
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.infrastructure.persistence.sqlite_subscriptions import (
    SqliteSubscriptionRepository,
)
from telegram_media_bot.infrastructure.security.handoff import (
    Ed25519HandoffSigner,
    Ed25519HandoffVerifier,
)
from telegram_media_bot.telegram.admin_menu import (
    ADMIN_BACK_TO_MENU_BUTTON,
    ADMIN_DOWNLOAD_BUTTON,
    ADMIN_MANAGEMENT_BUTTONS,
    ADMIN_REFRESH_MENU_BUTTON,
    AdminCookieState,
    AdminDownloadState,
)
from telegram_media_bot.telegram.handlers import build_router
from telegram_media_bot.telegram.texts import (
    PRIVATE_CONNECT_REQUIRED_TEXT,
    PRIVATE_RECONNECT_REQUIRED_TEXT,
    PRIVATE_VIP_REQUIRED_TEXT,
    SELECTION_INVALID_TEXT,
)
from telegram_media_bot.telegram.user_menu import (
    INSTAGRAM_CONNECT_BUTTON,
    INSTAGRAM_DISCONNECT_BUTTON,
    INSTAGRAM_STATUS_BUTTON,
    PRIVATE_DOWNLOAD_BUTTON,
    PUBLIC_DOWNLOAD_BUTTON,
    USER_MENU_BUTTON,
    UserDownloadState,
    private_job_fields,
)

_T = TypeVar("_T")
NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
OWNER = 20
ADMIN = 99
PRIVATE_URL = "https://www.instagram.com/p/PrivateFixture/"


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []

    async def close(self) -> None:
        return None

    # The aiogram transport interface requires a timeout argument.
    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[_T],
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> _T:
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return cast(_T, True)
        if isinstance(method, SendMessage | EditMessageText):
            return cast(
                _T,
                Message(
                    message_id=1000 + len(self.calls),
                    date=NOW,
                    chat=Chat(id=int(method.chat_id or OWNER), type="private"),
                    from_user=User(id=bot.id, is_bot=True, first_name="Downloader"),
                    text=method.text,
                ),
            )
        raise AssertionError(f"Unexpected Telegram method: {type(method).__name__}")

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        raise AssertionError("Menu flow must not fetch files")
        yield b""  # pragma: no cover


class Queue:
    def __init__(self) -> None:
        self.jobs: list[JobId] = []

    async def queue_depth(self) -> int:
        return len(self.jobs)

    async def enqueue_inspection(self, *, job_id: JobId, **kwargs: object) -> JobId:
        self.jobs.append(job_id)
        return job_id

    async def enqueue_download(self, *, job_id: JobId, **kwargs: object) -> JobId:
        self.jobs.append(job_id)
        return job_id

    async def enqueue_highlight_tray(self, *, job_id: JobId, **kwargs: object) -> JobId:
        self.jobs.append(job_id)
        return job_id


class AllowRateLimiter:
    async def allow(self, user_id: int, limit: int) -> bool:
        return True

    async def close(self) -> None:
        return None


class Validator:
    def __init__(self, **kwargs: object) -> None:
        pass

    def validate(self, url: str) -> str:
        return url


@dataclass
class Runtime:
    settings: Settings
    bot: Bot
    dispatcher: Dispatcher
    session: RecordingSession
    repository: SqliteJobRepository
    subscriptions: SqliteSubscriptionRepository
    entitlements: EntitlementService
    connection: InstagramConnectionService
    vault: CredentialVault
    jobs: JobService
    queue: Queue
    signing_key: str
    sequence: int = 0

    async def send(self, text: str, owner: int = OWNER) -> None:
        self.sequence += 1
        await self.dispatcher.feed_update(
            self.bot,
            Update(
                update_id=self.sequence,
                message=Message(
                    message_id=self.sequence,
                    date=NOW,
                    chat=Chat(id=owner, type="private"),
                    from_user=User(id=owner, is_bot=False, first_name="User", is_premium=True),
                    text=text,
                ),
            ),
        )

    async def callback(self, data: str, owner: int = OWNER) -> None:
        self.sequence += 1
        await self.dispatcher.feed_update(
            self.bot,
            Update(
                update_id=self.sequence,
                callback_query=CallbackQuery(
                    id=str(self.sequence),
                    from_user=User(id=owner, is_bot=False, first_name="User"),
                    chat_instance="menu-test",
                    data=data,
                    message=Message(
                        message_id=800,
                        date=NOW,
                        chat=Chat(id=OWNER, type="private"),
                        text="Media selection",
                    ),
                ),
            ),
        )

    async def state(self, owner: int = OWNER) -> FSMContext:
        return self.dispatcher.fsm.get_context(bot=self.bot, chat_id=owner, user_id=owner)

    def grant(self, *, confirmed_at: datetime = NOW) -> None:
        self.entitlements.activate_grant(
            EntitlementGrant(
                grant_id=GrantId("owner-grant"),
                user_id=OWNER,
                plan_id=PlanId("vip"),
                duration_months=1,
                confirmed_at=confirmed_at,
                created_at=confirmed_at,
                source_type="test",
                source_reference="owner-grant",
            ),
            now=confirmed_at,
        )

    def last_text(self) -> str:
        return str(self.session.calls[-1].text)  # type: ignore[attr-defined]

    def last_buttons(self) -> set[str]:
        markup = self.session.calls[-1].reply_markup  # type: ignore[attr-defined]
        assert isinstance(markup, ReplyKeyboardMarkup)
        return {button.text for row in markup.keyboard for button in row}


@pytest.fixture
def runtime(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Runtime:
    raw = settings.model_dump()
    raw["telegram"]["admin_ids"] = [ADMIN]
    settings = Settings.model_validate(raw)
    monkeypatch.setattr(handlers_module, "PublicUrlValidator", Validator)
    clock = Mock(wraps=datetime)
    clock.now.return_value = NOW
    monkeypatch.setattr(handlers_module, "datetime", clock)
    repository = SqliteJobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    subscriptions = SqliteSubscriptionRepository(tmp_path / "jobs.sqlite3")
    subscriptions.initialize()
    subscriptions.save_plan(
        SubscriptionPlan(
            plan_id=PlanId("vip"),
            name="VIP",
            duration_months=1,
            price_minor=100_000,
            currency="IRT",
            enabled=True,
            capabilities=frozenset({Capability.INSTAGRAM_PRIVATE_MEDIA}),
        )
    )
    entitlements = EntitlementService(plans=subscriptions, subscriptions=subscriptions)
    credentials = SqliteInstagramCredentialRepository(tmp_path / "credentials.sqlite3")
    credentials.initialize()
    ring = VaultKeyRing.from_hex_material(active_key_id="test", active_key=(b"k" * 32).hex())
    vault = CredentialVault(credentials, CredentialCryptor(ring))
    signer, signing_key = Ed25519HandoffSigner.generate()
    connection = InstagramConnectionService(
        vault=vault,
        acquirer=FakeInstagramSessionAcquirer(),
        link_service=HandoffLinkService(signer, lifetime=timedelta(minutes=5)),
        connect_base_url="https://connect.example.test",
    )
    session = RecordingSession()
    bot = Bot(token="123456:TEST_TOKEN_FOR_UNIT_TESTS", session=session)
    dispatcher = Dispatcher()
    jobs = JobService(repository)
    queue = Queue()
    dispatcher.include_router(
        build_router(
            settings=settings,
            queue=queue,  # type: ignore[arg-type]
            repository=repository,
            access_policy=AccessPolicyService(
                settings=settings, repository=repository, rate_limiter=AllowRateLimiter()
            ),
            jobs=jobs,
            users=repository,
            connection=connection,
            subscription_store=subscriptions,
            entitlements=entitlements,
        )
    )
    return Runtime(
        settings,
        bot,
        dispatcher,
        session,
        repository,
        subscriptions,
        entitlements,
        connection,
        vault,
        jobs,
        queue,
        signing_key,
    )


async def test_current_role_replaces_stale_keyboard_and_fsm(runtime: Runtime) -> None:
    state = await runtime.state()
    await state.set_state(AdminDownloadState.awaiting_url)
    await runtime.send("/start")
    assert runtime.last_buttons().isdisjoint(ADMIN_MANAGEMENT_BUTTONS)
    assert PRIVATE_DOWNLOAD_BUTTON in runtime.last_buttons()
    assert await state.get_state() is None

    await runtime.send("/menu", ADMIN)
    assert runtime.last_buttons() & ADMIN_MANAGEMENT_BUTTONS
    object.__setattr__(runtime.settings.telegram, "admin_ids", ())
    await runtime.send("/menu", ADMIN)
    assert runtime.last_buttons().isdisjoint(ADMIN_MANAGEMENT_BUTTONS)
    await runtime.send(USER_MENU_BUTTON)
    assert runtime.last_buttons().isdisjoint(ADMIN_MANAGEMENT_BUTTONS)


async def test_forged_admin_action_is_denied_and_removes_stale_keyboard(runtime: Runtime) -> None:
    state = await runtime.state()
    await state.set_state(AdminDownloadState.awaiting_url)
    await runtime.send("/panel")
    assert runtime.last_buttons().isdisjoint(ADMIN_MANAGEMENT_BUTTONS)
    await runtime.send(next(iter(ADMIN_MANAGEMENT_BUTTONS)))
    assert runtime.last_buttons().isdisjoint(ADMIN_MANAGEMENT_BUTTONS)
    assert await state.get_state() is None
    assert runtime.queue.jobs == []


@pytest.mark.parametrize("navigation", [ADMIN_REFRESH_MENU_BUTTON, ADMIN_BACK_TO_MENU_BUTTON])
@pytest.mark.parametrize(
    "pending_state",
    [
        AdminDownloadState.awaiting_url,
        AdminCookieState.awaiting_upload,
        UserDownloadState.private_url,
    ],
)
@pytest.mark.parametrize(
    ("owner", "demote"),
    [(OWNER, False), (ADMIN, False), (ADMIN, True)],
    ids=["ordinary", "administrator", "demoted"],
)
async def test_shared_navigation_recovers_current_role_and_public_downloading(
    runtime: Runtime, navigation: str, pending_state: State, owner: int, demote: bool
) -> None:
    if demote:
        object.__setattr__(runtime.settings.telegram, "admin_ids", ())
    state = await runtime.state(owner)
    await state.set_state(pending_state)

    await runtime.send(navigation, owner)

    assert await state.get_state() is None
    is_admin = owner == ADMIN and not demote
    if is_admin:
        assert ADMIN_DOWNLOAD_BUTTON in runtime.last_buttons()
    else:
        assert runtime.last_buttons().isdisjoint(ADMIN_MANAGEMENT_BUTTONS)
        assert PUBLIC_DOWNLOAD_BUTTON in runtime.last_buttons()
        assert PRIVATE_DOWNLOAD_BUTTON in runtime.last_buttons()

    await runtime.send(ADMIN_DOWNLOAD_BUTTON if is_admin else PUBLIC_DOWNLOAD_BUTTON, owner)
    await runtime.send(PRIVATE_URL, owner)

    assert len(runtime.queue.jobs) == 1
    accepted = runtime.repository.get_job(runtime.queue.jobs[0])
    assert accepted is not None and accepted.user_id == owner
    assert accepted.entitlement_snapshot is None
    assert accepted.user_credential_generation is None
    assert await state.get_state() is None


@pytest.mark.parametrize("restriction", ["allowlist", "static_block", "dynamic_block"])
async def test_menu_recovery_does_not_bypass_general_download_restrictions(
    runtime: Runtime, restriction: str
) -> None:
    if restriction == "allowlist":
        object.__setattr__(runtime.settings.security, "allowed_user_ids", frozenset({ADMIN}))
    elif restriction == "static_block":
        object.__setattr__(runtime.settings.security, "blocked_user_ids", frozenset({OWNER}))
    else:
        runtime.repository.block_user(OWNER, ADMIN)

    await runtime.send(ADMIN_REFRESH_MENU_BUTTON)
    assert runtime.last_buttons().isdisjoint(ADMIN_MANAGEMENT_BUTTONS)
    await runtime.send(PUBLIC_DOWNLOAD_BUTTON)
    await runtime.send(PRIVATE_URL)

    assert runtime.queue.jobs == []
    assert runtime.repository.counts().queued == 0

    object.__setattr__(runtime.settings.security, "allowed_user_ids", frozenset())
    object.__setattr__(runtime.settings.security, "blocked_user_ids", frozenset())
    runtime.repository.unblock_user(OWNER)
    await runtime.send(PRIVATE_URL)

    assert len(runtime.queue.jobs) == 1
    accepted = runtime.repository.get_job(runtime.queue.jobs[0])
    assert accepted is not None and accepted.user_id == OWNER


async def test_free_connected_and_telegram_premium_do_not_authorize_private(
    runtime: Runtime,
) -> None:
    runtime.vault.store_session(OWNER, b"test-instagram-session")
    await runtime.send(PRIVATE_DOWNLOAD_BUTTON)
    assert runtime.last_text() == PRIVATE_VIP_REQUIRED_TEXT
    assert await (await runtime.state()).get_state() is None
    assert runtime.queue.jobs == []
    await runtime.send(PUBLIC_DOWNLOAD_BUTTON)
    await runtime.send("https://www.youtube.com/watch?v=abcdefghijk")
    public = runtime.repository.get_job(runtime.queue.jobs[-1])
    assert public is not None
    assert public.entitlement_snapshot is None
    assert public.user_credential_generation is None
    assert public.credential_policy is not CredentialPolicy.USER_ONLY


@pytest.mark.parametrize(
    ("credential_state", "expected"),
    [
        (None, PRIVATE_CONNECT_REQUIRED_TEXT),
        (InstagramCredentialState.EXPIRED, PRIVATE_RECONNECT_REQUIRED_TEXT),
        (InstagramCredentialState.CHALLENGE_REQUIRED, PRIVATE_RECONNECT_REQUIRED_TEXT),
        (InstagramCredentialState.DISCONNECTED, PRIVATE_CONNECT_REQUIRED_TEXT),
    ],
)
async def test_vip_requires_healthy_personal_session(
    runtime: Runtime, credential_state: InstagramCredentialState | None, expected: str
) -> None:
    runtime.grant()
    if credential_state is not None:
        runtime.vault.store_session(OWNER, b"test-instagram-session")
        if credential_state is InstagramCredentialState.EXPIRED:
            runtime.vault.set_state_category(OWNER, InstagramCredentialState.EXPIRED)
        elif credential_state is InstagramCredentialState.CHALLENGE_REQUIRED:
            runtime.vault.set_state_category(OWNER, InstagramCredentialState.CHALLENGE_REQUIRED)
        else:
            runtime.connection.disconnect(OWNER)
    await runtime.send(PRIVATE_DOWNLOAD_BUTTON)
    assert runtime.last_text() == expected
    assert runtime.queue.jobs == []
    assert await (await runtime.state()).get_state() is None


async def test_private_and_public_requests_keep_distinct_durable_authorizations(
    runtime: Runtime,
) -> None:
    runtime.grant()
    runtime.vault.store_session(OWNER, b"test-instagram-session")
    await runtime.send(PRIVATE_DOWNLOAD_BUTTON)
    await runtime.send(PRIVATE_URL)
    private = runtime.repository.get_job(runtime.queue.jobs[-1])
    assert private is not None
    assert private.credential_policy is CredentialPolicy.USER_ONLY
    assert private.content_access_scope is ContentAccessScope.USER_RESTRICTED
    assert private.user_credential_generation == 1
    assert private.entitlement_snapshot is not None
    assert private.entitlement_snapshot.owner_user_id == OWNER
    assert await (await runtime.state()).get_state() is None

    await runtime.send(PRIVATE_URL)
    public = runtime.repository.get_job(runtime.queue.jobs[-1])
    assert public is not None and public.job_id != private.job_id
    assert public.entitlement_snapshot is None
    assert public.user_credential_generation is None


async def test_subscription_is_rechecked_when_private_url_arrives(runtime: Runtime) -> None:
    runtime.grant()
    runtime.vault.store_session(OWNER, b"test-instagram-session")
    await runtime.send(PRIVATE_DOWNLOAD_BUTTON)
    runtime.entitlements.reverse_grant(
        GrantId("owner-grant"), reason="test", reversed_at=NOW, now=NOW
    )
    await runtime.send(PRIVATE_URL)
    assert runtime.last_text() == PRIVATE_VIP_REQUIRED_TEXT
    assert runtime.queue.jobs == []


async def test_instagram_account_actions_are_owner_bound_and_clear_private_mode(
    runtime: Runtime,
) -> None:
    runtime.vault.store_session(ADMIN, b"admin-session")
    state = await runtime.state()
    await state.set_state(UserDownloadState.private_url)
    await runtime.send(INSTAGRAM_CONNECT_BUTTON)
    link = runtime.last_text().split("https://", 1)[1]
    token = link.split("#handoff=", 1)[1].split()[0]
    result = Ed25519HandoffVerifier.from_private_encoded(runtime.signing_key).verify(
        token, now=datetime.now(UTC)
    )
    assert result.claim is not None and result.claim.owner_user_id == OWNER
    assert await state.get_state() is None
    runtime.vault.store_session(OWNER, b"owner-session")
    await runtime.send(INSTAGRAM_STATUS_BUTTON)
    assert runtime.vault.get_view(OWNER) is not None
    await runtime.send(INSTAGRAM_DISCONNECT_BUTTON)
    owner = runtime.vault.get_view(OWNER)
    admin = runtime.vault.get_view(ADMIN)
    assert owner is not None and owner.state is InstagramCredentialState.DISCONNECTED
    assert admin is not None and admin.state is InstagramCredentialState.CONNECTED


def _private_selection(runtime: Runtime) -> SelectionRecord:
    authorization = authorize_private_request(
        owner_user_id=OWNER,
        entitlements=runtime.entitlements,
        connection=runtime.connection,
        accepted_at=NOW,
    )
    parent, _ = runtime.jobs.create_inspection(
        chat_id=OWNER, user_id=OWNER, url=PRIVATE_URL, **private_job_fields(authorization)
    )
    runtime.repository.transition(parent.job_id, JobStatus.SUCCEEDED)
    selection = SelectionRecord(
        token=SelectionToken("private-choice"),
        owner_user_id=OWNER,
        chat_id=OWNER,
        media=MediaInfo(
            media_id="PrivateFixture",
            title="Image",
            source="instagram",
            kind=MediaKind.IMAGE,
            webpage_url=PRIVATE_URL,
            format_options=(MediaFormatOption(mode=DownloadMode.IMAGE_ORIGINAL),),
            assets=(
                MediaAsset(
                    index=1,
                    asset_id="image",
                    kind=MediaKind.IMAGE,
                    extension="jpg",
                    mime_type="image/jpeg",
                    source_post_id="PrivateFixture",
                    provider="instagram",
                ),
            ),
        ),
        allowed_modes=(DownloadMode.IMAGE_ORIGINAL,),
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        source_job_id=parent.job_id,
    )
    runtime.repository.save_selection(selection)
    return selection


@pytest.mark.parametrize("change", ["expire", "reconnect", "other-owner", "missing-parent"])
async def test_private_selection_reauthorizes_without_downgrading_to_public(
    runtime: Runtime, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.grant()
    runtime.vault.store_session(OWNER, b"test-instagram-session")
    selection = _private_selection(runtime)
    callback_owner = OWNER
    if change == "expire":
        clock = Mock(wraps=datetime)
        clock.now.return_value = NOW + timedelta(days=32)
        monkeypatch.setattr(handlers_module, "datetime", clock)
    elif change == "reconnect":
        runtime.vault.store_session(OWNER, b"new-session")
    elif change == "other-owner":
        callback_owner = ADMIN
    else:
        runtime.repository.save_selection(replace(selection, source_job_id=JobId("missing")))
    await runtime.callback("i2:private-choice:document", callback_owner)
    alert = next(
        call for call in reversed(runtime.session.calls) if isinstance(call, AnswerCallbackQuery)
    )
    expected = {
        "expire": PRIVATE_VIP_REQUIRED_TEXT,
        "reconnect": PRIVATE_RECONNECT_REQUIRED_TEXT,
        "other-owner": SELECTION_INVALID_TEXT,
        "missing-parent": SELECTION_INVALID_TEXT,
    }[change]
    assert alert.text == expected and alert.show_alert
    assert runtime.queue.jobs == []


async def test_authorized_private_selection_creates_only_user_bound_download(
    runtime: Runtime,
) -> None:
    runtime.grant()
    runtime.vault.store_session(OWNER, b"test-instagram-session")
    _private_selection(runtime)
    await runtime.callback("i2:private-choice:document")
    download = runtime.repository.get_job(runtime.queue.jobs[-1])
    assert download is not None
    assert download.credential_policy is CredentialPolicy.USER_ONLY
    assert download.user_credential_generation == 1
    assert download.entitlement_snapshot is not None
    assert download.entitlement_snapshot.owner_user_id == OWNER
