from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_media_bot.domain.cookie_health import CookieHealthState, StaticCookieCheck
from telegram_media_bot.domain.cookies import CookieService
from telegram_media_bot.infrastructure.cookies.health import (
    MissingCookieChecker,
    NetscapeStaticCookieChecker,
)
from telegram_media_bot.infrastructure.cookies.manager import NetscapeCookieManager

_HEADER = b"# Netscape HTTP Cookie File\n"
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _record(domain: str, name: str, value: str, expires: int) -> bytes:
    return f"{domain}\tTRUE\t/\tTRUE\t{expires}\t{name}\t{value}\n".encode()


def _store(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "cookies.txt"
    path.write_bytes(content)
    return path


def _check(path: Path, provider: CookieService = CookieService.INSTAGRAM) -> StaticCookieCheck:
    checker = NetscapeStaticCookieChecker(NetscapeCookieManager(path))
    return checker.check(provider, now=_NOW, expiring_soon_hours=24)


def _expires_in(days: int) -> int:
    return int((_NOW + timedelta(days=days)).timestamp())


def test_valid_cookies_are_healthy(tmp_path: Path) -> None:
    path = _store(tmp_path, _HEADER + _record(".instagram.com", "sessionid", "s", _expires_in(30)))
    check = _check(path)
    assert check.status is CookieHealthState.HEALTHY
    assert check.file_ok is True
    assert check.record_count == 1
    assert check.malformed_record_count == 0


def test_expired_cookies_are_expired(tmp_path: Path) -> None:
    path = _store(tmp_path, _HEADER + _record(".instagram.com", "sessionid", "s", _expires_in(-1)))
    check = _check(path)
    assert check.status is CookieHealthState.EXPIRED
    assert check.earliest_expiry is not None
    assert check.earliest_expiry <= _NOW


def test_expiring_soon_detected(tmp_path: Path) -> None:
    expires = int((_NOW + timedelta(hours=8)).timestamp())
    path = _store(tmp_path, _HEADER + _record(".instagram.com", "sessionid", "s", expires))
    check = _check(path)
    assert check.status is CookieHealthState.EXPIRING_SOON


def test_mixed_expiry_uses_earliest(tmp_path: Path) -> None:
    content = (
        _HEADER
        + _record(".instagram.com", "sessionid", "s", _expires_in(30))
        + _record(".instagram.com", "csrftoken", "c", _expires_in(-2))
    )
    check = _check(_store(tmp_path, content))
    assert check.status is CookieHealthState.EXPIRED
    assert check.record_count == 2


def test_missing_provider_domain_is_missing(tmp_path: Path) -> None:
    path = _store(tmp_path, _HEADER + _record(".youtube.com", "SID", "s", _expires_in(30)))
    check = _check(path, provider=CookieService.INSTAGRAM)
    assert check.status is CookieHealthState.MISSING
    assert check.record_count == 0


def test_malformed_file_is_malformed(tmp_path: Path) -> None:
    path = _store(tmp_path, b"# Netscape HTTP Cookie File\nnot a tab separated record\n")
    check = _check(path)
    assert check.status is CookieHealthState.MALFORMED
    assert check.file_ok is True


def test_missing_file_is_missing(tmp_path: Path) -> None:
    path = tmp_path / "does-not-exist.txt"
    check = _check(path)
    assert check.status is CookieHealthState.MISSING
    assert check.file_ok is False


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_world_writable_file_reports_permission_warning(tmp_path: Path) -> None:
    path = _store(tmp_path, _HEADER + _record(".instagram.com", "sessionid", "s", _expires_in(30)))
    path.chmod(0o666)
    check = _check(path)
    assert check.status is CookieHealthState.HEALTHY
    assert check.permission_ok is False
    assert stat.S_IMODE(path.stat().st_mode) & 0o022


def test_missing_cookie_checker_reports_missing_for_every_provider() -> None:
    checker = MissingCookieChecker()
    check = checker.check(CookieService.YOUTUBE, now=_NOW, expiring_soon_hours=24)
    assert check.status is CookieHealthState.MISSING
    assert check.file_ok is False


def test_session_cookies_do_not_count_as_expired(tmp_path: Path) -> None:
    # expires=0 session cookies are long-lived in the Netscape contract.
    path = _store(tmp_path, _HEADER + _record(".instagram.com", "sessionid", "s", 0))
    check = _check(path)
    assert check.status is CookieHealthState.MISSING  # no dated records -> nothing to expire
    assert check.record_count == 0
