from __future__ import annotations

import logging
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import structlog

from telegram_media_bot.bootstrap.config import Settings

_SENSITIVE_FRAGMENTS = ("token", "cookie", "authorization", "password", "proxy", "secret")


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.app.log_level),
        force=True,
    )

    renderer: structlog.types.Processor
    if settings.app.log_format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            redact_sensitive_data,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.app.log_level)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def redact_sensitive_data(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
    return {key: _redact_value(key, value) for key, value in event_dict.items()}


def _redact_value(key: str, value: Any) -> Any:
    if any(fragment in key.casefold() for fragment in _SENSITIVE_FRAGMENTS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact_value(str(child_key), child)
            for child_key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(key, item) for item in value]
    if isinstance(value, str) and "://" in value:
        return _redact_url_credentials(value)
    return value


def _redact_url_credentials(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[REDACTED_URL]"
    try:
        if parsed.username is None and parsed.password is None:
            return value
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "[REDACTED_URL]"
    if hostname is None:
        return "[REDACTED_URL]"
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"[REDACTED]@{host}"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
