from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic

import structlog

from telegram_media_bot.application.ports.job_repository import JobRepository
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.models import JobId

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WorkspaceCleanupReport:
    files_deleted: int = 0
    directories_deleted: int = 0
    bytes_reclaimed: int = 0
    failed_paths_count: int = 0
    duration_seconds: float = 0.0

    def __add__(self, other: WorkspaceCleanupReport) -> WorkspaceCleanupReport:
        return WorkspaceCleanupReport(
            files_deleted=self.files_deleted + other.files_deleted,
            directories_deleted=self.directories_deleted + other.directories_deleted,
            bytes_reclaimed=self.bytes_reclaimed + other.bytes_reclaimed,
            failed_paths_count=self.failed_paths_count + other.failed_paths_count,
            duration_seconds=self.duration_seconds + other.duration_seconds,
        )


def cleanup_job_workspace(
    settings: Settings,
    job_id: JobId | str,
    *,
    terminal_status: str,
    cleanup_reason: str,
) -> WorkspaceCleanupReport:
    started = monotonic()
    identifier = str(job_id)
    logger.info(
        "job_workspace_cleanup_started",
        job_id=identifier,
        terminal_status=terminal_status,
        cleanup_reason=cleanup_reason,
    )
    if not _valid_job_id(identifier):
        report = WorkspaceCleanupReport(
            failed_paths_count=1,
            duration_seconds=monotonic() - started,
        )
        _log_cleanup_failure(identifier, terminal_status, cleanup_reason, report)
        return report

    report = WorkspaceCleanupReport()
    for root in (settings.storage.downloads_path(), settings.storage.temp_path()):
        try:
            report += _remove_exact_child(
                root,
                identifier,
                terminal_status=terminal_status,
                cleanup_reason=cleanup_reason,
            )
        except OSError:
            report += WorkspaceCleanupReport(failed_paths_count=1)
    report = WorkspaceCleanupReport(
        files_deleted=report.files_deleted,
        directories_deleted=report.directories_deleted,
        bytes_reclaimed=report.bytes_reclaimed,
        failed_paths_count=report.failed_paths_count,
        duration_seconds=monotonic() - started,
    )
    event = (
        "job_workspace_cleanup_failed"
        if report.failed_paths_count
        else "job_workspace_cleanup_completed"
    )
    getattr(logger, "warning" if report.failed_paths_count else "info")(
        event,
        job_id=identifier,
        terminal_status=terminal_status,
        cleanup_reason=cleanup_reason,
        files_deleted=report.files_deleted,
        directories_deleted=report.directories_deleted,
        bytes_reclaimed=report.bytes_reclaimed,
        duration_seconds=round(report.duration_seconds, 6),
        failed_paths_count=report.failed_paths_count,
    )
    return report


def sweep_workspaces(
    settings: Settings,
    repository: JobRepository,
    now: datetime,
    *,
    cleanup_reason: str,
    dry_run: bool = False,
) -> WorkspaceCleanupReport:
    started = monotonic()
    cutoff = now.timestamp() - settings.storage.orphan_grace_seconds
    identifiers: set[str] = set()
    for root in (settings.storage.downloads_path(), settings.storage.temp_path()):
        for child in root.iterdir():
            if child.name == ".gitkeep" or not (child.is_dir() or child.is_symlink()):
                continue
            record = repository.get_job(JobId(child.name))
            if record is not None and not record.status.terminal:
                continue
            if record is None and child.lstat().st_mtime > cutoff:
                continue
            identifiers.add(child.name)

    report = WorkspaceCleanupReport()
    for identifier in sorted(identifiers):
        record = repository.get_job(JobId(identifier))
        if dry_run:
            report += _measure_job_workspace(settings, identifier)
        else:
            report += cleanup_job_workspace(
                settings,
                identifier,
                terminal_status=record.status.value if record is not None else "orphan",
                cleanup_reason=cleanup_reason,
            )
    report = WorkspaceCleanupReport(
        files_deleted=report.files_deleted,
        directories_deleted=report.directories_deleted,
        bytes_reclaimed=report.bytes_reclaimed,
        failed_paths_count=report.failed_paths_count,
        duration_seconds=monotonic() - started,
    )
    logger.info(
        "orphan_workspace_cleanup_completed",
        cleanup_reason=cleanup_reason,
        files_deleted=report.files_deleted,
        directories_deleted=report.directories_deleted,
        bytes_reclaimed=report.bytes_reclaimed,
        duration_seconds=round(report.duration_seconds, 6),
        failed_paths_count=report.failed_paths_count,
        dry_run=dry_run,
    )
    return report


def _measure_job_workspace(settings: Settings, identifier: str) -> WorkspaceCleanupReport:
    if not _valid_job_id(identifier):
        return WorkspaceCleanupReport(failed_paths_count=1)
    report = WorkspaceCleanupReport()
    for root in (settings.storage.downloads_path(), settings.storage.temp_path()):
        target = root / identifier
        if target.is_symlink():
            report += WorkspaceCleanupReport(directories_deleted=1)
        elif target.is_dir():
            report += _measure_tree(target)
    return report


def _measure_tree(directory: Path) -> WorkspaceCleanupReport:
    report = WorkspaceCleanupReport(directories_deleted=1)
    try:
        entries = tuple(os.scandir(directory))
    except OSError:
        return WorkspaceCleanupReport(failed_paths_count=1)
    for entry in entries:
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError:
            report += WorkspaceCleanupReport(failed_paths_count=1)
            continue
        if stat.S_ISDIR(metadata.st_mode) and not entry.is_symlink():
            report += _measure_tree(Path(entry.path))
        else:
            report += WorkspaceCleanupReport(
                files_deleted=0 if entry.is_symlink() else 1,
                directories_deleted=1 if entry.is_symlink() else 0,
                bytes_reclaimed=metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0,
            )
    return report


def _remove_exact_child(
    root: Path,
    identifier: str,
    *,
    terminal_status: str,
    cleanup_reason: str,
) -> WorkspaceCleanupReport:
    resolved_root = root.resolve(strict=True)
    target = root / identifier
    if target.parent.resolve(strict=True) != resolved_root:
        return WorkspaceCleanupReport(failed_paths_count=1)
    if target.is_symlink():
        return _unlink_entry(
            target,
            identifier,
            terminal_status=terminal_status,
            cleanup_reason=cleanup_reason,
            directory=True,
        )
    if not target.exists():
        return WorkspaceCleanupReport()
    resolved_target = target.resolve(strict=True)
    if resolved_target == resolved_root or not resolved_target.is_relative_to(resolved_root):
        return WorkspaceCleanupReport(failed_paths_count=1)
    return _remove_tree(
        target,
        identifier,
        terminal_status=terminal_status,
        cleanup_reason=cleanup_reason,
    )


def _remove_tree(
    directory: Path,
    job_id: str,
    *,
    terminal_status: str,
    cleanup_reason: str,
) -> WorkspaceCleanupReport:
    report = WorkspaceCleanupReport()
    try:
        entries = tuple(os.scandir(directory))
    except FileNotFoundError:
        return report
    except OSError:
        return WorkspaceCleanupReport(failed_paths_count=1)
    for entry in entries:
        path = Path(entry.path)
        try:
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode) and not entry.is_symlink():
                report += _remove_tree(
                    path,
                    job_id,
                    terminal_status=terminal_status,
                    cleanup_reason=cleanup_reason,
                )
            else:
                report += _unlink_entry(
                    path,
                    job_id,
                    terminal_status=terminal_status,
                    cleanup_reason=cleanup_reason,
                    directory=False,
                )
        except FileNotFoundError:
            continue
        except OSError:
            report += WorkspaceCleanupReport(failed_paths_count=1)
    try:
        directory.rmdir()
        report += WorkspaceCleanupReport(directories_deleted=1)
    except FileNotFoundError:
        pass
    except OSError:
        report += WorkspaceCleanupReport(failed_paths_count=1)
    return report


def _unlink_entry(
    path: Path,
    job_id: str,
    *,
    terminal_status: str,
    cleanup_reason: str,
    directory: bool,
) -> WorkspaceCleanupReport:
    try:
        metadata = path.lstat()
        size = metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0
        path.unlink()
    except FileNotFoundError:
        return WorkspaceCleanupReport()
    except OSError:
        return WorkspaceCleanupReport(failed_paths_count=1)
    logger.info(
        "job_workspace_file_deleted",
        job_id=job_id,
        terminal_status=terminal_status,
        cleanup_reason=cleanup_reason,
        bytes_reclaimed=size,
        entry_kind="directory_symlink" if directory else "file",
    )
    return WorkspaceCleanupReport(
        files_deleted=0 if directory else 1,
        directories_deleted=1 if directory else 0,
        bytes_reclaimed=size,
    )


def _valid_job_id(identifier: str) -> bool:
    return bool(
        identifier
        and identifier not in {".", ".."}
        and "/" not in identifier
        and "\\" not in identifier
        and "\x00" not in identifier
    )


def _log_cleanup_failure(
    job_id: str,
    terminal_status: str,
    cleanup_reason: str,
    report: WorkspaceCleanupReport,
) -> None:
    logger.warning(
        "job_workspace_cleanup_failed",
        job_id=job_id,
        terminal_status=terminal_status,
        cleanup_reason=cleanup_reason,
        files_deleted=report.files_deleted,
        directories_deleted=report.directories_deleted,
        bytes_reclaimed=report.bytes_reclaimed,
        duration_seconds=round(report.duration_seconds, 6),
        failed_paths_count=report.failed_paths_count,
    )
