from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import telegram_media_bot.telegram.handlers as handlers_module
from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.models import (
    DownloadMode,
    HighlightItem,
    HighlightTrayRecord,
    JobStatus,
    MediaInfo,
    MediaKind,
    SelectionToken,
    StoryDeliveryMode,
)
from telegram_media_bot.infrastructure.observability.metrics import MetricsRegistry
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.telegram.handlers import build_router
from telegram_media_bot.workers.jobs import process_inspection_job


class PromptMessage:
    def __init__(self) -> None:
        self.message_id = 30
        self.chat = SimpleNamespace(id=10, type="private")
        self.markup: Any = None

    async def edit_text(self, _text: str, reply_markup: object | None = None) -> None:
        self.markup = reply_markup


class Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(
            id=20,
            username="owner",
            first_name="Owner",
            last_name=None,
            language_code="fa",
            is_premium=False,
        )
        self.message = PromptMessage()

    async def answer(self, *_args: object, **_kwargs: object) -> None:
        pass


class AccessPolicy:
    async def authorize_request(self, *_args: object, **_kwargs: object) -> None:
        pass


class Queue:
    def __init__(self) -> None:
        self.downloads: list[dict[str, Any]] = []

    async def enqueue_download(self, **kwargs: Any) -> object:
        self.downloads.append(kwargs)
        return kwargs["job_id"]

    async def queue_depth(self) -> int:
        return 0


class Inspection:
    def inspect(self, url: str, **_kwargs: object) -> MediaInfo:
        # Even a video-only Highlight without assets must not enter auto-download.
        return MediaInfo("222", "Highlight", "instagram", MediaKind.VIDEO, url)


class Bot:
    def __init__(self) -> None:
        self.markup: Any = None

    async def edit_message_text(self, **kwargs: Any) -> object:
        self.markup = kwargs["reply_markup"]
        return SimpleNamespace(message_id=kwargs["message_id"])


@pytest.mark.parametrize("entrypoint", ["direct", "browser"])
@pytest.mark.parametrize("delivery_mode", list(StoryDeliveryMode))
async def test_highlight_choice_survives_restart_before_creating_exact_queue_payload(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    delivery_mode: StoryDeliveryMode,
) -> None:
    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
    configured = Settings.model_validate(raw)
    configured.create_runtime_directories()
    path = configured.database_path()
    repository = SqliteJobRepository(path)
    repository.initialize()
    jobs = JobService(repository)
    url = "https://www.instagram.com/stories/highlights/222/"
    queue = Queue()
    bot = Bot()
    if entrypoint == "direct":
        parent, _ = jobs.create_inspection(chat_id=10, user_id=20, url=url)
        repository.set_status_message(parent.job_id, 30)
        await process_inspection_job(
            {
                "settings": configured,
                "repository": repository,
                "download_service": Inspection(),
                "bot": bot,
                "metrics": MetricsRegistry(),
                "job_id": str(parent.job_id),
                "queue": queue,
            },
            chat_id=10,
            user_id=20,
            url=url,
        )
        assert queue.downloads == []
    else:
        parent, _ = jobs.create_highlight_tray(
            chat_id=10,
            user_id=20,
            url="https://www.instagram.com/owner/highlights/",
            username="owner",
        )
        repository.transition(parent.job_id, JobStatus.SUCCEEDED)
        now = datetime.now(UTC)
        repository.save_highlight_tray(
            HighlightTrayRecord(
                SelectionToken("tray"),
                20,
                10,
                "owner",
                (HighlightItem("222", "Highlight", 2),),
                now,
                now + timedelta(minutes=10),
                parent.job_id,
            )
        )
    restarted = SqliteJobRepository(path)
    users = SimpleNamespace(
        upsert_user=lambda *_args, **_kwargs: None,
        get_username=lambda _owner: None,
        record_request=lambda *_args: None,
    )
    router = build_router(
        settings=configured,
        queue=cast(Any, queue),
        repository=restarted,
        jobs=JobService(restarted),
        access_policy=cast(Any, AccessPolicy()),
        users=cast(Any, users),
    )
    callbacks = {item.callback.__name__: item.callback for item in router.callback_query.handlers}
    monkeypatch.setattr(handlers_module, "Message", PromptMessage)
    if entrypoint == "browser":
        pick = Callback("h2:tray:pick:222")
        await callbacks["highlight_tray_navigation"](pick)
        assert queue.downloads == []
        markup = pick.message.markup
    else:
        markup = bot.markup
    data = next(
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data.endswith(f":{delivery_mode.value}")
    )
    choice = Callback(data)
    await callbacks["choose_all_stories_delivery_mode"](choice)
    await callbacks["choose_all_stories_delivery_mode"](choice)
    assert len(queue.downloads) == 1
    payload = queue.downloads[0]
    assert payload["url"] == url
    assert payload["mode"] is DownloadMode.INSTAGRAM_HIGHLIGHT
    assert payload["story_delivery_mode"] is delivery_mode
    record = SqliteJobRepository(path).get_job(payload["job_id"])
    assert record is not None and record.story_delivery_mode is delivery_mode
    token = SelectionToken(data.split(":")[1])
    assert restarted.get_selection(token, 20).source_job_id == parent.job_id
