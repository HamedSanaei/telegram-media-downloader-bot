from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import telegram_media_bot.telegram.admin_handlers as admin_module
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.analytics import UsageReport, UsageReportPeriod
from telegram_media_bot.telegram.admin_handlers import (
    ADMIN_DOWNLOAD_CANCELLED,
    ADMIN_DOWNLOAD_PROMPT,
    REPORT_BUSY_TEXT,
    build_admin_router,
)
from telegram_media_bot.telegram.admin_menu import (
    ADMIN_DOWNLOAD_BUTTON,
    build_admin_main_keyboard,
)
from telegram_media_bot.telegram.texts import ACCESS_DENIED_TEXT


class FakeState:
    def __init__(self) -> None:
        self.value: object | None = None
        self.cleared = 0

    async def set_state(self, value: object) -> None:
        self.value = value

    async def clear(self) -> None:
        self.value = None
        self.cleared += 1


class FakeSentMessage:
    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit_text(self, text: str) -> None:
        self.edits.append(text)


class FakeMessage:
    def __init__(self, user_id: int, text: str = "") -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.text = text
        self.caption = None
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.answers: list[tuple[str, object | None]] = []
        self.photos: list[tuple[object, str | None, object | None]] = []

    async def answer(self, text: str, reply_markup: object | None = None) -> FakeSentMessage:
        self.answers.append((text, reply_markup))
        return FakeSentMessage()

    async def answer_photo(
        self,
        photo: object,
        *,
        caption: str | None = None,
        reply_markup: object | None = None,
    ) -> None:
        self.photos.append((photo, caption, reply_markup))


class FakeCallback:
    def __init__(self, user_id: int, data: str) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.message = FakeMessage(user_id)
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


class FakeAnalytics:
    def build(self, period: UsageReportPeriod) -> UsageReport:
        return _report(period)


class BlockingAnalytics(FakeAnalytics):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def build(self, period: UsageReportPeriod) -> UsageReport:
        self.calls += 1
        self.started.set()
        assert self.release.wait(2)
        return _report(period)


class FakeRenderer:
    def render(self, _report: UsageReport) -> bytes:
        return b"\x89PNG\r\n\x1a\nfixture"


@pytest.fixture
def admin_settings(settings: Settings) -> Settings:
    raw = settings.model_dump()
    raw["telegram"]["admin_ids"] = [99]
    return Settings.model_validate(raw)


async def test_download_button_enters_state_and_cancel_restores_menu(
    admin_settings: Settings,
) -> None:
    async def submit(_message: object, _markup: object) -> bool:
        raise AssertionError("download prompt must not submit a URL")

    router = build_admin_router(
        settings=admin_settings,
        submit_url=submit,
        analytics=FakeAnalytics(),  # type: ignore[arg-type]
        chart_renderer=FakeRenderer(),
    )
    state = FakeState()
    message = FakeMessage(99, ADMIN_DOWNLOAD_BUTTON)

    await _handler(router, "begin_admin_download")(message, state)

    assert state.value is not None
    assert message.answers[-1][0] == ADMIN_DOWNLOAD_PROMPT

    await _handler(router, "cancel_admin_download")(message, state)

    assert state.value is None
    assert message.answers[-1][0] == ADMIN_DOWNLOAD_CANCELLED
    assert message.answers[-1][1] == build_admin_main_keyboard()


async def test_valid_admin_url_uses_injected_shared_pipeline_and_clears_state(
    admin_settings: Settings,
) -> None:
    calls: list[object] = []

    async def submit(message: object, markup: object) -> bool:
        calls.append((message, markup))
        return True

    router = build_admin_router(
        settings=admin_settings,
        submit_url=submit,
        analytics=FakeAnalytics(),  # type: ignore[arg-type]
        chart_renderer=FakeRenderer(),
    )
    state = FakeState()
    message = FakeMessage(99, "https://example.com/video")

    await _handler(router, "receive_admin_url")(message, state)

    assert len(calls) == 1
    assert state.cleared == 1


async def test_invalid_admin_url_keeps_state_for_retry(admin_settings: Settings) -> None:
    async def submit(_message: object, _markup: object) -> bool:
        return False

    router = build_admin_router(
        settings=admin_settings,
        submit_url=submit,
        analytics=FakeAnalytics(),  # type: ignore[arg-type]
        chart_renderer=FakeRenderer(),
    )
    state = FakeState()

    await _handler(router, "receive_admin_url")(FakeMessage(99, "bad"), state)

    assert state.cleared == 0


async def test_non_admin_forged_button_is_denied_without_state(
    admin_settings: Settings,
) -> None:
    async def submit(_message: object, _markup: object) -> bool:
        raise AssertionError("forged button must not reach URL submission")

    router = build_admin_router(
        settings=admin_settings,
        submit_url=submit,
        analytics=FakeAnalytics(),  # type: ignore[arg-type]
        chart_renderer=FakeRenderer(),
    )
    state = FakeState()
    message = FakeMessage(20, ADMIN_DOWNLOAD_BUTTON)

    await _handler(router, "begin_admin_download")(message, state)

    assert state.value is None
    assert message.answers[-1][0] == ACCESS_DENIED_TEXT


async def test_report_generation_is_single_flight_per_admin(
    admin_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_module, "Message", FakeMessage)
    analytics = BlockingAnalytics()

    async def submit(_message: object, _markup: object) -> bool:
        return True

    router = build_admin_router(
        settings=admin_settings,
        submit_url=submit,
        analytics=analytics,  # type: ignore[arg-type]
        chart_renderer=FakeRenderer(),
    )
    first = FakeMessage(99)
    second = FakeMessage(99)
    state = FakeState()
    handler = _handler(router, "weekly_report")
    task = asyncio.create_task(handler(first, state))
    assert await asyncio.to_thread(analytics.started.wait, 1)

    await handler(second, FakeState())
    analytics.release.set()
    await task

    assert analytics.calls == 1
    assert second.answers[-1][0] == REPORT_BUSY_TEXT
    assert len(first.photos) == 1


@pytest.mark.parametrize(
    ("handler_name", "period", "expects_photo"),
    [
        ("weekly_report", UsageReportPeriod.WEEKLY, True),
        ("monthly_report", UsageReportPeriod.MONTHLY, True),
        ("full_report", UsageReportPeriod.FULL, False),
    ],
)
async def test_each_report_button_works_without_panel(
    admin_settings: Settings,
    handler_name: str,
    period: UsageReportPeriod,
    expects_photo: bool,
) -> None:
    class CapturingAnalytics(FakeAnalytics):
        def __init__(self) -> None:
            self.periods: list[UsageReportPeriod] = []

        def build(self, selected: UsageReportPeriod) -> UsageReport:
            self.periods.append(selected)
            return _report(selected)

    analytics = CapturingAnalytics()

    async def submit(_message: object, _markup: object) -> bool:
        return True

    router = build_admin_router(
        settings=admin_settings,
        submit_url=submit,
        analytics=analytics,  # type: ignore[arg-type]
        chart_renderer=FakeRenderer(),
    )
    message = FakeMessage(99)

    await _handler(router, handler_name)(message, FakeState())

    assert analytics.periods == [period]
    assert bool(message.photos) is expects_photo
    assert message.answers[0][1] == build_admin_main_keyboard()


async def test_forged_report_refresh_callback_is_denied(
    admin_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_module, "Message", FakeMessage)

    async def submit(_message: object, _markup: object) -> bool:
        return True

    router = build_admin_router(
        settings=admin_settings,
        submit_url=submit,
        analytics=FakeAnalytics(),  # type: ignore[arg-type]
        chart_renderer=FakeRenderer(),
    )
    callback = FakeCallback(20, "adm:rpt:w:refresh")

    await _handler(router, "refresh_report")(callback)

    assert callback.answers == [(ACCESS_DENIED_TEXT, True)]
    assert callback.message.photos == []


def _handler(router: object, name: str) -> Any:
    for observer in router.observers.values():  # type: ignore[attr-defined]
        for item in observer.handlers:
            if item.callback.__name__ == name:
                return item.callback
    raise AssertionError(f"handler {name} not found")


def _report(period: UsageReportPeriod) -> UsageReport:
    now = datetime.now(UTC)
    return UsageReport(
        period=period,
        start_at=now,
        end_at=now,
        unique_users=1,
        interactions=1,
        downloads=1,
        succeeded=1,
        failed=0,
        cancelled=0,
        delivered_bytes=100,
        sources=(),
        formats=(),
        daily=(),
    )
