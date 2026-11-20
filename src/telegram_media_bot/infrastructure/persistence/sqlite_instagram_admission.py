from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from telegram_media_bot.domain.instagram_admission import (
    AdmissionDecision,
    InstagramAdmissionError,
    InstagramAdmissionState,
    InstagramAdmissionSummary,
)


class SqliteInstagramAdmissionRepository:
    """Durable quotas/circuit breakers; Redis loss cannot reset safety state.

    An interrupted operation remains active until explicit offline review. We deliberately do
    not expire durable active markers: a lost Redis lease must never admit a second extractor.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous = FULL")
            try:
                yield connection
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise InstagramAdmissionError("state_unavailable") from exc

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS instagram_admission (
                    identity TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    active_token TEXT, reason TEXT NOT NULL DEFAULT 'ready',
                    pause_until REAL, next_start REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS instagram_operations (
                    identity TEXT NOT NULL, started REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS instagram_operations_identity_time
                    ON instagram_operations(identity, started);
                CREATE TABLE IF NOT EXISTS instagram_waiters (
                    identity TEXT NOT NULL, token TEXT PRIMARY KEY, expires REAL NOT NULL
                );
            """)

    def admit(
        self,
        identity: str,
        kind: str,
        token: str,
        *,
        now: float,
        spacing: float,
        burst_window: int,
        burst_limit: int,
        daily_limit: int,
    ) -> AdmissionDecision:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO instagram_admission(identity, kind) VALUES (?, ?)",
                (identity, kind),
            )
            row = db.execute(
                "SELECT * FROM instagram_admission WHERE identity=?", (identity,)
            ).fetchone()
            assert row is not None
            decision = _pause_decision(row, now)
            if decision.allowed and row["active_token"] is not None:
                decision = AdmissionDecision(False, "active", 1)
            if decision.allowed and row["next_start"] > now:
                decision = AdmissionDecision(False, "spacing", row["next_start"] - now)
            if decision.allowed:
                db.execute(
                    "DELETE FROM instagram_operations WHERE identity=? AND started<=?",
                    (identity, now - 86400),
                )
                times = [
                    float(r[0])
                    for r in db.execute(
                        "SELECT started FROM instagram_operations WHERE identity=? ORDER BY started",
                        (identity,),
                    )
                ]
                recent = [t for t in times if t > now - burst_window]
                if len(times) >= daily_limit:
                    decision = AdmissionDecision(
                        False, "daily_budget", times[-daily_limit] + 86400 - now
                    )
                elif len(recent) >= burst_limit:
                    decision = AdmissionDecision(
                        False, "burst_budget", recent[-burst_limit] + burst_window - now
                    )
            if decision.allowed:
                db.execute(
                    "UPDATE instagram_admission SET active_token=?, next_start=?, reason='ready', pause_until=NULL WHERE identity=?",
                    (token, now + spacing, identity),
                )
                db.execute("INSERT INTO instagram_operations VALUES (?, ?)", (identity, now))
            elif decision.reason in {"daily_budget", "burst_budget"}:
                db.execute(
                    "UPDATE instagram_admission SET reason=?, pause_until=? WHERE identity=?",
                    (decision.reason, now + (decision.retry_after_seconds or 0), identity),
                )
            db.execute("COMMIT")
            return decision

    def finish(self, identity: str, token: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE instagram_admission SET active_token=NULL WHERE identity=? AND active_token=?",
                (identity, token),
            )

    def pause(self, identity: str, *, reason: str, until: float | None) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT reason, pause_until FROM instagram_admission WHERE identity=?", (identity,)
            ).fetchone()
            if row is None:
                raise InstagramAdmissionError("state_unavailable")
            # Manual review beats timed pauses, and a later signal cannot shorten a deadline.
            if row["reason"] != "ready":
                if row["pause_until"] is None:
                    db.execute("COMMIT")
                    return False
                if until is not None:
                    until = max(until, row["pause_until"])
            changed = bool(row["reason"] != reason or row["pause_until"] != until)
            db.execute(
                "UPDATE instagram_admission SET reason=?, pause_until=? WHERE identity=?",
                (reason, until, identity),
            )
            db.execute("COMMIT")
            return changed

    def check_pause(self, identity: str, *, now: float) -> AdmissionDecision:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM instagram_admission WHERE identity=?", (identity,)
            ).fetchone()
        return _pause_decision(row, now) if row is not None else AdmissionDecision(True)

    def waiter(self, identity: str, token: str, *, until: float | None) -> None:
        with self._connect() as db:
            if until is None:
                db.execute("DELETE FROM instagram_waiters WHERE token=?", (token,))
            else:
                db.execute("DELETE FROM instagram_waiters WHERE expires<=?", (until - 605,))
                db.execute(
                    "INSERT INTO instagram_waiters VALUES (?, ?, ?) ON CONFLICT(token) DO UPDATE SET expires=excluded.expires",
                    (identity, token, until),
                )

    def snapshot(self, *, now: float, burst_window: int) -> tuple[InstagramAdmissionState, ...]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT s.*,
                    (SELECT COUNT(*) FROM instagram_operations o WHERE o.identity=s.identity AND started>?) recent,
                    (SELECT COUNT(*) FROM instagram_operations o WHERE o.identity=s.identity AND started>?) daily,
                    (SELECT COUNT(*) FROM instagram_waiters w WHERE w.identity=s.identity AND expires>?) waiting
                FROM instagram_admission s ORDER BY s.identity
            """,
                (now - burst_window, now - 86400, now),
            ).fetchall()
        return tuple(
            InstagramAdmissionState(
                r["identity"],
                r["kind"],
                _pause_decision(r, now).reason,
                r["pause_until"],
                r["active_token"] is not None,
                r["recent"],
                r["daily"],
                r["waiting"],
            )
            for r in rows
        )

    def summary(self, *, now: float) -> InstagramAdmissionSummary:
        """Health probes need aggregate counts, never per-credential operation histories."""
        with self._connect() as db:
            row = db.execute(
                """
                SELECT COUNT(*) credentials,
                    COALESCE(SUM(reason!='ready' AND (pause_until IS NULL OR pause_until>?)),0) paused,
                    COALESCE(SUM(active_token IS NOT NULL),0) active,
                    (SELECT COUNT(*) FROM instagram_waiters WHERE expires>?) waiting
                FROM instagram_admission
            """,
                (now, now),
            ).fetchone()
        return InstagramAdmissionSummary(
            row["credentials"], row["paused"], row["active"], row["waiting"]
        )

    def review(self, identity: str, *, now: float, clear_interrupted: bool = False) -> bool:
        """Operator-only offline recovery; retain history, spacing and all quota charges."""
        with self._connect() as db:
            query = "UPDATE instagram_admission SET reason='ready', pause_until=NULL"
            if clear_interrupted:
                query += ", active_token=NULL"
            query += " WHERE identity=? AND (pause_until IS NULL OR pause_until<=?)"
            if not clear_interrupted:
                query += " AND active_token IS NULL"
            return db.execute(query, (identity, now)).rowcount == 1


def _pause_decision(row: sqlite3.Row, now: float) -> AdmissionDecision:
    if row["reason"] != "ready" and (row["pause_until"] is None or row["pause_until"] > now):
        return AdmissionDecision(
            False, row["reason"], None if row["pause_until"] is None else row["pause_until"] - now
        )
    return AdmissionDecision(True)
