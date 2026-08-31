"""Role-authorized logger destination management boundary (T028)."""

from __future__ import annotations

from telegram_media_bot.application.ports.audit import AuditRepository, LoggerDestinationVerifier
from telegram_media_bot.domain.audit import (
    DestinationProbeOutcome,
    DestinationProbeResult,
    LoggerDestination,
    LoggerDestinationHealth,
    LoggerHealthSnapshot,
)
from telegram_media_bot.domain.errors import MediaBotError


class LoggerDestinationError(MediaBotError):
    pass


class InvalidLoggerChannelError(LoggerDestinationError):
    pass


class ConfigOwnedLoggerChannelError(LoggerDestinationError):
    pass


_PROBE_HEALTH: dict[DestinationProbeOutcome, LoggerDestinationHealth] = {
    DestinationProbeOutcome.OK: LoggerDestinationHealth.ACTIVE,
    DestinationProbeOutcome.NOT_CHANNEL: LoggerDestinationHealth.FORBIDDEN,
    DestinationProbeOutcome.BOT_NOT_MEMBER: LoggerDestinationHealth.FORBIDDEN,
    DestinationProbeOutcome.FORBIDDEN: LoggerDestinationHealth.FORBIDDEN,
    DestinationProbeOutcome.UNREACHABLE: LoggerDestinationHealth.UNREACHABLE,
    DestinationProbeOutcome.AMBIGUOUS: LoggerDestinationHealth.UNREACHABLE,
}


def validate_logger_channel_id(chat_id: int) -> int:
    """Require the numeric `-100...` Telegram channel ID form used everywhere in T027."""
    if chat_id > -1000000000000:
        raise InvalidLoggerChannelError(
            "logger destination must be a numeric -100... Telegram channel ID"
        )
    return chat_id


class LoggerDestinationAdminService:
    """Presentation-safe destination management backed by the durable T027 repository.

    Every state-changing call is expected to be role-authorized by the caller. The service never
    inspects raw Telegram exceptions, sends operational alerts, or touches job state.
    """

    def __init__(self, repository: AuditRepository, verifier: LoggerDestinationVerifier) -> None:
        self._repository = repository
        self._verifier = verifier

    def list(self) -> tuple[LoggerDestination, ...]:
        return self._repository.list_destinations()

    def add(self, chat_id: int) -> LoggerDestination:
        validate_logger_channel_id(chat_id)
        return self._repository.add_runtime_destination(chat_id)

    async def probe(self, chat_id: int) -> tuple[LoggerDestination, DestinationProbeResult]:
        """Verify the channel and bot posting permission, then record destination health."""
        result = await self._verifier.probe(chat_id)
        destination = self._repository.record_probe_health(
            chat_id,
            _PROBE_HEALTH[result.outcome],
            result.failure_class,
        )
        if destination is None:
            raise LoggerDestinationError("logger destination does not exist")
        return destination, result

    def remove(self, chat_id: int) -> LoggerDestination | None:
        """Remove a runtime destination; config-owned rows cannot be falsely removed."""
        destination = self._by_chat_id(chat_id)
        if destination is not None and destination.config_owned and not destination.runtime_owned:
            raise ConfigOwnedLoggerChannelError(
                "config-managed logger channel cannot be removed through the UI"
            )
        if not self._repository.remove_runtime_destination(chat_id):
            return None
        return destination

    def set_enabled(self, chat_id: int, enabled: bool) -> LoggerDestination:
        return self._repository.set_destination_enabled(chat_id, enabled)

    def health(self) -> LoggerHealthSnapshot:
        return self._repository.health_snapshot()

    def _by_chat_id(self, chat_id: int) -> LoggerDestination | None:
        for destination in self._repository.list_destinations():
            if destination.chat_id == chat_id:
                return destination
        return None


__all__ = [
    "ConfigOwnedLoggerChannelError",
    "InvalidLoggerChannelError",
    "LoggerDestinationAdminService",
    "LoggerDestinationError",
    "validate_logger_channel_id",
]
