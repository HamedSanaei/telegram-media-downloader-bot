from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

import telegram_media_bot.telegram.handlers as handlers_module
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    HighlightItem,
    HighlightTrayRecord,
    JobId,
    JobKind,
    JobRecord,
    JobStatus,
    MediaAsset,
    MediaFormatOption,
    MediaInfo,
    MediaKind,
    OutputContainer,
    SelectionRecord,
    SelectionToken,
)
from telegram_media_bot.telegram.handlers import build_router
from telegram_media_bot.telegram.texts import (
    SELECTION_INVALID_TEXT,
)


class FakeMessage:
    def __init__(self, user_id: int) -> None:
        self.from_user = SimpleNamespace(
            id=user_id,
            username="user",
            first_name="User",
            last_name=None,
            language_code="fa",
            is_premium=False,
        )
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.message_id = 500
        self.answers: list[tuple[str, object | None]] = []
        self.edits: list[str] = []

    async def answer(self, text: str, reply_markup: object | None = None) -> object:
        self.answers.append((text, reply_markup))
        return SimpleNamespace(message_id=499 + len(self.answers))

    async def edit_text(self, text: str, reply_markup: object | None = None) -> object:
        self.edits.append(text)
        return SimpleNamespace(message_id=self.message_id)


class FakeCallback:
    def __init__(self, user_id: int, data: str, message: FakeMessage | None = None) -> None:
        self.from_user = SimpleNamespace(
            id=user_id,
            username="user",
            first_name="User",
            last_name=None,
            language_code="fa",
            is_premium=False,
        )
        self.data = data
        self.message = message or FakeMessage(user_id)
        self.alerts: list[str] = []
        self.answers: list[str] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        if show_alert:
            self.alerts.append(text or "")
        self.answers.append(text or "")


class FakeState:
    async def clear(self) -> None:
        return None


class FakeAccessPolicy:
    async def authorize_request(self, _user_id: int, **_kwargs: object) -> None:
        return None


class FakeUsers:
    def upsert_user(self, *_args: object, **_kwargs: object) -> None:
        return None


class FakeRepository:
    def __init__(self) -> None:
        self.selections: dict[str, SelectionRecord] = {}
        self.trays: dict[str, HighlightTrayRecord] = {}
        self.status_messages: list[tuple[JobId, int]] = []

    def get_selection(self, token: SelectionToken, owner_user_id: int) -> SelectionRecord:
        selection = self.selections.get(token)
        if selection is None:
            from telegram_media_bot.domain.errors import SelectionExpiredError

            raise SelectionExpiredError("missing")
        if selection.owner_user_id != owner_user_id:
            from telegram_media_bot.domain.errors import SelectionOwnershipError

            raise SelectionOwnershipError("owner")
        if selection.expired:
            from telegram_media_bot.domain.errors import SelectionExpiredError

            raise SelectionExpiredError("expired")
        return selection

    def get_highlight_tray(self, token: SelectionToken, owner_user_id: int) -> HighlightTrayRecord:
        tray = self.trays.get(token)
        if tray is None:
            from telegram_media_bot.domain.errors import SelectionExpiredError

            raise SelectionExpiredError("missing")
        if tray.owner_user_id != owner_user_id:
            from telegram_media_bot.domain.errors import SelectionOwnershipError

            raise SelectionOwnershipError("owner")
        if tray.expired:
            from telegram_media_bot.domain.errors import SelectionExpiredError

            raise SelectionExpiredError("expired")
        return tray

    def set_status_message(self, job_id: JobId, message_id: int) -> None:
        self.status_messages.append((job_id, message_id))

    def transition(self, *_args: object, **_kwargs: object) -> None:
        return None

    def record_download_outcome(self, *_args: object, **_kwargs: object) -> None:
        return None


class FakeJobs:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def _record(self, **kwargs: object) -> tuple[JobRecord, bool]:
        self.calls.append(kwargs)
        now = datetime.now(UTC)
        return (
            JobRecord(
                job_id=JobId(f"job-{len(self.calls)}"),
                kind=JobKind.DOWNLOAD,
                status=JobStatus.QUEUED,
                chat_id=cast(int, kwargs["chat_id"]),
                user_id=cast(int, kwargs["user_id"]),
                url=str(kwargs["url"]),
                mode=cast(DownloadMode, kwargs.get("mode")),
                idempotency_key="key",
                created_at=now,
                updated_at=now,
                container=cast(OutputContainer, kwargs.get("container")),
                container_policy=ContainerPolicy.NATIVE_ONLY,
                selected_format_ids=tuple(
                    cast(tuple[str, ...], kwargs.get("selected_format_ids") or ())
                ),
            ),
            True,
        )

    def create_download(self, **kwargs: object) -> tuple[JobRecord, bool]:
        return self._record(**kwargs)

    def create_highlight_tray(self, **kwargs: object) -> tuple[JobRecord, bool]:
        record, created = self._record(**kwargs)
        record = JobRecord(
            job_id=record.job_id,
            kind=JobKind.HIGHLIGHT_TRAY,
            status=JobStatus.QUEUED,
            chat_id=record.chat_id,
            user_id=record.user_id,
            url=record.url,
            mode=None,
            idempotency_key="key",
            created_at=record.created_at,
            updated_at=record.updated_at,
            selected_format_ids=tuple(
                cast(tuple[str, ...], kwargs.get("selected_format_ids") or ())
            ),
        )
        return record, created


class FakeQueue:
    def __init__(self) -> None:
        self.downloads: list[dict[str, object]] = []
        self.trays: list[dict[str, object]] = []

    async def enqueue_download(self, **kwargs: object) -> JobId:
        self.downloads.append(kwargs)
        return cast(JobId, kwargs["job_id"])

    async def enqueue_highlight_tray(self, **kwargs: object) -> JobId:
        self.trays.append(kwargs)
        return cast(JobId, kwargs["job_id"])


class FakeValidator:
    def __init__(self, **_kwargs: object) -> None:
        return None

    def validate(self, url: str) -> str:
        return url


def _story_selection(token: str, url: str, owner: int = 20) -> SelectionRecord:
    asset = MediaAsset(
        1, "a1", MediaKind.VIDEO, "mp4", "video/mp4", "3964254748584813861", "instagram"
    )
    info = MediaInfo(
        media_id="3964254748584813861",
        title="Story",
        source="instagram",
        kind=MediaKind.VIDEO,
        webpage_url=url,
        item_count=1,
        format_options=(
            MediaFormatOption(
                DownloadMode.VIDEO_ORIGINAL,
                container_policy=ContainerPolicy.NATIVE_ONLY,
                selected_format_ids=("a1",),
            ),
        ),
        assets=(asset,),
    )
    now = datetime.now(UTC)
    return SelectionRecord(
        token=SelectionToken(token),
        owner_user_id=owner,
        chat_id=10,
        media=info,
        allowed_modes=(DownloadMode.VIDEO_ORIGINAL,),
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )


def _tray(token: str, owner: int = 20) -> HighlightTrayRecord:
    now = datetime.now(UTC)
    return HighlightTrayRecord(
        token=SelectionToken(token),
        owner_user_id=owner,
        chat_id=10,
        username="exampleuser",
        highlights=(HighlightItem("111", "safar", 2), HighlightItem("222", "zendegi", 1)),
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )


def _router(settings: Settings) -> tuple[Any, FakeJobs, FakeQueue, FakeRepository]:
    jobs = FakeJobs()
    queue = FakeQueue()
    repository = FakeRepository()
    router = build_router(
        settings=settings,
        queue=queue,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        access_policy=FakeAccessPolicy(),  # type: ignore[arg-type]
        jobs=jobs,  # type: ignore[arg-type]
        users=FakeUsers(),  # type: ignore[arg-type]
    )
    return router, jobs, queue, repository


def _handler(router: object, name: str) -> Any:
    for current in (router, *router.sub_routers):  # type: ignore[attr-defined]
        for observer in current.observers.values():
            for item in observer.handlers:
                if item.callback.__name__ == name:
                    return item.callback
    raise AssertionError(f"handler {name} not found")


@pytest.fixture(autouse=True)
def _patch_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers_module, "PublicUrlValidator", FakeValidator)


def test_story_single_keeps_exact_media_id(settings: Settings) -> None:
    router, jobs, queue, repository = _router(settings)
    token = "tok_story_single"
    repository.selections[token] = _story_selection(
        token, "https://www.instagram.com/stories/exampleuser/3964254748584813861/"
    )
    callback = FakeCallback(20, f"s2:{token}:single")

    import asyncio

    asyncio.run(_handler(router, "choose_story_action")(callback))

    assert len(jobs.calls) == 1
    assert jobs.calls[0]["url"] == (
        "https://www.instagram.com/stories/exampleuser/3964254748584813861/"
    )
    assert jobs.calls[0]["mode"] is DownloadMode.VIDEO_ORIGINAL
    assert queue.downloads[0]["job_id"] == JobId("job-1")


def test_story_all_targets_stories_account_collection(settings: Settings) -> None:
    router, jobs, queue, repository = _router(settings)
    token = "tok_story_all"
    repository.selections[token] = _story_selection(
        token, "https://www.instagram.com/stories/exampleuser/3964254748584813861/"
    )
    callback = FakeCallback(20, f"s2:{token}:all")

    import asyncio

    asyncio.run(_handler(router, "choose_story_action")(callback))

    assert jobs.calls[0]["url"] == "https://www.instagram.com/stories/exampleuser/"
    assert jobs.calls[0]["mode"] is DownloadMode.INSTAGRAM_ALL_STORIES
    assert queue.downloads[0]["mode"] is DownloadMode.INSTAGRAM_ALL_STORIES


def test_story_callback_rejects_non_story_selection(settings: Settings) -> None:
    router, _jobs, _queue, repository = _router(settings)
    token = "tok_not_story"
    now = datetime.now(UTC)
    post = MediaInfo(
        media_id="IG1",
        title="Post",
        source="instagram",
        kind=MediaKind.PLAYLIST,
        webpage_url="https://www.instagram.com/p/IG1/",
        assets=(MediaAsset(1, "a1", MediaKind.IMAGE, "jpg", "image/jpeg", "IG1", "instagram"),),
    )
    repository.selections[token] = SelectionRecord(
        token=SelectionToken(token),
        owner_user_id=20,
        chat_id=10,
        media=post,
        allowed_modes=(DownloadMode.IMAGE_ORIGINAL,),
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    callback = FakeCallback(20, f"s2:{token}:all")

    import asyncio

    asyncio.run(_handler(router, "choose_story_action")(callback))

    assert callback.alerts == [SELECTION_INVALID_TEXT]


def test_highlight_open_creates_tray_job(settings: Settings) -> None:
    router, jobs, queue, _repository = _router(settings)
    callback = FakeCallback(20, "h2:open:exampleuser")

    import asyncio

    asyncio.run(_handler(router, "highlight_tray_navigation")(callback))

    assert len(jobs.calls) == 1
    assert jobs.calls[0]["username"] == "exampleuser"
    assert jobs.calls[0]["url"] == "https://www.instagram.com/exampleuser/highlights/"
    assert queue.trays[0]["username"] == "exampleuser"
    assert callback.answers[0] == "در حال دریافت فهرست هایلایت‌ها…"  # noqa: RUF001


def test_highlight_open_rejects_forged_username(settings: Settings) -> None:
    router, jobs, _queue, _repository = _router(settings)
    callback = FakeCallback(20, "h2:open:bad username!")

    import asyncio

    asyncio.run(_handler(router, "highlight_tray_navigation")(callback))

    assert jobs.calls == []
    assert callback.alerts == [SELECTION_INVALID_TEXT]


def test_highlight_page_navigation(settings: Settings) -> None:
    # Pagination is a pure UI concern; the handler edit path is guarded by aiogram Message
    # typing, so the renderer and keyboard are asserted directly.
    from telegram_media_bot.telegram.ui import highlight_tray_keyboard, render_highlight_tray

    tray = _tray("tok_tray")
    text = render_highlight_tray(tray, page=1)
    assert text.startswith("⭐ هایلایت")
    assert "safar" in text
    keyboard = highlight_tray_keyboard(tray, page=1)
    callbacks = [
        cast(str, button.callback_data) for row in keyboard.inline_keyboard for button in row
    ]
    assert any(callback.startswith("h2:tok_tray:page:") for callback in callbacks)
    assert any(callback == "h2:tok_tray:close" for callback in callbacks)


def test_highlight_pick_enqueues_selected_only(settings: Settings) -> None:
    router, jobs, queue, repository = _router(settings)
    token = "tok_tray"
    repository.trays[token] = _tray(token)
    callback = FakeCallback(20, f"h2:{token}:pick:222")

    import asyncio

    asyncio.run(_handler(router, "highlight_tray_navigation")(callback))

    assert jobs.calls[0]["url"] == "https://www.instagram.com/stories/highlights/222/"
    assert jobs.calls[0]["mode"] is DownloadMode.INSTAGRAM_HIGHLIGHT
    assert queue.downloads[0]["mode"] is DownloadMode.INSTAGRAM_HIGHLIGHT


def test_highlight_pick_rejects_unoffered_highlight(settings: Settings) -> None:
    router, jobs, _queue, repository = _router(settings)
    token = "tok_tray"
    repository.trays[token] = _tray(token)
    callback = FakeCallback(20, f"h2:{token}:pick:999999")

    import asyncio

    asyncio.run(_handler(router, "highlight_tray_navigation")(callback))

    assert jobs.calls == []
    assert callback.alerts == [SELECTION_INVALID_TEXT]


def test_highlight_ownership_is_enforced(settings: Settings) -> None:
    router, jobs, _queue, repository = _router(settings)
    token = "tok_tray"
    repository.trays[token] = _tray(token, owner=99)
    callback = FakeCallback(20, f"h2:{token}:pick:111")

    import asyncio

    asyncio.run(_handler(router, "highlight_tray_navigation")(callback))

    assert jobs.calls == []
    assert callback.alerts == [SELECTION_INVALID_TEXT]
