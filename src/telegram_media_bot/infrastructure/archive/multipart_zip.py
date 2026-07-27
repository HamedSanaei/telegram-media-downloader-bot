from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
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

    def executable(self) -> str | None:
        return resolve_seven_zip(self._settings.seven_zip_executable)

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
        try:
            completed = subprocess.run(
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
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=86400,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PostProcessingError("Unable to create multipart ZIP archive") from exc
        if completed.returncode != 0:
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
