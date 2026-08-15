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
from telegram_media_bot.domain.cookies import (
    MAX_COOKIE_UPLOAD_BYTES,
    CookieService,
    CookieUpdateSummary,
)
from telegram_media_bot.domain.errors import (
    EmptyCookieFileError,
    InvalidCookieFileError,
    UnsupportedCookieDomainsError,
)
from telegram_media_bot.telegram.admin_handlers import (
    ADMIN_DOWNLOAD_CANCELLED,
    ADMIN_DOWNLOAD_PROMPT,
    COOKIE_EMPTY_TEXT,
    COOKIE_INVALID_TEXT,
    COOKIE_PRIVATE_CHAT_REQUIRED_TEXT,
    COOKIE_TOO_LARGE_TEXT,
    COOKIE_UNSUPPORTED_TEXT,
    COOKIE_UPDATE_FAILED_TEXT,
    REPORT_BUSY_TEXT,
    build_admin_router,
)
from telegram_media_bot.telegram.admin_menu import (
    ADMIN_COOKIE_DOWNLOAD_BUTTON,
    ADMIN_DOWNLOAD_BUTTON,
    build_admin_cookie_keyboard,
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
    def __init__(
        self,
        user_id: int,
        text: str = "",
        *,
        document: object | None = None,
        bot: object | None = None,
        chat_type: str = "private",
    ) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.text = text
        self.caption = None
        self.document = document
        self.bot = bot
        self.chat = SimpleNamespace(id=user_id, type=chat_type)
        self.answers: list[tuple[str, object | None]] = []
        self.photos: list[tuple[object, str | None, object | None]] = []
        self.documents: list[tuple[object, str | None, object | None]] = []

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

    async def answer_document(
        self,
        document: object,
        *,
        caption: str | None = None,
        reply_markup: object | None = None,
    ) -> None:
        self.documents.append((document, caption, reply_markup))


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


class FakeTelegramBot:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.downloads = 0
        self.destinations: list[object] = []

    async def download(self, _document: object, *, destination: object) -> object:
        self.downloads += 1
        self.destinations.append(destination)
        destination.write(self.content)  # type: ignore[attr-defined]
        return destination


class FakeCookieManager:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.uploads: list[bytes] = []
        self.exports = 0

    def merge(self, uploaded: bytes) -> CookieUpdateSummary:
        self.uploads.append(uploaded)
        return CookieUpdateSummary(
            services=(CookieService.YOUTUBE, CookieService.INSTAGRAM),
            replaced=2,
            added=3,
        )

    def export_combined(self) -> bytes:
        self.exports += 1
        return self.content


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


async def test_admin_upload_uses_document_bytes_not_filename_and_reports_counts(
    admin_settings: Settings,
) -> None:
    content = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n"
    cookie_manager = FakeCookieManager(content)
    bot = FakeTelegramBot(content)

    async def submit(_message: object, _markup: object) -> bool:
        return True

    router = build_admin_router(
        settings=admin_settings,
        submit_url=submit,
        analytics=None,
        chart_renderer=None,
        cookie_manager=cookie_manager,
    )
    message = FakeMessage(
        99,
        document=SimpleNamespace(file_size=len(content), file_name="untrusted-name.bin"),
        bot=bot,
    )
    state = FakeState()

    await _handler(router, "receive_cookie_upload")(message, state)

    assert cookie_manager.uploads == [content]
    assert bot.downloads == 1
    assert state.cleared == 1
    response = message.answers[-1][0]
    assert "YouTube، Instagram" in response
    assert "جایگزین‌شده: 2" in response
    assert "افزوده‌شده: 3" in response
    assert "secret" not in response


async def test_admin_can_download_complete_combined_cookie_file(
    admin_settings: Settings,
) -> None:
    content = b"# Netscape HTTP Cookie File\ncomplete-secret-content"
    cookie_manager = FakeCookieManager(content)

    async def submit(_message: object, _markup: object) -> bool:
        return True

    router = build_admin_router(
        settings=admin_settings,
        submit_url=submit,
        analytics=None,
        chart_renderer=None,
        cookie_manager=cookie_manager,
    )
    message = FakeMessage(99, ADMIN_COOKIE_DOWNLOAD_BUTTON)

    await _handler(router, "download_combined_cookies")(message, FakeState())

    assert cookie_manager.exports == 1
    assert len(message.documents) == 1
    document, caption, markup = message.documents[0]
    assert document.data == content  # type: ignore[attr-defined]
    assert document.filename == "cookies.txt"  # type: ignore[attr-defined]
    assert caption is not None
    assert markup == build_admin_cookie_keyboard()


async def test_non_admin_cannot_download_or_upload_cookies(admin_settings: Settings) -> None:
    content = b"# Netscape HTTP Cookie File\n"
    cookie_manager = FakeCookieManager(content)

    async def submit(_message: object, _markup: object) -> bool:
        return True

    router = build_admin_router(
        settings=admin_settings,
        submit_url=submit,
        analytics=None,
        chart_renderer=None,
        cookie_manager=cookie_manager,
    )
    state = FakeState()
    message = FakeMessage(
        20,
        document=SimpleNamespace(file_size=len(content), file_name="cookies.txt"),
        bot=FakeTelegramBot(content),
    )

    await _handler(router, "receive_cookie_upload")(message, state)
    await _handler(router, "download_combined_cookies")(message, state)

    assert cookie_manager.uploads == []
    assert cookie_manager.exports == 0
    assert all(answer[0] == ACCESS_DENIED_TEXT for answer in message.answers)


async def test_cookie_export_is_rejected_outside_private_admin_chat(
    admin_settings: Settings,
) -> None:
    cookie_manager = FakeCookieManager(b"complete-secret-content")

    async def submit(_message: object, _markup: object) -> bool:
        return True

    router = build_admin_router(
        settings=admin_settings,
        submit_url=submit,
        analytics=None,
        chart_renderer=None,
        cookie_manager=cookie_manager,
    )
    message = FakeMessage(99, ADMIN_COOKIE_DOWNLOAD_BUTTON, chat_type="group")

    await _handler(router, "download_combined_cookies")(message, FakeState())

    assert cookie_manager.exports == 0
    assert message.documents == []
    assert message.answers[-1][0] == COOKIE_PRIVATE_CHAT_REQUIRED_TEXT


async def test_unexpected_cookie_failure_does_not_log_cookie_secret(
    admin_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_marker = "opaque-private-cookie-marker"
    content = b"# Netscape HTTP Cookie File\n"

    class FailingManager(FakeCookieManager):
        def merge(self, uploaded: bytes) -> CookieUpdateSummary:
            raise RuntimeError(sensitive_marker)

    class CapturingLogger:
        def __init__(self) -> None:
            self.events: list[tuple[object, dict[str, object]]] = []

        async def aerror(self, event: object, **kwargs: object) -> None:
            self.events.append((event, kwargs))

    logger = CapturingLogger()
    monkeypatch.setattr(admin_module, "logger", logger)

    async def submit(_message: object, _markup: object) -> bool:
        return True

    router = build_admin_router(
        settings=admin_settings,
        submit_url=submit,
        analytics=None,
        chart_renderer=None,
        cookie_manager=FailingManager(content),
    )
    message = FakeMessage(
        99,
        document=SimpleNamespace(file_size=len(content), file_name=sensitive_marker),
        bot=FakeTelegramBot(content),
    )

    await _handler(router, "receive_cookie_upload")(message, FakeState())

    assert message.answers[-1][0] == COOKIE_UPDATE_FAILED_TEXT
    captured = repr(logger.events) + repr(message.answers)
    assert sensitive_marker not in captured


async def test_cookie_upload_enforces_stream_limit_and_closes_memory_buffer(
    admin_settings: Settings,
) -> None:
    content = b"x" * (MAX_COOKIE_UPLOAD_BYTES + 1)
    cookie_manager = FakeCookieManager(b"")
    bot = FakeTelegramBot(content)

    async def submit(_message: object, _markup: object) -> bool:
        return True

    router = build_admin_router(
        settings=admin_settings,
        submit_url=submit,
        analytics=None,
        chart_renderer=None,
        cookie_manager=cookie_manager,
    )
    message = FakeMessage(
        99,
        document=SimpleNamespace(file_size=None, file_name="anything.dat"),
        bot=bot,
    )

    await _handler(router, "receive_cookie_upload")(message, FakeState())

    assert message.answers[-1][0] == COOKIE_TOO_LARGE_TEXT
    assert cookie_manager.uploads == []
    assert len(bot.destinations) == 1
    assert bot.destinations[0].closed is True  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("failure", "expected_text"),
    [
        (InvalidCookieFileError("fixed"), COOKIE_INVALID_TEXT),
        (EmptyCookieFileError("fixed"), COOKIE_EMPTY_TEXT),
        (UnsupportedCookieDomainsError("fixed"), COOKIE_UNSUPPORTED_TEXT),
    ],
)
async def test_cookie_validation_failures_have_clear_persian_messages(
    admin_settings: Settings,
    failure: Exception,
    expected_text: str,
) -> None:
    content = b"# Netscape HTTP Cookie File\n"

    class RejectingManager(FakeCookieManager):
        def merge(self, uploaded: bytes) -> CookieUpdateSummary:
            raise failure

    async def submit(_message: object, _markup: object) -> bool:
        return True

    router = build_admin_router(
        settings=admin_settings,
        submit_url=submit,
        analytics=None,
        chart_renderer=None,
        cookie_manager=RejectingManager(content),
    )
    message = FakeMessage(
        99,
        document=SimpleNamespace(file_size=len(content), file_name="cookies.txt"),
        bot=FakeTelegramBot(content),
    )

    await _handler(router, "receive_cookie_upload")(message, FakeState())

    assert message.answers[-1][0] == expected_text


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
