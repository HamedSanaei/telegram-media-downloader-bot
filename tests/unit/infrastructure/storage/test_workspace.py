from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.models import JobId, JobStatus
from telegram_media_bot.infrastructure.storage.workspace import (
    cleanup_job_workspace,
    sweep_workspaces,
)


@dataclass(frozen=True)
class StubJob:
    status: JobStatus


class StubRepository:
    def __init__(self, jobs: dict[str, StubJob]) -> None:
        self.jobs = jobs

    def get_job(self, job_id: JobId) -> StubJob | None:
        return self.jobs.get(str(job_id))


def _settings(settings: Settings, tmp_path: Path) -> Settings:
    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
    configured = Settings.model_validate(raw)
    configured.create_runtime_directories()
    return configured


def test_cleanup_removes_only_the_exact_job_workspaces(
    settings: Settings,
    tmp_path: Path,
) -> None:
    configured = _settings(settings, tmp_path)
    roots = (configured.storage.downloads_path(), configured.storage.temp_path())
    expected_bytes = 0
    for root in roots:
        (root / ".gitkeep").touch()
        job = root / "job-1"
        nested = job / "nested"
        nested.mkdir(parents=True)
        for name in (
            "video.mp4",
            "audio.webm",
            "partial.part",
            "state.ytdl",
            "metadata.info.json",
            "archive.zip.001",
            "thumbnail.jpg",
            "temporary.telegram.mp4",
        ):
            payload = name.encode()
            (nested / name).write_bytes(payload)
            expected_bytes += len(payload)
        other = root / "job-2"
        other.mkdir()
        (other / "keep.mp4").write_bytes(b"other")

    report = cleanup_job_workspace(
        configured,
        "job-1",
        terminal_status=JobStatus.SUCCEEDED.value,
        cleanup_reason="test",
    )

    assert report.files_deleted == 16
    assert report.directories_deleted == 4
    assert report.bytes_reclaimed == expected_bytes
    assert report.failed_paths_count == 0
    assert all(not (root / "job-1").exists() for root in roots)
    assert all((root / "job-2" / "keep.mp4").read_bytes() == b"other" for root in roots)
    assert all((root / ".gitkeep").exists() for root in roots)


def test_cleanup_is_idempotent(settings: Settings, tmp_path: Path) -> None:
    configured = _settings(settings, tmp_path)

    first = cleanup_job_workspace(
        configured,
        "missing-job",
        terminal_status=JobStatus.FAILED.value,
        cleanup_reason="test",
    )
    second = cleanup_job_workspace(
        configured,
        "missing-job",
        terminal_status=JobStatus.FAILED.value,
        cleanup_reason="test",
    )

    assert first.failed_paths_count == 0
    assert second.failed_paths_count == 0
    assert second.files_deleted == 0
    assert second.directories_deleted == 0


def test_cleanup_rejects_path_traversal(settings: Settings, tmp_path: Path) -> None:
    configured = _settings(settings, tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    report = cleanup_job_workspace(
        configured,
        f"../{outside.name}",
        terminal_status=JobStatus.FAILED.value,
        cleanup_reason="test",
    )

    assert report.failed_paths_count == 1
    assert marker.read_text(encoding="utf-8") == "keep"


def test_cleanup_unlinks_symlink_without_following_it(
    settings: Settings,
    tmp_path: Path,
) -> None:
    configured = _settings(settings, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    job = configured.storage.downloads_path() / "symlink-job"
    job.mkdir()
    try:
        (job / "external").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    report = cleanup_job_workspace(
        configured,
        "symlink-job",
        terminal_status=JobStatus.CANCELLED.value,
        cleanup_reason="test",
    )

    assert report.failed_paths_count == 0
    assert marker.read_text(encoding="utf-8") == "keep"
    assert not job.exists()


def test_sweeper_removes_terminal_and_old_orphans_but_preserves_active_and_young(
    settings: Settings,
    tmp_path: Path,
) -> None:
    configured = _settings(settings, tmp_path)
    root = configured.storage.downloads_path()
    for name in ("terminal", "active", "old-orphan", "young-orphan"):
        path = root / name
        path.mkdir()
        (path / "media.mp4").write_bytes(b"x")
    old_timestamp = datetime.now(UTC).timestamp() - 600
    os.utime(root / "old-orphan", (old_timestamp, old_timestamp))
    repository = StubRepository(
        {
            "terminal": StubJob(JobStatus.FAILED),
            "active": StubJob(JobStatus.RUNNING),
        }
    )

    report = sweep_workspaces(
        configured,
        repository=cast(Any, repository),
        now=datetime.now(UTC),
        cleanup_reason="test",
    )

    assert report.failed_paths_count == 0
    assert not (root / "terminal").exists()
    assert not (root / "old-orphan").exists()
    assert (root / "active").exists()
    assert (root / "young-orphan").exists()
