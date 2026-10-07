from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
    StoryDeliveryMode,
)
from telegram_media_bot.telegram.handlers import build_router
from telegram_media_bot.telegram.texts import (
    SELECTION_EXPIRED_TEXT,
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
        self.text: str | None = None
        self.caption: str | None = None
        self.media_group_id: str | None = None
        self.bot = None
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

    def record_request(self, *_args: object, **_kwargs: object) -> None:
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
        self._records: list[JobRecord] = []

    def _record(self, **kwargs: object) -> tuple[JobRecord, bool]:
        self.calls.append(kwargs)
        now = datetime.now(UTC)
        record = JobRecord(
            job_id=JobId(f"job-{len(self._records) + 1}"),
            kind=JobKind.DOWNLOAD,
            status=JobStatus.QUEUED,
            chat_id=cast(int, kwargs["chat_id"]),
            user_id=cast(int, kwargs["user_id"]),
            url=str(kwargs["url"]),
            mode=cast(DownloadMode, kwargs.get("mode")),
            idempotency_key=_fake_idem_key(kwargs),
            created_at=now,
            updated_at=now,
            container=cast(OutputContainer, kwargs.get("container")),
            container_policy=ContainerPolicy.NATIVE_ONLY,
            selected_format_ids=tuple(
                cast(tuple[str, ...], kwargs.get("selected_format_ids") or ())
            ),
            story_delivery_mode=cast(StoryDeliveryMode, kwargs.get("story_delivery_mode")),
        )
        return record, True

    def create_download(self, **kwargs: object) -> tuple[JobRecord, bool]:
        key = _fake_idem_key(kwargs)
        for existing in self._records:
            if existing.idempotency_key == key:
                return existing, False
        record, _created = self._record(**kwargs)
        self._records.append(record)
        return record, True

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
        self.inspections: list[dict[str, object]] = []

    async def enqueue_download(self, **kwargs: object) -> JobId:
        self.downloads.append(kwargs)
        return cast(JobId, kwargs["job_id"])

    async def enqueue_highlight_tray(self, **kwargs: object) -> JobId:
        self.trays.append(kwargs)
        return cast(JobId, kwargs["job_id"])

    async def enqueue_inspection(self, **kwargs: object) -> JobId:
        self.inspections.append(kwargs)
        return cast(JobId, kwargs["job_id"])

    async def queue_depth(self) -> int:
        return 0


class FakeValidator:
    def __init__(self, **_kwargs: object) -> None:
        return None

    def validate(self, url: str) -> str:
        return url


def _fake_idem_key(kwargs: dict[str, object]) -> str:
    mode = kwargs.get("mode")
    mode_s = mode.value if isinstance(mode, DownloadMode) else "inspect"
    parts = ["download", str(kwargs["user_id"]), str(kwargs["url"]), mode_s]
    mode_value = kwargs.get("story_delivery_mode")
    if isinstance(mode_value, StoryDeliveryMode):
        parts.append(mode_value.value)
    return "|".join(parts)


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


def test_story_all_asks_for_delivery_mode_before_creating_job(settings: Settings) -> None:
    """s2:...:all now shows the delivery-mode prompt instead of creating a job."""
    router, jobs, queue, repository = _router(settings)
    token = "tok_story_all"
    repository.selections[token] = _story_selection(
        token, "https://www.instagram.com/stories/exampleuser/3964254748584813861/"
    )
    callback = FakeCallback(20, f"s2:{token}:all")

    import asyncio

    asyncio.run(_handler(router, "choose_story_action")(callback))

    # No job should be created yet; the user must first pick a delivery mode.
    assert jobs.calls == []
    assert queue.downloads == []


def test_story_all_normal_mode_creates_all_stories_job(settings: Settings) -> None:
    router, jobs, queue, repository = _router(settings)
    token = "tok_story_normal"
    repository.selections[token] = _story_selection(
        token, "https://www.instagram.com/stories/exampleuser/3964254748584813861/"
    )
    callback = FakeCallback(20, f"s3:{token}:{StoryDeliveryMode.NORMAL.value}")

    import asyncio

    asyncio.run(_handler(router, "choose_all_stories_delivery_mode")(callback))

    assert len(jobs.calls) == 1
    assert jobs.calls[0]["url"] == "https://www.instagram.com/stories/exampleuser/"
    assert jobs.calls[0]["mode"] is DownloadMode.INSTAGRAM_ALL_STORIES
    assert cast(StoryDeliveryMode, jobs.calls[0]["story_delivery_mode"]).value == "normal"
    assert queue.downloads[0]["mode"] is DownloadMode.INSTAGRAM_ALL_STORIES


def test_story_all_file_mode_creates_all_stories_job(settings: Settings) -> None:
    router, jobs, queue, repository = _router(settings)
    token = "tok_story_file"
    repository.selections[token] = _story_selection(
        token, "https://www.instagram.com/stories/exampleuser/3964254748584813861/"
    )

    import asyncio

    first = FakeCallback(20, f"s2:{token}:all")
    asyncio.run(_handler(router, "choose_story_action")(first))

    callback = FakeCallback(20, f"s3:{token}:file")
    asyncio.run(_handler(router, "choose_all_stories_delivery_mode")(callback))

    assert len(jobs.calls) == 1
    assert jobs.calls[0]["url"] == "https://www.instagram.com/stories/exampleuser/"
    assert jobs.calls[0]["mode"] is DownloadMode.INSTAGRAM_ALL_STORIES
    assert cast(StoryDeliveryMode, jobs.calls[0]["story_delivery_mode"]).value == "file"
    assert cast(StoryDeliveryMode, queue.downloads[0]["story_delivery_mode"]).value == "file"


def test_story_all_delivery_mode_callback_is_idempotent(settings: Settings) -> None:
    """Tapping the s3 mode button twice must not create two bulk jobs."""
    router, jobs, queue, repository = _router(settings)
    token = "tok_story_dup"
    repository.selections[token] = _story_selection(
        token, "https://www.instagram.com/stories/exampleuser/3964254748584813861/"
    )

    import asyncio

    callback = FakeCallback(20, f"s3:{token}:file")
    asyncio.run(_handler(router, "choose_all_stories_delivery_mode")(callback))
    same = FakeCallback(20, f"s3:{token}:file")
    asyncio.run(_handler(router, "choose_all_stories_delivery_mode")(same))

    assert len(jobs.calls) == 1
    assert len(queue.downloads) == 1


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
    router, jobs, queue, repository = _router(settings)
    from telegram_media_bot.telegram.ui import instagram_image_delivery_keyboard

    selection = _profile_selection("profile_token")
    repository.selections["profile_token"] = selection
    keyboard = instagram_image_delivery_keyboard(selection, highlights_username="exampleuser")
    open_callback = next(
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("h2:")
    )
    callback = FakeCallback(20, open_callback)

    import asyncio

    asyncio.run(_handler(router, "highlight_tray_navigation")(callback))

    assert len(jobs.calls) == 1
    assert jobs.calls[0]["username"] == "exampleuser"
    assert jobs.calls[0]["url"] == "https://www.instagram.com/exampleuser/highlights/"
    assert queue.trays[0]["username"] == "exampleuser"


def test_highlight_open_rejects_forged_username(settings: Settings) -> None:
    router, jobs, _queue, _repository = _router(settings)
    callback = FakeCallback(20, "h2:open:exampleuser")

    import asyncio

    asyncio.run(_handler(router, "highlight_tray_navigation")(callback))

    assert jobs.calls == []
    assert callback.alerts == [SELECTION_INVALID_TEXT]


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


def _profile_selection(token: str, owner: int = 20) -> SelectionRecord:
    selection = _story_selection(token, "https://www.instagram.com/exampleuser/avatar/", owner)
    return replace(
        selection,
        media=replace(
            selection.media,
            kind=MediaKind.IMAGE,
            assets=(replace(selection.media.assets[0], kind=MediaKind.IMAGE, extension="jpg"),),
        ),
    )


@pytest.mark.parametrize("owner,expired", [(99, False), (20, True)])
def test_highlight_open_enforces_source_owner_and_expiry(
    settings: Settings, owner: int, expired: bool
) -> None:
    router, jobs, queue, repository = _router(settings)
    selection = _profile_selection("profile_token", owner)
    if expired:
        selection = replace(selection, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    repository.selections["profile_token"] = selection
    callback = FakeCallback(20, "h2:profile_token:open")

    import asyncio

    asyncio.run(_handler(router, "highlight_tray_navigation")(callback))

    assert jobs.calls == []
    assert queue.trays == []
    assert callback.alerts == [SELECTION_EXPIRED_TEXT if expired else SELECTION_INVALID_TEXT]


def test_highlight_open_rejects_unoffered_source(settings: Settings) -> None:
    router, jobs, queue, repository = _router(settings)
    repository.selections["story_token"] = _story_selection(
        "story_token", "https://www.instagram.com/stories/exampleuser/123/"
    )
    callback = FakeCallback(20, "h2:story_token:open")

    import asyncio

    asyncio.run(_handler(router, "highlight_tray_navigation")(callback))

    assert jobs.calls == []
    assert queue.trays == []
    assert callback.alerts == [SELECTION_INVALID_TEXT]


def test_highlight_close_accepts_emitted_callback(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers_module, "Message", FakeMessage)
    router, jobs, queue, repository = _router(settings)
    repository.trays["tray_token"] = _tray("tray_token")
    message = FakeMessage(20)
    callback = FakeCallback(20, "h2:tray_token:close", message)

    import asyncio

    asyncio.run(_handler(router, "highlight_tray_navigation")(callback))

    assert len(message.edits) == 1
    assert callback.alerts == []
    assert jobs.calls == []
    assert queue.downloads == []


@pytest.mark.parametrize("action", ["pick:111", "page:2", "close"])
def test_highlight_tray_expired_actions_fail_closed(settings: Settings, action: str) -> None:
    router, jobs, queue, repository = _router(settings)
    repository.trays["tray_token"] = replace(
        _tray("tray_token"), expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    callback = FakeCallback(20, f"h2:tray_token:{action}")

    import asyncio

    asyncio.run(_handler(router, "highlight_tray_navigation")(callback))

    assert callback.alerts == [SELECTION_EXPIRED_TEXT]
    assert jobs.calls == []
    assert queue.downloads == []


def test_highlight_browser_pages_preserve_source_choices() -> None:
    from telegram_media_bot.telegram.ui import highlight_tray_keyboard

    tray = replace(
        _tray("tray_token"),
        highlights=tuple(HighlightItem(str(100 - index), str(index), index) for index in range(7)),
    )
    pages = [highlight_tray_keyboard(tray, page) for page in (1, 2)]
    choices = [
        button.callback_data
        for page in pages
        for row in page.inline_keyboard
        for button in row
        if button.callback_data and ":pick:" in button.callback_data
    ]

    assert choices == [f"h2:tray_token:pick:{100 - index}" for index in range(7)]
    assert all(len(choice.encode()) <= 64 for choice in choices)


@pytest.mark.parametrize(
    "url,expected_mode",
    [
        (
            "https://www.instagram.com/stories/highlights/222/?igsh=tracking",
            DownloadMode.INSTAGRAM_HIGHLIGHT,
        ),
        ("https://www.instagram.com/exampleuser/highlights/?igsh=tracking", None),
    ],
)
def test_submitted_highlight_urls_dispatch_only_the_requested_collection(
    settings: Settings, url: str, expected_mode: DownloadMode | None
) -> None:
    router, jobs, queue, _repository = _router(settings)
    message = FakeMessage(20)
    message.text = url

    import asyncio

    asyncio.run(_handler(router, "enqueue_url")(message))

    assert len(jobs.calls) == 1
    assert jobs.calls[0]["url"] == url.split("?")[0]
    if expected_mode is None:
        assert jobs.calls[0]["username"] == "exampleuser"
        assert len(queue.trays) == 1 and queue.downloads == []
    else:
        assert jobs.calls[0]["mode"] is expected_mode
        assert len(queue.downloads) == 1 and queue.trays == []
        assert queue.downloads[0]["mode"] is expected_mode


@pytest.mark.parametrize("entrypoint", ["url", "pick"])
def test_highlight_uncertain_delivery_is_never_reenqueued(
    settings: Settings, entrypoint: str
) -> None:
    router, jobs, queue, repository = _router(settings)
    record, _created = jobs.create_download(
        chat_id=10,
        user_id=20,
        url="https://www.instagram.com/stories/highlights/222/",
        mode=DownloadMode.INSTAGRAM_HIGHLIGHT,
    )
    jobs._records[0] = replace(record, status=JobStatus.DELIVERY_UNCERTAIN)
    jobs.calls.clear()

    import asyncio

    if entrypoint == "url":
        message = FakeMessage(20)
        message.text = record.url
        asyncio.run(_handler(router, "enqueue_url")(message))
        assert len(message.answers) == 1
    else:
        repository.trays["tray_token"] = _tray("tray_token")
        callback = FakeCallback(20, "h2:tray_token:pick:222")
        asyncio.run(_handler(router, "highlight_tray_navigation")(callback))
        assert len(callback.alerts) == 1

    assert queue.downloads == []
    assert jobs.calls == []


def test_submitted_profile_keeps_browser_intent_without_crawling_posts(
    settings: Settings, tmp_path: Path
) -> None:
    from telegram_media_bot.application.services.job_service import JobService
    from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository

    repository = SqliteJobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    queue = FakeQueue()
    router = build_router(
        settings=settings,
        queue=queue,  # type: ignore[arg-type]
        repository=repository,
        access_policy=FakeAccessPolicy(),  # type: ignore[arg-type]
        jobs=JobService(repository),
        users=FakeUsers(),  # type: ignore[arg-type]
    )
    message = FakeMessage(20)
    message.text = "https://www.instagram.com/exampleuser/?igsh=tracking"

    import asyncio

    asyncio.run(_handler(router, "enqueue_url")(message))

    assert len(queue.inspections) == 1
    record = repository.get_job(cast(JobId, queue.inspections[0]["job_id"]))
    assert record is not None
    assert record.url == "https://www.instagram.com/exampleuser/avatar/"
    assert record.url_classification == "profile"
    assert queue.trays == [] and queue.downloads == []
