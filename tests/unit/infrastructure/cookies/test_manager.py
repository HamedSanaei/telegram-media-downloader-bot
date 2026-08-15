from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from telegram_media_bot.domain.cookies import CookieService
from telegram_media_bot.domain.errors import (
    CookieStoreWriteError,
    EmptyCookieFileError,
    InvalidCookieFileError,
    UnsupportedCookieDomainsError,
)
from telegram_media_bot.infrastructure.cookies.manager import NetscapeCookieManager

_HEADER = b"# Netscape HTTP Cookie File\n"


def test_youtube_upload_preserves_instagram_and_unrelated_records_exactly(
    tmp_path: Path,
) -> None:
    instagram = _record(".instagram.com", "sessionid", "instagram-secret", ending="\r\n")
    unrelated = _record(".example.net", "opaque", "unrelated-secret", ending="\r\n")
    current = b"# Netscape HTTP Cookie File\r\n# retained comment\r\n" + instagram + unrelated
    path = _store(tmp_path, current)

    summary = NetscapeCookieManager(path).merge(
        _HEADER + _record(".youtube.com", "SID", "youtube-new")
    )

    updated = path.read_bytes()
    assert instagram in updated
    assert unrelated in updated
    assert b"youtube-new" in updated
    assert summary.services == (CookieService.YOUTUBE,)
    assert summary.replaced == 0
    assert summary.added == 1


def test_instagram_upload_preserves_youtube_record_exactly(tmp_path: Path) -> None:
    youtube = _record(".youtube.com", "SID", "youtube-secret")
    path = _store(
        tmp_path,
        _HEADER + youtube + _record(".instagram.com", "sessionid", "instagram-old"),
    )

    summary = NetscapeCookieManager(path).merge(
        _HEADER + _record(".instagram.com", "sessionid", "instagram-new")
    )

    updated = path.read_bytes()
    assert youtube in updated
    assert b"instagram-new" in updated
    assert b"instagram-old" not in updated
    assert summary.services == (CookieService.INSTAGRAM,)
    assert summary.replaced == 1
    assert summary.added == 0


def test_multi_service_upload_updates_every_detected_service(tmp_path: Path) -> None:
    pinterest = _record(".pinterest.com", "_auth", "pinterest-secret")
    path = _store(tmp_path, _HEADER + pinterest)

    summary = NetscapeCookieManager(path).merge(
        _HEADER
        + _record(".youtube.com", "SID", "youtube-secret")
        + _record(".instagram.com", "sessionid", "instagram-secret")
        + _record(".x.com", "auth_token", "twitter-secret")
    )

    updated = path.read_bytes()
    assert pinterest in updated
    assert summary.services == (
        CookieService.YOUTUBE,
        CookieService.INSTAGRAM,
        CookieService.TWITTER,
    )
    assert summary.replaced == 0
    assert summary.added == 3


@pytest.mark.parametrize(
    ("domain", "service"),
    [
        (".google.com", CookieService.YOUTUBE),
        (".tiktok.com", CookieService.TIKTOK),
        (".pinterest.com", CookieService.PINTEREST),
        (".soundcloud.com", CookieService.SOUNDCLOUD),
    ],
)
def test_supported_cookie_domains_are_detected(
    tmp_path: Path, domain: str, service: CookieService
) -> None:
    path = _store(tmp_path, _HEADER)

    summary = NetscapeCookieManager(path).merge(_HEADER + _record(domain, "session", "secret"))

    assert summary.services == (service,)


def test_duplicate_keys_are_deduplicated_and_last_uploaded_value_wins(
    tmp_path: Path,
) -> None:
    path = _store(
        tmp_path,
        _HEADER
        + _record(".youtube.com", "SID", "old-first")
        + _record(".youtube.com", "SID", "old-duplicate"),
    )

    summary = NetscapeCookieManager(path).merge(
        _HEADER
        + _record(".youtube.com", "SID", "new-first")
        + _record(".youtube.com", "SID", "new-final")
    )

    updated = path.read_bytes()
    assert updated.count(b"\tSID\t") == 1
    assert b"new-final" in updated
    assert b"new-first" not in updated
    assert b"old-" not in updated
    assert summary.replaced == 1
    assert summary.added == 0


def test_merge_key_includes_exact_path_and_name(tmp_path: Path) -> None:
    account_path = b".youtube.com\tTRUE\t/account\tTRUE\t0\tSID\taccount-secret\n"
    path = _store(
        tmp_path,
        _HEADER + _record(".youtube.com", "SID", "root-old") + account_path,
    )

    NetscapeCookieManager(path).merge(_HEADER + _record(".youtube.com", "SID", "root-new"))

    updated = path.read_bytes()
    assert b"root-new" in updated
    assert account_path in updated


@pytest.mark.parametrize(
    "uploaded",
    [
        b"not a Netscape cookie file\n",
        _HEADER + b".youtube.com\tTRUE\t/\tFALSE\t0\tmissing-value\n",
    ],
)
def test_malformed_upload_is_rejected_without_modification(tmp_path: Path, uploaded: bytes) -> None:
    current = _HEADER + _record(".instagram.com", "sessionid", "preserved-secret")
    path = _store(tmp_path, current)

    with pytest.raises(InvalidCookieFileError):
        NetscapeCookieManager(path).merge(uploaded)

    assert path.read_bytes() == current
    assert not (tmp_path / ".cookie-backups").exists()


def test_empty_upload_is_rejected(tmp_path: Path) -> None:
    current = _HEADER + _record(".instagram.com", "sessionid", "preserved-secret")
    path = _store(tmp_path, current)

    with pytest.raises(EmptyCookieFileError):
        NetscapeCookieManager(path).merge(_HEADER)

    assert path.read_bytes() == current

    with pytest.raises(EmptyCookieFileError):
        NetscapeCookieManager(path).merge(b"")


def test_unsupported_domain_upload_is_rejected(tmp_path: Path) -> None:
    current = _HEADER + _record(".instagram.com", "sessionid", "preserved-secret")
    path = _store(tmp_path, current)

    with pytest.raises(UnsupportedCookieDomainsError):
        NetscapeCookieManager(path).merge(
            _HEADER + _record(".unsupported.example", "session", "unsupported-secret")
        )

    assert path.read_bytes() == current


def test_atomic_replace_failure_keeps_original_and_durable_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = _HEADER + _record(".youtube.com", "SID", "original-secret")
    path = _store(tmp_path, current)

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("injected atomic replacement failure")

    monkeypatch.setattr(
        "telegram_media_bot.infrastructure.cookies.manager._atomic_replace",
        fail_replace,
    )

    with pytest.raises(CookieStoreWriteError):
        NetscapeCookieManager(path).merge(_HEADER + _record(".youtube.com", "SID", "new-secret"))

    assert path.read_bytes() == current
    backups = list((tmp_path / ".cookie-backups").glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == current
    assert list(tmp_path.glob(".cookies.txt.*.tmp")) == []


def test_success_creates_exact_backup_and_preserves_file_mode(tmp_path: Path) -> None:
    current = _HEADER + _record(".youtube.com", "SID", "original-secret")
    path = _store(tmp_path, current)
    path.chmod(0o640)
    before = path.stat()

    manager = NetscapeCookieManager(path)
    manager.merge(_HEADER + _record(".youtube.com", "SID", "new-secret"))

    after = path.stat()
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    if os.name == "posix":
        assert stat.S_IMODE(after.st_mode) == 0o640
        assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    backups = list((tmp_path / ".cookie-backups").glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == current
    assert stat.S_IMODE(backups[0].stat().st_mode) == stat.S_IMODE(before.st_mode)


def test_export_returns_complete_combined_file_exactly(tmp_path: Path) -> None:
    current = (
        _HEADER
        + _record(".youtube.com", "SID", "youtube-secret")
        + _record(".instagram.com", "sessionid", "instagram-secret")
    )
    path = _store(tmp_path, current)

    assert NetscapeCookieManager(path).export_combined() == current


def _store(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "cookies.txt"
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def _record(domain: str, name: str, value: str, *, ending: str = "\n") -> bytes:
    return f"{domain}\tTRUE\t/\tTRUE\t0\t{name}\t{value}{ending}".encode()
