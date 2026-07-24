from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from telegram_media_bot.bootstrap.config import MultipartSection
from telegram_media_bot.infrastructure.archive.multipart_zip import MultipartZipBuilder


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

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        archive = Path(args[-2])
        archive.with_name(f"{archive.name}.001").write_bytes(b"part-one")
        archive.with_name(f"{archive.name}.002").write_bytes(b"part-two")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = builder.build(source)
    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))

    assert [path.suffix for path in result.volumes] == [".001", ".002"]
    assert manifest["extract_from"].endswith(".zip.001")
    assert manifest["original"]["sha256"] == hashlib.sha256(b"original-content").hexdigest()
    assert manifest["volumes"][1]["sha256"] == hashlib.sha256(b"part-two").hexdigest()
