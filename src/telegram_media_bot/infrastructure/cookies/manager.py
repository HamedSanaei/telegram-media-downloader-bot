from __future__ import annotations

import os
import stat
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from telegram_media_bot.application.services.diagnostic_sanitizer import sanitize_exception_message
from telegram_media_bot.domain.cookie_health import CookieHealthState, StaticCookieCheck
from telegram_media_bot.domain.cookies import (
    MAX_COOKIE_UPLOAD_BYTES,
    CookieService,
    CookieUpdateSummary,
    cookie_provider_domains,
    cookie_service_for_domain,
)
from telegram_media_bot.domain.errors import (
    CookieFileTooLargeError,
    CookieManagementError,
    CookieStoreUnavailableError,
    CookieStoreWriteError,
    EmptyCookieFileError,
    InvalidCookieFileError,
    UnsupportedCookieDomainsError,
)

_MAX_COMBINED_COOKIE_BYTES = 8 * 1024 * 1024
_HEADER = "# Netscape HTTP Cookie File"
_HTTP_ONLY_PREFIX = "#HttpOnly_"


@dataclass(frozen=True, slots=True)
class _CookieRecord:
    domain: str
    path: str
    name: str
    service: CookieService | None
    content: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.domain, self.path, self.name)


@dataclass(frozen=True, slots=True)
class _CookieLine:
    raw: str
    content: str
    ending: str
    record: _CookieRecord | None


class NetscapeCookieManager:
    """Merge supported-service cookies into one canonical Netscape file."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve()
        self._lock = threading.Lock()

    def merge(self, uploaded: bytes) -> CookieUpdateSummary:
        if len(uploaded) > MAX_COOKIE_UPLOAD_BYTES:
            raise CookieFileTooLargeError("uploaded cookie file exceeds the safe limit")
        with self._lock:
            try:
                current, metadata = self._read_store_snapshot()
                try:
                    current_lines = _parse_document(current, upload=False)
                except InvalidCookieFileError as exc:
                    raise CookieStoreUnavailableError(
                        "canonical cookie file is not valid Netscape data"
                    ) from exc
                uploaded_lines = _parse_document(uploaded, upload=True)
                merged, summary = _merge_documents(current_lines, uploaded_lines)
                backup = self._create_atomic_backup(metadata)
                replacement_completed = False
                try:
                    self._replace_atomically(merged, metadata)
                    replacement_completed = True
                    self._verify_replacement(
                        expected=merged,
                        original_metadata=metadata,
                        uploaded_lines=uploaded_lines,
                        summary=summary,
                    )
                except Exception as exc:
                    # The canonical path is unchanged until os.replace succeeds. Keep the durable
                    # backup for operator recovery and restore it if post-write verification fails.
                    if replacement_completed and self._path.exists():
                        self._restore_backup(backup, metadata)
                    if isinstance(exc, CookieManagementError):
                        raise
                    raise CookieStoreWriteError("cookie post-write verification failed") from exc
                _fsync_directory(backup.parent)
                return summary
            except CookieManagementError:
                raise
            except OSError as exc:
                raise CookieStoreWriteError("cookie store update failed") from exc

    def export_combined(self) -> bytes:
        with self._lock:
            return self._read_store_snapshot()[0]

    def static_health(
        self,
        provider: CookieService,
        *,
        now: datetime | None = None,
        expiring_soon_hours: float = 24,
    ) -> StaticCookieCheck:
        """Network-free static validation for one provider (Cookie Health Center)."""
        checked_at = now or datetime.now(UTC)
        try:
            content, metadata = self._read_store_snapshot()
        except CookieManagementError as exc:
            if isinstance(exc, CookieStoreUnavailableError) and not self._path.exists():
                return StaticCookieCheck(
                    provider=provider,
                    status=CookieHealthState.MISSING,
                    file_ok=False,
                    safe_reason="canonical cookie file is missing",
                )
            return StaticCookieCheck(
                provider=provider,
                status=CookieHealthState.CHECK_ERROR,
                file_ok=False,
                safe_reason=sanitize_exception_message(str(exc)) or "cookie store unavailable",
            )
        permission_ok = not _world_writable(metadata.st_mode)
        try:
            lines = _parse_document(content, upload=False)
        except InvalidCookieFileError as exc:
            return StaticCookieCheck(
                provider=provider,
                status=CookieHealthState.MALFORMED,
                file_ok=True,
                malformed_record_count=0,
                safe_reason=sanitize_exception_message(str(exc)) or "cookie file is malformed",
                permission_ok=permission_ok,
            )
        matching_records = 0
        persistent_expiries: list[int] = []
        session_records = 0
        malformed = 0
        domains = cookie_provider_domains(provider)
        for line in lines:
            record = line.record
            if record is None:
                continue
            if not _matches_provider_domain(record.domain, domains):
                continue
            matching_records += 1
            try:
                expires = int(record.content.split("\t")[4])
            except IndexError, ValueError:
                malformed += 1
                continue
            if expires == 0:
                # The record exists, but a static check cannot prove its lifetime or auth state.
                session_records += 1
                continue
            persistent_expiries.append(expires)
        if malformed and matching_records == malformed:
            return StaticCookieCheck(
                provider=provider,
                status=CookieHealthState.MALFORMED,
                file_ok=True,
                malformed_record_count=malformed,
                safe_reason="provider cookie records are malformed",
                permission_ok=permission_ok,
            )
        if matching_records == 0:
            return StaticCookieCheck(
                provider=provider,
                status=CookieHealthState.MISSING,
                file_ok=True,
                malformed_record_count=malformed,
                safe_reason="no provider-domain cookie records found",
                permission_ok=permission_ok,
            )
        if not persistent_expiries:
            return StaticCookieCheck(
                provider=provider,
                status=CookieHealthState.UNVERIFIED,
                file_ok=True,
                record_count=matching_records,
                malformed_record_count=malformed,
                safe_reason="provider cookies exist but have no persistent expiry",
                permission_ok=permission_ok,
            )
        now_epoch = int(checked_at.timestamp())
        usable_expiries = sorted(expiry for expiry in persistent_expiries if expiry > now_epoch)
        if not usable_expiries:
            status = CookieHealthState.UNVERIFIED if session_records else CookieHealthState.EXPIRED
            return StaticCookieCheck(
                provider=provider,
                status=status,
                file_ok=True,
                record_count=matching_records,
                earliest_expiry=_expiry_datetime(min(persistent_expiries)),
                latest_expiry=_expiry_datetime(max(persistent_expiries)),
                malformed_record_count=malformed,
                safe_reason=(
                    "provider cookies exist but only session records may remain usable"
                    if session_records
                    else "no usable persistent provider cookies remain"
                ),
                permission_ok=permission_ok,
            )
        earliest = _expiry_datetime(usable_expiries[0])
        latest = _expiry_datetime(usable_expiries[-1])
        earliest_epoch = int(earliest.timestamp()) if earliest is not None else None
        if (
            earliest_epoch is not None
            and earliest_epoch - checked_at.timestamp() <= expiring_soon_hours * 3600
        ):
            status = CookieHealthState.EXPIRING_SOON
        else:
            status = CookieHealthState.HEALTHY
        return StaticCookieCheck(
            provider=provider,
            status=status,
            file_ok=True,
            record_count=matching_records,
            earliest_expiry=earliest,
            latest_expiry=latest,
            malformed_record_count=malformed,
            safe_reason=None,
            permission_ok=permission_ok,
        )

    def _verify_replacement(
        self,
        *,
        expected: bytes,
        original_metadata: os.stat_result,
        uploaded_lines: list[_CookieLine],
        summary: CookieUpdateSummary,
    ) -> None:
        actual, metadata = self._read_store_snapshot()
        if actual != expected:
            raise CookieStoreWriteError("canonical cookie bytes failed verification")
        if stat.S_IMODE(metadata.st_mode) != stat.S_IMODE(original_metadata.st_mode):
            raise CookieStoreWriteError("canonical cookie mode changed during update")
        if os.name == "posix" and (
            metadata.st_uid != original_metadata.st_uid
            or metadata.st_gid != original_metadata.st_gid
        ):
            raise CookieStoreWriteError("canonical cookie ownership changed during update")
        actual_lines = _parse_document(actual, upload=False)
        actual_records = {
            line.record.key: line.record
            for line in actual_lines
            if line.record is not None and line.record.service in summary.services
        }
        uploaded_records = {
            line.record.key: line.record
            for line in uploaded_lines
            if line.record is not None and line.record.service in summary.services
        }
        if any(
            key not in actual_records or actual_records[key].content != record.content
            for key, record in uploaded_records.items()
        ):
            raise CookieStoreWriteError("uploaded cookie identities failed verification")
        if any(summary.record_count(provider) <= 0 for provider in summary.services):
            raise CookieStoreWriteError("detected provider has no canonical cookie records")

    def _restore_backup(self, backup: Path, original_metadata: os.stat_result) -> None:
        try:
            current_metadata = self._path.stat(follow_symlinks=False)
            self._replace_atomically(
                backup.read_bytes(),
                original_metadata,
                expected_metadata=current_metadata,
            )
        except OSError as exc:
            raise CookieStoreWriteError("cookie rollback failed") from exc

    def _read_store_snapshot(self) -> tuple[bytes, os.stat_result]:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._path, flags)
        except OSError as exc:
            raise CookieStoreUnavailableError("canonical cookie file is unavailable") from exc
        try:
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise CookieStoreUnavailableError("canonical cookie path is not a regular file")
                if metadata.st_size > _MAX_COMBINED_COOKIE_BYTES:
                    raise CookieStoreUnavailableError(
                        "canonical cookie file exceeds the safe limit"
                    )
                content = stream.read(_MAX_COMBINED_COOKIE_BYTES + 1)
                if len(content) > _MAX_COMBINED_COOKIE_BYTES:
                    raise CookieStoreUnavailableError(
                        "canonical cookie file exceeds the safe limit"
                    )
            path_metadata = self._path.lstat()
            if self._path.is_symlink() or not _same_snapshot(metadata, path_metadata):
                raise CookieStoreUnavailableError("canonical cookie path changed during read")
            return content, metadata
        except CookieManagementError:
            raise
        except OSError as exc:
            raise CookieStoreUnavailableError("canonical cookie file is unreadable") from exc

    def _create_atomic_backup(self, metadata: os.stat_result) -> Path:
        backup_directory = self._path.parent / ".cookie-backups"
        if backup_directory.exists():
            backup_metadata = backup_directory.lstat()
            if not stat.S_ISDIR(backup_metadata.st_mode) or backup_directory.is_symlink():
                raise CookieStoreWriteError("cookie backup path is unsafe")
        else:
            backup_directory.mkdir(mode=0o700)
        backup_directory.chmod(0o700)
        backup = backup_directory / (f"{self._path.name}.{time.time_ns()}.{uuid.uuid4().hex}.bak")
        try:
            os.link(self._path, backup, follow_symlinks=False)
            if not _same_snapshot(metadata, backup.stat(follow_symlinks=False)):
                raise CookieStoreWriteError("canonical cookie path changed before backup")
            _preserve_owner(backup, metadata)
            backup.chmod(stat.S_IMODE(metadata.st_mode))
            _fsync_directory(backup_directory)
        except CookieStoreWriteError:
            backup.unlink(missing_ok=True)
            raise
        except OSError as exc:
            backup.unlink(missing_ok=True)
            raise CookieStoreWriteError("atomic cookie backup failed") from exc
        return backup

    def _replace_atomically(
        self,
        content: bytes,
        metadata: os.stat_result,
        *,
        expected_metadata: os.stat_result | None = None,
    ) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=self._path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            _preserve_owner(temporary, metadata)
            temporary.chmod(stat.S_IMODE(metadata.st_mode))
            current_metadata = self._path.stat(follow_symlinks=False)
            if self._path.is_symlink() or not _same_snapshot(
                expected_metadata or metadata, current_metadata
            ):
                raise CookieStoreWriteError("canonical cookie path changed before replacement")
            _atomic_replace(temporary, self._path)
            _fsync_directory(self._path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def _parse_document(data: bytes, *, upload: bool) -> list[_CookieLine]:
    if upload and len(data) > MAX_COOKIE_UPLOAD_BYTES:
        raise CookieFileTooLargeError("uploaded cookie file exceeds the safe limit")
    if upload and not data.strip():
        raise EmptyCookieFileError("cookie upload is empty")
    if b"\x00" in data:
        raise InvalidCookieFileError("cookie file contains a null byte")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidCookieFileError("cookie file is not valid UTF-8") from exc
    chunks = text.splitlines(keepends=True)
    if text and not chunks:
        chunks = [text]
    lines: list[_CookieLine] = []
    header_found = False
    first_nonempty_seen = False
    records = 0
    unsupported = False
    for chunk in chunks:
        content, ending = _split_line_ending(chunk)
        if content and not first_nonempty_seen:
            first_nonempty_seen = True
            header_found = content.removeprefix("\ufeff").startswith(_HEADER)
        record = _parse_record(content)
        if record is not None:
            records += 1
            unsupported = unsupported or record.service is None
        lines.append(_CookieLine(raw=chunk, content=content, ending=ending, record=record))
    if not header_found:
        raise InvalidCookieFileError("Netscape cookie header is missing")
    if upload and records == 0:
        raise EmptyCookieFileError("cookie upload contains no records")
    if upload and unsupported:
        raise UnsupportedCookieDomainsError("cookie upload contains an unsupported domain")
    return lines


def _parse_record(content: str) -> _CookieRecord | None:
    if not content or (content.startswith("#") and not content.startswith(_HTTP_ONLY_PREFIX)):
        return None
    fields = content.split("\t")
    if len(fields) != 7:
        raise InvalidCookieFileError("cookie record must contain seven tab-separated fields")
    raw_domain, include_subdomains, path, secure, expires, name, _value = fields
    domain = raw_domain.removeprefix(_HTTP_ONLY_PREFIX)
    normalized_domain = domain.casefold()
    if (
        not normalized_domain
        or "://" in normalized_domain
        or "/" in normalized_domain
        or any(character.isspace() for character in normalized_domain)
    ):
        raise InvalidCookieFileError("cookie domain is invalid")
    if include_subdomains.casefold() not in {"true", "false"}:
        raise InvalidCookieFileError("cookie include-subdomains flag is invalid")
    if secure.casefold() not in {"true", "false"}:
        raise InvalidCookieFileError("cookie secure flag is invalid")
    if not path.startswith("/") or any(ord(character) < 32 for character in path):
        raise InvalidCookieFileError("cookie path is invalid")
    if not name or any(ord(character) < 32 for character in name):
        raise InvalidCookieFileError("cookie name is invalid")
    try:
        if int(expires) < 0:
            raise ValueError
    except ValueError as exc:
        raise InvalidCookieFileError("cookie expiry is invalid") from exc
    return _CookieRecord(
        domain=normalized_domain,
        path=path,
        name=name,
        service=cookie_service_for_domain(normalized_domain),
        content=content,
    )


def _merge_documents(
    current_lines: list[_CookieLine], uploaded_lines: list[_CookieLine]
) -> tuple[bytes, CookieUpdateSummary]:
    uploaded_records: dict[tuple[str, str, str], _CookieRecord] = {}
    upload_order: list[tuple[str, str, str]] = []
    detected: set[CookieService] = set()
    for line in uploaded_lines:
        record = line.record
        if record is None or record.service is None:
            continue
        detected.add(record.service)
        if record.key not in uploaded_records:
            upload_order.append(record.key)
        uploaded_records[record.key] = record

    existing_keys = {
        line.record.key
        for line in current_lines
        if line.record is not None and line.record.service in detected
    }
    seen_detected: set[tuple[str, str, str]] = set()
    output: list[str] = []
    default_ending = next((line.ending for line in current_lines if line.ending), "\n")
    for line in current_lines:
        record = line.record
        if record is None or record.service not in detected:
            output.append(line.raw)
            continue
        if record.key in seen_detected:
            continue
        seen_detected.add(record.key)
        replacement = uploaded_records.get(record.key)
        if replacement is None:
            output.append(line.raw)
        else:
            output.append(replacement.content + line.ending)

    for key in upload_order:
        if key in existing_keys:
            continue
        if output and not output[-1].endswith(("\n", "\r")):
            output.append(default_ending)
        output.append(uploaded_records[key].content + default_ending)

    services = tuple(service for service in CookieService if service in detected)
    merged_lines = _parse_document("".join(output).encode("utf-8"), upload=False)
    provider_counts = tuple(
        (
            service,
            sum(
                line.record is not None and line.record.service is service for line in merged_lines
            ),
        )
        for service in services
    )
    current_record_count = sum(line.record is not None for line in current_lines)
    new_record_count = sum(line.record is not None for line in merged_lines)
    summary = CookieUpdateSummary(
        services=services,
        replaced=sum(key in existing_keys for key in upload_order),
        added=sum(key not in existing_keys for key in upload_order),
        uploaded_record_count=len(upload_order),
        previous_canonical_record_count=current_record_count,
        new_canonical_record_count=new_record_count,
        preserved_other_provider_count=sum(
            line.record is not None and line.record.service not in detected
            for line in current_lines
        ),
        provider_record_counts=provider_counts,
    )
    return "".join(output).encode("utf-8"), summary


def _split_line_ending(chunk: str) -> tuple[str, str]:
    if chunk.endswith("\r\n"):
        return chunk[:-2], "\r\n"
    if chunk.endswith(("\n", "\r")):
        return chunk[:-1], chunk[-1]
    return chunk, ""


def _matches_provider_domain(domain: str, provider_domains: tuple[str, ...]) -> bool:
    hostname = domain.lstrip(".").casefold()
    return any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in provider_domains)


def _expiry_datetime(epoch_seconds: int) -> datetime | None:
    try:
        return datetime.fromtimestamp(epoch_seconds, tz=UTC)
    except OverflowError, OSError, ValueError:
        return None


def _world_writable(mode: int) -> bool:
    if os.name != "posix":
        return False
    return bool(stat.S_IMODE(mode) & (stat.S_IWGRP | stat.S_IWOTH))


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _preserve_owner(path: Path, metadata: os.stat_result) -> None:
    chown = getattr(os, "chown", None)
    if os.name == "posix" and chown is not None:
        chown(path, metadata.st_uid, metadata.st_gid, follow_symlinks=False)


def _atomic_replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _same_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_size,
        left.st_mtime_ns,
        stat.S_IMODE(left.st_mode),
        left.st_uid,
        left.st_gid,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_size,
        right.st_mtime_ns,
        stat.S_IMODE(right.st_mode),
        right.st_uid,
        right.st_gid,
    )
