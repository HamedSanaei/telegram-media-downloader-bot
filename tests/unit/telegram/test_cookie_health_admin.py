from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.types import Chat, Message

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.cookie_health import (
    CookieHealthState,
    ProviderCookieHealth,
    StaticCookieCheck,
)
from telegram_media_bot.domain.cookies import CookieService
from telegram_media_bot.telegram.admin_menu import (
    ADMIN_COOKIE_HEALTH_BUTTON,
    ADMIN_COOKIE_HEALTH_REFRESH_BUTTON,
    build_admin_cookie_health_keyboard,
)
from telegram_media_bot.telegram.handlers import build_router
from telegram_media_bot.telegram.texts import ACCESS_DENIED_TEXT


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

    async def answer(self, text: str, reply_markup: object | None = None) -> SimpleNamespace:
        self.answers.append((text, reply_markup))

        async def edit_text(*_args: object, **_kwargs: object) -> None:
            self.answers.append((str(_args[0]), None))

        return SimpleNamespace(message_id=500 + len(self.answers), edit_text=edit_text)


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


class FakeQueue:
    async def queue_depth(self) -> int:
        return 0

    async def enqueue_inspection(self, **kwargs: object) -> object:
        return kwargs["job_id"]


class FakeJobs:
    def create_inspection(self, **kwargs: object) -> tuple[object, bool]:
        return SimpleNamespace(job_id="j1", chat_id=1, user_id=1, url="u"), True


class FakeRepository:
    def set_status_message(self, _job_id: object, _message_id: int) -> None:
        return None


class FakeHealthService:
    def __init__(self) -> None:
        self.static_calls = 0

    def all_health(self) -> dict[CookieService, ProviderCookieHealth]:
        provider = CookieService.INSTAGRAM
        return {
            provider: ProviderCookieHealth(
                provider=provider,
                status=CookieHealthState.UNVERIFIED,
                static=StaticCookieCheck(
                    provider=provider,
                    status=CookieHealthState.UNVERIFIED,
                    file_ok=False,
                    safe_reason="not checked yet",
                ),
            )
        }

    def refresh_static(
        self,
        _providers: tuple[CookieService, ...] | None = None,
        *,
        clear_runtime_auth_failure: bool = False,
    ) -> tuple[dict[CookieService, ProviderCookieHealth], tuple[Any, ...]]:
        del clear_runtime_auth_failure
        self.static_calls += 1
        provider = CookieService.INSTAGRAM
        health = ProviderCookieHealth(
            provider=provider,
            status=CookieHealthState.EXPIRING_SOON,
            static=StaticCookieCheck(
                provider=provider,
                status=CookieHealthState.EXPIRING_SOON,
                file_ok=True,
                record_count=2,
            ),
            last_checked_at=datetime.now(UTC),
        )
        return {provider: health}, ()


@pytest.fixture
def admin_settings(settings: Settings) -> Settings:
    raw = settings.model_dump()
    raw["telegram"]["admin_ids"] = [99]
    return Settings.model_validate(raw)


def _router(settings: Settings, health: FakeHealthService) -> Any:
    router = build_router(
        settings=settings,
        queue=FakeQueue(),  # type: ignore[arg-type]
        repository=FakeRepository(),  # type: ignore[arg-type]
        access_policy=FakeAccessPolicy(),  # type: ignore[arg-type]
        jobs=FakeJobs(),  # type: ignore[arg-type]
        users=FakeUsers(),  # type: ignore[arg-type]
        cookie_health_service=health,  # type: ignore[arg-type]
    )
    return router


def _handler(router: object, name: str) -> Any:
    for current in (router, *router.sub_routers):  # type: ignore[attr-defined]
        for observer in current.observers.values():
            for item in observer.handlers:
                if item.callback.__name__ == name:
                    return item.callback
    raise AssertionError(f"handler {name} not found")


async def test_cookie_health_button_is_admin_only(admin_settings: Settings) -> None:
    health = FakeHealthService()
    router = _router(admin_settings, health)
    handler = _handler(router, "open_cookie_health")
    admin = FakeMessage(99, ADMIN_COOKIE_HEALTH_BUTTON)
    regular = FakeMessage(20, ADMIN_COOKIE_HEALTH_BUTTON)

    await handler(admin, FakeState())
    await handler(regular, FakeState())

    assert regular.answers[-1][0] == ACCESS_DENIED_TEXT
    assert admin.answers[-1][0].startswith("🍪")
    assert health.static_calls == 1


async def test_cookie_health_refresh_runs_static_inspection_only(
    admin_settings: Settings,
) -> None:
    health = FakeHealthService()
    router = _router(admin_settings, health)
    handler = _handler(router, "refresh_cookie_health")
    message = FakeMessage(99, ADMIN_COOKIE_HEALTH_REFRESH_BUTTON)

    await handler(message, FakeState())

    assert health.static_calls == 1
    final = message.answers[-1][0]
    assert "Expiring soon" in final
    assert "Instagram" in final


async def test_cookie_health_callback_fails_closed_for_non_admin(
    admin_settings: Settings,
) -> None:
    router = _router(admin_settings, FakeHealthService())
    handler = _handler(router, "cookie_health_callback")
    callback = SimpleNamespace(
        from_user=SimpleNamespace(
            id=20,
            username="u",
            first_name="U",
            last_name=None,
            language_code=None,
            is_premium=None,
        ),
        data="adm:ch:refresh",
        message=None,
    )
    answers: list[str] = []

    async def answer(text: str, show_alert: bool = False) -> None:
        answers.append(text)

    callback.answer = answer

    await handler(callback)

    assert answers == [ACCESS_DENIED_TEXT]


async def test_cookie_health_callback_check_for_admin(
    admin_settings: Settings,
) -> None:
    health = FakeHealthService()
    router = _router(admin_settings, health)
    handler = _handler(router, "cookie_health_callback")
    edits: list[str] = []

    class FakeBot:
        async def __call__(self, method: Any) -> Message:
            edits.append(str(getattr(method, "text", "")))
            return Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=1, type="private"),
                text=str(getattr(method, "text", "")),
            )

    message = Message.model_construct(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=1, type="private"),
        text="x",
    ).as_(cast(Any, FakeBot()))
    callback = SimpleNamespace(
        from_user=SimpleNamespace(
            id=99,
            username="a",
            first_name="A",
            last_name=None,
            language_code=None,
            is_premium=None,
        ),
        data="adm:ch:refresh",
        message=message,
    )
    answers: list[str] = []

    async def answer(text: str, show_alert: bool = False) -> None:
        answers.append(text)

    callback.answer = answer

    await handler(callback)

    assert health.static_calls == 1
    assert edits and "Expiring soon" in edits[-1]


def test_cookie_health_keyboard_exposes_static_refresh_without_live_check() -> None:
    keyboard = build_admin_cookie_health_keyboard()
    labels = [button.text for row in keyboard.keyboard for button in row]
    assert ADMIN_COOKIE_HEALTH_REFRESH_BUTTON in labels
    assert all("بررسی سلامت همه" not in label for label in labels)


def test_cookie_health_text_never_shows_cookie_values() -> None:
    from telegram_media_bot.telegram.ui import cookie_health_status_text

    health = FakeHealthService().all_health()
    text = cookie_health_status_text(health)
    assert "sessionid" not in text
    assert "=" not in text or "انقضا" in text
