"""T028: administrator logger destination management (UX + role authorization)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import telegram_media_bot.telegram.admin_handlers as admin_module
from telegram_media_bot.application.services.audit_destination_admin import (
    ConfigOwnedLoggerChannelError,
    InvalidLoggerChannelError,
    LoggerDestinationAdminService,
)
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.audit import (
    DestinationProbeOutcome,
    DestinationProbeResult,
    LoggerDestinationHealth,
)
from telegram_media_bot.infrastructure.persistence.sqlite_audit import SqliteAuditRepository
from telegram_media_bot.telegram.admin_handlers import build_admin_router
from telegram_media_bot.telegram.admin_menu import ADMIN_LOGGER_BUTTON
from telegram_media_bot.telegram.texts import ACCESS_DENIED_TEXT

CHANNEL = -1001234567890
CONFIG_CHANNEL = -1002222222222


class FakeVerifier:
    def __init__(self, outcome: DestinationProbeOutcome) -> None:
        self.outcome = outcome
        self.calls: list[int] = []

    async def probe(self, chat_id: int) -> DestinationProbeResult:
        self.calls.append(chat_id)
        failure = None if self.outcome is DestinationProbeOutcome.OK else "FakeFailure"
        return DestinationProbeResult(self.outcome, failure)


def _service(
    tmp_path: Path, outcome: DestinationProbeOutcome = DestinationProbeOutcome.OK
) -> tuple[LoggerDestinationAdminService, SqliteAuditRepository, FakeVerifier]:
    repository = SqliteAuditRepository(tmp_path / "audit.db")
    repository.initialize()
    verifier = FakeVerifier(outcome)
    return LoggerDestinationAdminService(repository, verifier), repository, verifier


# ---------------------------------------------------------------------------
# Service-level destination administration
# ---------------------------------------------------------------------------


def test_add_runtime_destination(tmp_path: Path) -> None:
    service, _repository, _verifier = _service(tmp_path)
    destination = service.add(CHANNEL)
    assert destination.chat_id == CHANNEL
    assert destination.runtime_owned
    assert not destination.config_owned
    assert destination.enabled
    assert destination.health is LoggerDestinationHealth.ACTIVE


def test_invalid_channel_id_rejected(tmp_path: Path) -> None:
    service, _repository, _verifier = _service(tmp_path)
    with pytest.raises(InvalidLoggerChannelError):
        service.add(123)  # not a -100... channel
    with pytest.raises(InvalidLoggerChannelError):
        service.add(-100)  # malformed negative id


def test_duplicate_add_is_idempotent(tmp_path: Path) -> None:
    service, repository, _verifier = _service(tmp_path)
    first = service.add(CHANNEL)
    second = service.add(CHANNEL)
    assert first.chat_id == second.chat_id
    destinations = repository.list_destinations()
    assert len(destinations) == 1


def test_probe_success_marks_active(tmp_path: Path) -> None:
    service, _repository, verifier = _service(tmp_path)
    service.add(CHANNEL)
    destination, result = asyncio.run(service.probe(CHANNEL))
    assert result.outcome is DestinationProbeOutcome.OK
    assert destination.health is LoggerDestinationHealth.ACTIVE
    assert verifier.calls == [CHANNEL]


def test_probe_forbidden_marks_forbidden(tmp_path: Path) -> None:
    service, _repository, _verifier = _service(tmp_path, DestinationProbeOutcome.FORBIDDEN)
    service.add(CHANNEL)
    destination, result = asyncio.run(service.probe(CHANNEL))
    assert result.outcome is DestinationProbeOutcome.FORBIDDEN
    assert destination.health is LoggerDestinationHealth.FORBIDDEN
    assert destination.last_failure_class is not None


def test_probe_unreachable_marks_unreachable(tmp_path: Path) -> None:
    service, _repository, _verifier = _service(tmp_path, DestinationProbeOutcome.UNREACHABLE)
    service.add(CHANNEL)
    destination, _result = asyncio.run(service.probe(CHANNEL))
    assert destination.health is LoggerDestinationHealth.UNREACHABLE


def test_config_runtime_union_deduplicates(tmp_path: Path) -> None:
    repository = SqliteAuditRepository(tmp_path / "audit.db")
    repository.initialize()
    repository.reconcile_config((CONFIG_CHANNEL,))
    service = LoggerDestinationAdminService(repository, FakeVerifier(DestinationProbeOutcome.OK))
    service.add(CONFIG_CHANNEL)
    destinations = repository.list_destinations()
    assert len(destinations) == 1
    destination = destinations[0]
    assert destination.config_owned and destination.runtime_owned


def test_remove_runtime_preserves_config_destination(tmp_path: Path) -> None:
    repository = SqliteAuditRepository(tmp_path / "audit.db")
    repository.initialize()
    repository.reconcile_config((CONFIG_CHANNEL,))
    service = LoggerDestinationAdminService(repository, FakeVerifier(DestinationProbeOutcome.OK))
    service.add(CONFIG_CHANNEL)
    removed = service.remove(CONFIG_CHANNEL)
    assert removed is not None
    destinations = repository.list_destinations()
    assert len(destinations) == 1
    destination = destinations[0]
    assert destination.config_owned
    assert not destination.runtime_owned
    assert destination.enabled  # config ownership keeps it active


def test_remove_config_only_raises(tmp_path: Path) -> None:
    repository = SqliteAuditRepository(tmp_path / "audit.db")
    repository.initialize()
    repository.reconcile_config((CONFIG_CHANNEL,))
    service = LoggerDestinationAdminService(repository, FakeVerifier(DestinationProbeOutcome.OK))
    with pytest.raises(ConfigOwnedLoggerChannelError):
        service.remove(CONFIG_CHANNEL)


def test_enable_disable(tmp_path: Path) -> None:
    service, _repository, _verifier = _service(tmp_path)
    service.add(CHANNEL)
    disabled = service.set_enabled(CHANNEL, False)
    assert not disabled.enabled
    assert disabled.health is LoggerDestinationHealth.DISABLED
    enabled = service.set_enabled(CHANNEL, True)
    assert enabled.enabled
    assert enabled.health is LoggerDestinationHealth.ACTIVE


def test_remove_unknown_returns_none(tmp_path: Path) -> None:
    service, _repository, _verifier = _service(tmp_path)
    assert service.remove(CHANNEL) is None


# ---------------------------------------------------------------------------
# Handler authorization and UX
# ---------------------------------------------------------------------------


class FakeState:
    def __init__(self) -> None:
        self.value: object | None = None
        self.cleared = 0

    async def set_state(self, value: object) -> None:
        self.value = value

    async def clear(self) -> None:
        self.value = None
        self.cleared += 1


class FakeMessage:
    def __init__(
        self,
        user_id: int,
        text: str = "",
        *,
        chat_type: str = "private",
    ) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.text = text
        self.chat = SimpleNamespace(id=user_id, type=chat_type)
        self.answers: list[tuple[str, object | None]] = []
        self.edits: list[str] = []

    async def answer(self, text: str, reply_markup: object | None = None) -> None:
        self.answers.append((text, reply_markup))

    async def edit_text(self, text: str, *, reply_markup: object | None = None) -> None:
        del reply_markup
        self.edits.append(text)


class FakeCallback:
    def __init__(self, user_id: int, data: str) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.message = FakeMessage(user_id)
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


def _router(settings: Settings, service: LoggerDestinationAdminService | None) -> Any:
    return build_admin_router(
        settings=settings,
        submit_url=lambda *_args: True,  # type: ignore[arg-type]
        analytics=None,
        chart_renderer=None,
        audit_admin=service,
    )


def _handler(router: object, name: str) -> Any:
    for observer in router.observers.values():  # type: ignore[attr-defined]
        for item in observer.handlers:
            if item.callback.__name__ == name:
                return item.callback
    raise AssertionError(f"handler {name} not found")


@pytest.fixture
def admin_settings(settings: Settings) -> Settings:
    raw = settings.model_dump()
    raw["telegram"]["admin_ids"] = [99]
    return Settings.model_validate(raw)


def test_admin_can_open_logger_menu(admin_settings: Settings, tmp_path: Path) -> None:
    service, _repository, _verifier = _service(tmp_path)
    router = _router(admin_settings, service)
    message = FakeMessage(99, ADMIN_LOGGER_BUTTON)
    state = FakeState()

    asyncio.run(_handler(router, "open_logger_menu")(message, state))

    assert message.answers[-1][0].startswith("🧾 مدیریت کانال")
    assert "هیچ کانال لاگری ثبت نشده است." in message.answers[-1][0]


def test_non_admin_forged_logger_button_denied(admin_settings: Settings, tmp_path: Path) -> None:
    service, _repository, _verifier = _service(tmp_path)
    router = _router(admin_settings, service)
    message = FakeMessage(20, ADMIN_LOGGER_BUTTON)
    state = FakeState()

    asyncio.run(_handler(router, "open_logger_menu")(message, state))

    assert state.value is None
    assert message.answers[-1][0] == ACCESS_DENIED_TEXT


def test_forged_logger_callback_rejected(admin_settings: Settings, tmp_path: Path) -> None:
    service, _repository, _verifier = _service(tmp_path)
    router = _router(admin_settings, service)
    callback = FakeCallback(20, f"adm:lg:test:{CHANNEL}")

    asyncio.run(_handler(router, "logger_callback")(callback))

    assert callback.answers == [(ACCESS_DENIED_TEXT, True)]


def test_non_admin_cannot_complete_add_state(admin_settings: Settings, tmp_path: Path) -> None:
    service, repository, _verifier = _service(tmp_path)
    router = _router(admin_settings, service)
    message = FakeMessage(20, str(CHANNEL))
    state = FakeState()

    asyncio.run(_handler(router, "receive_logger_chat_id")(message, state))

    assert message.answers[-1][0] == ACCESS_DENIED_TEXT
    assert repository.list_destinations() == ()


def test_add_valid_channel_via_state_flow(admin_settings: Settings, tmp_path: Path) -> None:
    service, repository, verifier = _service(tmp_path)
    router = _router(admin_settings, service)
    message = FakeMessage(99, str(CHANNEL))
    state = FakeState()

    asyncio.run(_handler(router, "receive_logger_chat_id")(message, state))

    assert state.value is None
    destinations = repository.list_destinations()
    assert len(destinations) == 1
    assert destinations[0].chat_id == CHANNEL
    assert verifier.calls == [CHANNEL]  # add probes the channel


def test_add_invalid_channel_id_rejected(admin_settings: Settings, tmp_path: Path) -> None:
    service, repository, _verifier = _service(tmp_path)
    router = _router(admin_settings, service)
    state = FakeState()

    handler = _handler(router, "receive_logger_chat_id")
    # Non-numeric
    asyncio.run(handler(FakeMessage(99, "not-a-number"), state))
    assert state.cleared == 0  # state remains for a retry
    assert repository.list_destinations() == ()
    # Malformed negative id
    asyncio.run(handler(FakeMessage(99, "-100"), state))
    assert state.cleared == 0
    assert repository.list_destinations() == ()


def test_logger_callback_test_updates_health(
    admin_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admin_module, "Message", FakeMessage)
    service, repository, verifier = _service(tmp_path, DestinationProbeOutcome.FORBIDDEN)
    service.add(CHANNEL)
    router = _router(admin_settings, service)
    callback = FakeCallback(99, f"adm:lg:test:{CHANNEL}")

    asyncio.run(_handler(router, "logger_callback")(callback))

    assert verifier.calls == [CHANNEL]
    destination = next(d for d in repository.list_destinations() if d.chat_id == CHANNEL)
    assert destination.health is LoggerDestinationHealth.FORBIDDEN


def test_logger_callback_enable_disable(
    admin_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admin_module, "Message", FakeMessage)
    service, repository, _verifier = _service(tmp_path)
    service.add(CHANNEL)
    router = _router(admin_settings, service)

    asyncio.run(_handler(router, "logger_callback")(FakeCallback(99, f"adm:lg:disable:{CHANNEL}")))
    destination = next(d for d in repository.list_destinations() if d.chat_id == CHANNEL)
    assert not destination.enabled

    asyncio.run(_handler(router, "logger_callback")(FakeCallback(99, f"adm:lg:enable:{CHANNEL}")))
    destination = next(d for d in repository.list_destinations() if d.chat_id == CHANNEL)
    assert destination.enabled


def test_logger_callback_remove_requires_confirm(
    admin_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admin_module, "Message", FakeMessage)
    service, repository, _verifier = _service(tmp_path)
    service.add(CHANNEL)
    router = _router(admin_settings, service)

    # First tap arms confirmation
    asyncio.run(_handler(router, "logger_callback")(FakeCallback(99, f"adm:lg:remove:{CHANNEL}")))
    assert len(repository.list_destinations()) == 1
    # Confirm removes
    asyncio.run(_handler(router, "logger_callback")(FakeCallback(99, f"adm:lg:confirm:{CHANNEL}")))
    assert repository.list_destinations() == ()


def test_logger_callback_remove_config_owned_denied(
    admin_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admin_module, "Message", FakeMessage)
    repository = SqliteAuditRepository(tmp_path / "audit.db")
    repository.initialize()
    repository.reconcile_config((CONFIG_CHANNEL,))
    service = LoggerDestinationAdminService(repository, FakeVerifier(DestinationProbeOutcome.OK))
    router = _router(admin_settings, service)

    asyncio.run(
        _handler(router, "logger_callback")(FakeCallback(99, f"adm:lg:confirm:{CONFIG_CHANNEL}"))
    )
    destinations = repository.list_destinations()
    assert len(destinations) == 1
    assert destinations[0].config_owned
    assert destinations[0].enabled


def test_logger_menu_unavailable_when_service_missing(admin_settings: Settings) -> None:
    router = _router(admin_settings, None)
    message = FakeMessage(99, ADMIN_LOGGER_BUTTON)
    state = FakeState()

    asyncio.run(_handler(router, "open_logger_menu")(message, state))

    assert message.answers[-1][0] == "سرویس لاگر در دسترس نیست."


def test_ownership_label_visible(admin_settings: Settings, tmp_path: Path) -> None:
    repository = SqliteAuditRepository(tmp_path / "audit.db")
    repository.initialize()
    repository.reconcile_config((CONFIG_CHANNEL,))
    service = LoggerDestinationAdminService(repository, FakeVerifier(DestinationProbeOutcome.OK))
    service.add(CHANNEL)
    router = _router(admin_settings, service)
    message = FakeMessage(99, ADMIN_LOGGER_BUTTON)
    state = FakeState()

    asyncio.run(_handler(router, "open_logger_menu")(message, state))

    text = message.answers[-1][0]
    assert str(CONFIG_CHANNEL) in text
    assert str(CHANNEL) in text
    assert "پیکربندی" in text  # config ownership is visible
    assert "مدیر" in text  # runtime ownership is visible
