from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

import telegram_media_bot.telegram.handlers as handlers_module
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.models import (
    JobId,
    JobKind,
    JobRecord,
    JobStatus,
)
from telegram_media_bot.telegram.admin_menu import (
    ADMIN_WEEKLY_REPORT_BUTTON,
    build_admin_main_keyboard,
)
from telegram_media_bot.telegram.handlers import build_router
from telegram_media_bot.telegram.texts import (
    ACCESS_DENIED_TEXT,
    INSPECTION_ACTIVE_TEXT,
    INSPECTION_QUEUED_TEXT,
    START_TEXT,
)


class FakeResponse:
    def __init__(self, message_id: int = 500) -> None:
        self.message_id = message_id
        self.edits: list[str] = []

    async def edit_text(self, text: str) -> None:
        self.edits.append(text)


class FakeMessage:
    def __init__(self, user_id: int, text: str) -> None:
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
        self.caption = None
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


@pytest.fixture
def role_settings(settings: Settings) -> Settings:
    raw = settings.model_dump()
    raw["telegram"]["admin_ids"] = [99]
    return Settings.model_validate(raw)


async def test_start_shows_admin_keyboard_only_to_admin(role_settings: Settings) -> None:
    router, _jobs, _queue = _router(role_settings)
    start = _handler(router, "start")
    admin = FakeMessage(99, "/start")
    regular = FakeMessage(20, "/start")

    await start(admin, FakeState())
    await start(regular, FakeState())

    assert admin.answers[-1] == (START_TEXT, build_admin_main_keyboard())
    assert regular.answers[-1] == (START_TEXT, None)


async def test_panel_denies_regular_user_and_menu_recovers_admin_keyboard(
    role_settings: Settings,
) -> None:
    router, _jobs, _queue = _router(role_settings)
    panel = _handler(router, "panel")
    menu = _handler(router, "menu")
    regular = FakeMessage(20, "/panel")
    admin = FakeMessage(99, "/menu")

    await panel(regular, FakeState())
    await menu(admin, FakeState())

    assert regular.answers[-1][0] == ACCESS_DENIED_TEXT
    assert admin.answers[-1][1] == build_admin_main_keyboard()


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
    assert message.answers == [(INSPECTION_QUEUED_TEXT.format(job_id="inspection-job"), None)]


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
    assert message.answers == [(INSPECTION_ACTIVE_TEXT, build_admin_main_keyboard())]
    assert all(not text.startswith("بررسی لینک آغاز شد") for text, _ in message.answers)


def test_admin_router_precedes_generic_url_router(role_settings: Settings) -> None:
    router, _jobs, _queue = _router(role_settings)

    assert [child.name for child in router.sub_routers[-2:]] == ["admin", "url"]
    assert ADMIN_WEEKLY_REPORT_BUTTON in {
        button.text for row in build_admin_main_keyboard().keyboard for button in row
    }


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
