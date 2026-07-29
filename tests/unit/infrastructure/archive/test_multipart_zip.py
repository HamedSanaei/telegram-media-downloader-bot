from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from telegram_media_bot.bootstrap.config import MultipartSection
from telegram_media_bot.infrastructure.archive.multipart_zip import (
    MultipartZipBuilder,
    resolve_seven_zip,
)


def test_multipart_builder_creates_ordered_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "7zz.exe"
    executable.write_bytes(b"binary")
    source = tmp_path / "video.mkv"
    source.write_bytes(b"original-content")
    builder = MultipartZipBuilder(
        MultipartSection(
            seven_zip_executable=executable,
            part_size_mb=1,
            max_total_size_mb=2,
        )
    )

    class FakePopen:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            self.args = args
            self.returncode = 0

        def communicate(self, *, timeout: int) -> tuple[str, str]:
            assert timeout == 86400
            archive = Path(self.args[-2])
            archive.with_name(f"{archive.name}.001").write_bytes(b"part-one")
            archive.with_name(f"{archive.name}.002").write_bytes(b"part-two")
            return "", ""

        def poll(self) -> int:
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    result = builder.build(source)
    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))

    assert [path.suffix for path in result.volumes] == [".001", ".002"]
    assert manifest["extract_from"].endswith(".zip.001")
    assert manifest["original"]["sha256"] == hashlib.sha256(b"original-content").hexdigest()
    assert manifest["volumes"][1]["sha256"] == hashlib.sha256(b"part-two").hexdigest()


def test_multipart_builder_isolates_active_process_state(tmp_path: Path) -> None:
    executable = tmp_path / "7zz.exe"
    executable.write_bytes(b"binary")
    builder = MultipartZipBuilder(MultipartSection(seven_zip_executable=executable))

    isolated = builder.isolated()

    assert isolated is not builder
    assert isolated.executable() == builder.executable()


def test_default_7zz_name_falls_back_to_distro_7z(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "telegram_media_bot.infrastructure.archive.multipart_zip.shutil.which",
        lambda name: "/usr/bin/7z" if name == "7z" else None,
    )

    assert resolve_seven_zip(Path("7zz")) == "/usr/bin/7z"
