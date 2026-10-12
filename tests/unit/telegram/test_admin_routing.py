from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

import telegram_media_bot.telegram.handlers as handlers_module
from telegram_media_bot.application.services.logger_privacy import LOGGER_PRIVACY_DISCLOSURE_FA
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.models import (
    JobId,
    JobKind,
    JobRecord,
    JobStatus,
)
from telegram_media_bot.telegram.admin_menu import (
    build_admin_main_keyboard,
)
from telegram_media_bot.telegram.handlers import build_router
from telegram_media_bot.telegram.texts import (
    INSPECTION_QUEUED_TEXT,
)


class FakeResponse:
    def __init__(self, message_id: int = 500) -> None:
        self.message_id = message_id
        self.edits: list[str] = []

    async def edit_text(self, text: str) -> None:
        self.edits.append(text)


class FakeMessage:
    def __init__(
        self,
        user_id: int,
        text: str | None,
        *,
        caption: str | None = None,
        media_kind: str | None = None,
        media_group_id: str | None = None,
        message_id: int = 100,
    ) -> None:
        self.from_user = SimpleNamespace(
            id=user_id,
            username="user",
            first_name="User",
            last_name=None,
            language_code="fa",
            is_premium=False,
        )
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.text = text
        self.caption = caption
        self.message_id = message_id
        self.media_group_id = media_group_id
        self.date = datetime.now(UTC)
        for field in ("photo", "video", "document", "audio", "animation"):
            setattr(self, field, object() if field == media_kind else None)
        self.answers: list[tuple[str, object | None]] = []

    async def answer(self, text: str, reply_markup: object | None = None) -> FakeResponse:
        self.answers.append((text, reply_markup))
        return FakeResponse(499 + len(self.answers))


class FakeState:
    async def clear(self) -> None:
        return None


class FakeAccessPolicy:
    async def authorize_request(self, _user_id: int, **_kwargs: object) -> None:
        return None


class FakeUsers:
    def upsert_user(self, *_args: object, **_kwargs: object) -> None:
        return None

    def get_username(self, _user_id: int) -> str | None:
        return None

    def record_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class FakeRepository:
    def set_status_message(self, _job_id: JobId, _message_id: int) -> None:
        return None


class FakeJobs:
    def __init__(self, *, created: bool = True) -> None:
        self.calls: list[dict[str, object]] = []
        self.created = created

    def create_inspection(self, **kwargs: object) -> tuple[JobRecord, bool]:
        self.calls.append(kwargs)
        now = datetime.now(UTC)
        return (
            JobRecord(
                job_id=JobId("inspection-job"),
                kind=JobKind.INSPECTION,
                status=JobStatus.QUEUED,
                chat_id=cast(int, kwargs["chat_id"]),
                user_id=cast(int, kwargs["user_id"]),
                url=str(kwargs["url"]),
                mode=None,
                idempotency_key="key",
                created_at=now,
                updated_at=now,
                status_message_id=321 if not self.created else None,
            ),
            self.created,
        )


class FakeQueue:
    def __init__(self) -> None:
        self.inspections: list[dict[str, object]] = []

    async def queue_depth(self) -> int:
        return 0

    async def enqueue_inspection(self, **kwargs: object) -> JobId:
        self.inspections.append(kwargs)
        return JobId(str(kwargs["job_id"]))


class FakeValidator:
    def __init__(self, **_kwargs: object) -> None:
        return None

    def validate(self, url: str) -> str:
        return url


class FakeCallback:
    def __init__(self, user_id: int, data: str) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.message = None
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


@pytest.fixture
def role_settings(settings: Settings) -> Settings:
    raw = settings.model_dump()
    raw["telegram"]["admin_ids"] = [99]
    return Settings.model_validate(raw)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abcdefghijk",
        "https://www.instagram.com/reel/DbQqWqBDLXS/",
    ],
)
async def test_direct_admin_url_uses_editable_status_and_regular_inspection_pipeline(
    role_settings: Settings,
    url: str,
) -> None:
    router, jobs, queue = _router(role_settings)
    message = FakeMessage(99, url)

    await _handler(router, "enqueue_url")(message)

    assert len(jobs.calls) == 1
    assert jobs.calls[0]["user_id"] == 99
    assert len(queue.inspections) == 1
    assert message.answers[0] == (
        INSPECTION_QUEUED_TEXT.format(job_id="inspection-job"),
        None,
    )
    assert message.answers[-1][1] == build_admin_main_keyboard()


async def test_regular_user_status_remains_editable_without_admin_keyboard(
    role_settings: Settings,
) -> None:
    router, jobs, queue = _router(role_settings)
    message = FakeMessage(20, "https://www.youtube.com/watch?v=abcdefghijk")

    await _handler(router, "enqueue_url")(message)

    assert len(jobs.calls) == 1
    assert len(queue.inspections) == 1
    assert message.answers[0][1] is None
    assert all(markup != build_admin_main_keyboard() for _text, markup in message.answers)


@pytest.mark.parametrize("media_kind", [None, "photo", "video", "document", "audio", "animation"])
async def test_url_text_and_media_captions_remain_accepted_download_submissions(
    role_settings: Settings,
    media_kind: str | None,
) -> None:
    jobs = FakeJobs()
    queue = FakeQueue()
    router = build_router(
        settings=role_settings,
        queue=queue,  # type: ignore[arg-type]
        repository=FakeRepository(),  # type: ignore[arg-type]
        access_policy=FakeAccessPolicy(),  # type: ignore[arg-type]
        jobs=jobs,  # type: ignore[arg-type]
        users=FakeUsers(),  # type: ignore[arg-type]
    )
    message = FakeMessage(
        20,
        "https://example.com/media" if media_kind is None else None,
        caption="caption https://example.com/media" if media_kind is not None else None,
        media_kind=media_kind,
    )

    await _handler(router, "enqueue_url")(message, durable_update_id=77)

    assert len(queue.inspections) == 1


async def test_invalid_and_start_control_traffic_do_not_create_download_jobs(
    role_settings: Settings,
) -> None:
    jobs = FakeJobs()
    queue = FakeQueue()
    router = build_router(
        settings=role_settings,
        queue=queue,  # type: ignore[arg-type]
        repository=FakeRepository(),  # type: ignore[arg-type]
        access_policy=FakeAccessPolicy(),  # type: ignore[arg-type]
        jobs=jobs,  # type: ignore[arg-type]
        users=FakeUsers(),  # type: ignore[arg-type]
    )

    await _handler(router, "start")(FakeMessage(20, "/start"), FakeState())
    await _handler(router, "enqueue_url")(FakeMessage(20, "ordinary text"), durable_update_id=79)

    assert jobs.calls == []


async def test_acceptance_never_demands_user_privacy_acknowledgement(
    role_settings: Settings,
) -> None:
    jobs = FakeJobs()
    queue = FakeQueue()
    router = build_router(
        settings=role_settings,
        queue=queue,  # type: ignore[arg-type]
        repository=FakeRepository(),  # type: ignore[arg-type]
        access_policy=FakeAccessPolicy(),  # type: ignore[arg-type]
        jobs=jobs,  # type: ignore[arg-type]
        users=FakeUsers(),  # type: ignore[arg-type]
    )
    message = FakeMessage(20, "https://example.com/media")

    await _handler(router, "enqueue_url")(message, durable_update_id=80)

    assert len(jobs.calls) == 1
    assert all(LOGGER_PRIVACY_DISCLOSURE_FA not in text for text, _ in message.answers)


async def test_privacy_disclosure_command_is_non_blocking(role_settings: Settings) -> None:
    router = build_router(
        settings=role_settings,
        queue=FakeQueue(),  # type: ignore[arg-type]
        repository=FakeRepository(),  # type: ignore[arg-type]
        access_policy=FakeAccessPolicy(),  # type: ignore[arg-type]
        jobs=FakeJobs(),  # type: ignore[arg-type]
        users=FakeUsers(),  # type: ignore[arg-type]
    )
    message = FakeMessage(20, "/privacy")

    await _handler(router, "privacy_disclosure")(message)

    assert message.answers[-1][0] == LOGGER_PRIVACY_DISCLOSURE_FA


async def test_existing_inspection_is_reconciled_without_new_pending_status(
    role_settings: Settings,
) -> None:
    jobs = FakeJobs(created=False)
    queue = FakeQueue()
    router = _build_router(role_settings, jobs, queue)
    message = FakeMessage(99, "https://www.instagram.com/reel/DbQqWqBDLXS/")

    await _handler(router, "enqueue_url")(message)

    assert len(queue.inspections) == 1
    assert queue.inspections[0]["job_id"] == JobId("inspection-job")
    assert message.answers[0][1] is None
    assert message.answers[-1][1] == build_admin_main_keyboard()


def _router(settings: Settings) -> tuple[Any, FakeJobs, FakeQueue]:
    jobs = FakeJobs()
    queue = FakeQueue()
    return _build_router(settings, jobs, queue), jobs, queue


def _build_router(settings: Settings, jobs: FakeJobs, queue: FakeQueue) -> Any:
    return build_router(
        settings=settings,
        queue=queue,  # type: ignore[arg-type]
        repository=FakeRepository(),  # type: ignore[arg-type]
        access_policy=FakeAccessPolicy(),  # type: ignore[arg-type]
        jobs=jobs,  # type: ignore[arg-type]
        users=FakeUsers(),  # type: ignore[arg-type]
    )


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
