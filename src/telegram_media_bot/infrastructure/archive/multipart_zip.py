from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from telegram_media_bot.bootstrap.config import MultipartSection
from telegram_media_bot.domain.errors import PostProcessingError


@dataclass(frozen=True, slots=True)
class MultipartArchive:
    volumes: tuple[Path, ...]
    manifest: Path


class MultipartZipBuilder:
    def __init__(self, settings: MultipartSection) -> None:
        self._settings = settings
        self._process_lock = threading.Lock()
        self._active_process: subprocess.Popen[str] | None = None

    def executable(self) -> str | None:
        return resolve_seven_zip(self._settings.seven_zip_executable)

    def isolated(self) -> MultipartZipBuilder:
        """Return a job-scoped builder so cancellation cannot affect another job."""
        return MultipartZipBuilder(self._settings)

    def build(self, source: Path) -> MultipartArchive:
        source = source.resolve()
        if not source.is_file():
            raise PostProcessingError("Multipart source file does not exist")
        maximum = self._settings.max_total_size_mb * 1024 * 1024
        if source.stat().st_size > maximum:
            raise PostProcessingError("File exceeds the configured multipart ceiling")
        executable = self.executable()
        if executable is None:
            raise PostProcessingError("Configured 7-Zip executable was not found")
        archive = source.with_name(f"{source.name}.zip")
        prefix = f"{archive.name}."
        for stale in source.parent.glob(f"{archive.name}.*"):
            stale.unlink(missing_ok=True)
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                [
                    executable,
                    "a",
                    "-tzip",
                    f"-mx={self._settings.compression_level}",
                    f"-v{self._settings.part_size_mb}m",
                    "-bd",
                    "-y",
                    str(archive),
                    str(source),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=os.name != "nt",
                creationflags=creationflags,
            )
            with self._process_lock:
                self._active_process = process
            process.communicate(timeout=86400)
        except (OSError, subprocess.SubprocessError) as exc:
            if "process" in locals():
                _terminate_process_tree(process)
            raise PostProcessingError("Unable to create multipart ZIP archive") from exc
        finally:
            with self._process_lock:
                self._active_process = None
        if process.returncode != 0:
            raise PostProcessingError("7-Zip multipart archive creation failed")
        volumes = tuple(
            sorted(
                (
                    path
                    for path in source.parent.iterdir()
                    if path.is_file() and path.name.startswith(prefix)
                ),
                key=lambda path: path.name,
            )
        )
        if not volumes:
            raise PostProcessingError("7-Zip did not create multipart volumes")
        part_limit = self._settings.part_size_mb * 1024 * 1024
        if any(path.stat().st_size > part_limit for path in volumes):
            raise PostProcessingError("A multipart volume exceeds the configured part ceiling")
        manifest = source.with_name(f"{source.name}.manifest.json")
        payload = {
            "version": 1,
            "archive_format": "zip",
            "extract_from": volumes[0].name,
            "original": {
                "name": source.name,
                "size_bytes": source.stat().st_size,
                "sha256": _sha256(source),
            },
            "volumes": [
                {
                    "ordinal": index,
                    "name": path.name,
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
                for index, path in enumerate(volumes, start=1)
            ],
        }
        manifest.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return MultipartArchive(volumes=volumes, manifest=manifest)

    def cancel_active(self) -> None:
        with self._process_lock:
            process = self._active_process
        if process is not None:
            _terminate_process_tree(process)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_seven_zip(configured_path: Path) -> str | None:
    configured = configured_path.expanduser()
    if configured.is_absolute() or configured.parent != Path("."):
        return str(configured.resolve()) if configured.is_file() else None
    names = [str(configured)]
    if configured.name == "7zz":
        names.append("7z")
    elif configured.name == "7z":
        names.append("7zz")
    for name in names:
        if resolved := shutil.which(name):
            return resolved
    return None


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name != "nt":
            kill_process_group = getattr(os, "killpg")  # noqa: B009
            kill_process_group(process.pid, signal.SIGTERM)
        else:
            process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
        process.wait(timeout=5)
    except OSError, subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                kill_process_group = getattr(os, "killpg")  # noqa: B009
                kill_process_group(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            else:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
                if process.poll() is None:
                    process.kill()
        except OSError, subprocess.TimeoutExpired:
            pass
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
