from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from telegram_media_bot.application.ports.cookie_health_repository import CookieHealthRepository
from telegram_media_bot.domain.cookie_health import (
    ActiveProbeResult,
    CookieHealthState,
    ProviderCookieHealth,
    StaticCookieCheck,
)
from telegram_media_bot.domain.cookies import CookieService
from telegram_media_bot.domain.errors import PersistenceError

_COOKIE_SERVICES = tuple(CookieService)


class SqliteCookieHealthRepository(CookieHealthRepository):
    """WAL-backed persistence for Cookie Health Center state.

    The persisted ``last_notified_state``/``last_reminder_at`` fields keep alert
    deduplication across worker/container restarts.
    """

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        except sqlite3.Error as exc:
            raise PersistenceError("Cookie health state operation failed") from exc
        finally:
            connection.close()

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cookie_health (
                    provider TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    static_json TEXT NOT NULL,
                    active_json TEXT,
                    last_checked_at TEXT,
                    last_successful_auth_check_at TEXT,
                    last_notified_state TEXT,
                    last_reminder_at TEXT
                );
                """
            )

    def load_all(self) -> dict[CookieService, ProviderCookieHealth]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM cookie_health").fetchall()
        return {_provider(row["provider"]): _health_from_row(row) for row in rows}

    def load(self, provider: CookieService) -> ProviderCookieHealth | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cookie_health WHERE provider = ?", (provider.value,)
            ).fetchone()
        return _health_from_row(row) if row is not None else None

    def save(self, health: ProviderCookieHealth) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cookie_health (
                    provider, status, static_json, active_json, last_checked_at,
                    last_successful_auth_check_at, last_notified_state, last_reminder_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    status = excluded.status,
                    static_json = excluded.static_json,
                    active_json = excluded.active_json,
                    last_checked_at = excluded.last_checked_at,
                    last_successful_auth_check_at = excluded.last_successful_auth_check_at,
                    last_notified_state = excluded.last_notified_state,
                    last_reminder_at = excluded.last_reminder_at
                """,
                (
                    health.provider.value,
                    health.status.value,
                    json.dumps(
                        _static_as_dict(health.static), ensure_ascii=False, separators=(",", ":")
                    ),
                    (
                        json.dumps(
                            _active_as_dict(health.active),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        if health.active is not None
                        else None
                    ),
                    _dump_datetime(health.last_checked_at),
                    _dump_datetime(health.last_successful_auth_check_at),
                    (
                        health.last_notified_state.value
                        if health.last_notified_state is not None
                        else None
                    ),
                    _dump_datetime(health.last_reminder_at),
                ),
            )


def _provider(value: object) -> CookieService:
    return CookieService(str(value))


def _health_from_row(row: sqlite3.Row) -> ProviderCookieHealth:
    static = _static_from_dict(json.loads(str(row["static_json"])))
    active = _active_from_dict(json.loads(str(row["active_json"]))) if row["active_json"] else None
    return ProviderCookieHealth(
        provider=CookieService(str(row["provider"])),
        status=CookieHealthState(str(row["status"])),
        static=static,
        active=active,
        last_checked_at=_load_datetime(row["last_checked_at"]),
        last_successful_auth_check_at=_load_datetime(row["last_successful_auth_check_at"]),
        last_notified_state=(
            CookieHealthState(str(row["last_notified_state"]))
            if row["last_notified_state"]
            else None
        ),
        last_reminder_at=_load_datetime(row["last_reminder_at"]),
    )


def _static_as_dict(check: StaticCookieCheck) -> dict[str, Any]:
    return {
        "provider": check.provider.value,
        "status": check.status.value,
        "file_ok": check.file_ok,
        "record_count": check.record_count,
        "earliest_expiry": _dump_datetime(check.earliest_expiry),
        "latest_expiry": _dump_datetime(check.latest_expiry),
        "malformed_record_count": check.malformed_record_count,
        "safe_reason": check.safe_reason,
        "permission_ok": check.permission_ok,
    }


def _static_from_dict(raw: dict[str, Any]) -> StaticCookieCheck:
    return StaticCookieCheck(
        provider=CookieService(str(raw["provider"])),
        status=CookieHealthState(str(raw["status"])),
        file_ok=bool(raw.get("file_ok", False)),
        record_count=int(raw.get("record_count", 0)),
        earliest_expiry=_load_datetime(raw.get("earliest_expiry")),
        latest_expiry=_load_datetime(raw.get("latest_expiry")),
        malformed_record_count=int(raw.get("malformed_record_count", 0)),
        safe_reason=raw.get("safe_reason"),
        permission_ok=bool(raw.get("permission_ok", True)),
    )


def _active_as_dict(result: ActiveProbeResult) -> dict[str, Any]:
    return {
        "provider": result.provider.value,
        "status": result.status.value,
        "probed_url": result.probed_url,
        "auth_required_endpoint": result.auth_required_endpoint,
        "http_status": result.http_status,
        "elapsed_seconds": result.elapsed_seconds,
        "safe_reason": result.safe_reason,
    }


def _active_from_dict(raw: dict[str, Any]) -> ActiveProbeResult:
    return ActiveProbeResult(
        provider=CookieService(str(raw["provider"])),
        status=CookieHealthState(str(raw["status"])),
        probed_url=raw.get("probed_url"),
        auth_required_endpoint=bool(raw.get("auth_required_endpoint", False)),
        http_status=raw.get("http_status"),
        elapsed_seconds=raw.get("elapsed_seconds"),
        safe_reason=raw.get("safe_reason"),
    )


def _dump_datetime(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="microseconds") if value is not None else None


def _load_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value)).astimezone(UTC)
