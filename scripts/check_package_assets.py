#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

FONT = "telegram_media_bot/assets/fonts/NotoSans-Regular.ttf"
LICENSE = "telegram_media_bot/assets/fonts/OFL.txt"
GALLERY_NOTICE = "telegram_media_bot/assets/licenses/THIRD_PARTY_NOTICES.md"
GALLERY_LICENSE = "telegram_media_bot/assets/licenses/gallery-dl-GPL-2.0.txt"


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify packaged usage-chart font assets")
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--install-smoke", action="store_true")
    parser.add_argument("--uv-executable", type=Path)
    args = parser.parse_args()
    wheel = _single(args.dist.glob("telegram_media_downloader_bot-*.whl"), "wheel")
    sdist = _single(args.dist.glob("telegram_media_downloader_bot-*.tar.gz"), "sdist")
    _check_wheel(wheel)
    _check_sdist(sdist)
    if args.install_smoke:
        _check_clean_install(wheel, args.uv_executable)
    print(f"Verified bundled font and OFL license in {wheel.name} and {sdist.name}")
    return 0


def _single(paths: object, label: str) -> Path:
    found = sorted(paths)  # type: ignore[arg-type]
    if len(found) != 1:
        raise SystemExit(f"Expected exactly one {label}, found {len(found)}")
    return found[0]


def _check_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        _require_assets(names, prefix="")
        if not archive.read(FONT):
            raise SystemExit("Bundled wheel font is empty")
        _validate_license(archive.read(LICENSE))
        _validate_gallery_assets(archive.read(GALLERY_NOTICE), archive.read(GALLERY_LICENSE))


def _check_sdist(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        names = set(archive.getnames())
        prefix = next(
            (name.removesuffix(FONT) for name in names if name.endswith(FONT)),
            None,
        )
        if prefix is None:
            raise SystemExit(f"{FONT} is missing from sdist")
        _require_assets(names, prefix=prefix, include_gallery=False)
        font = archive.extractfile(prefix + FONT)
        license_file = archive.extractfile(prefix + LICENSE)
        gallery_notice_name = next(
            (name for name in names if name.endswith("docs/THIRD_PARTY_NOTICES.md")),
            None,
        )
        gallery_license_name = next(
            (name for name in names if name.endswith("docs/licenses/gallery-dl-GPL-2.0.txt")),
            None,
        )
        gallery_notice = (
            archive.extractfile(gallery_notice_name) if gallery_notice_name is not None else None
        )
        gallery_license = (
            archive.extractfile(gallery_license_name) if gallery_license_name is not None else None
        )
        if font is None or not font.read():
            raise SystemExit("Bundled sdist font is empty")
        if license_file is None:
            raise SystemExit("Bundled sdist font license is unreadable")
        _validate_license(license_file.read())
        if gallery_notice is None or gallery_license is None:
            raise SystemExit("Bundled gallery-dl notices are unreadable")
        _validate_gallery_assets(gallery_notice.read(), gallery_license.read())


def _require_assets(names: set[str], *, prefix: str, include_gallery: bool = True) -> None:
    required = (
        (FONT, LICENSE, GALLERY_NOTICE, GALLERY_LICENSE) if include_gallery else (FONT, LICENSE)
    )
    missing = [asset for asset in required if prefix + asset not in names]
    if missing:
        raise SystemExit("Missing package assets: " + ", ".join(missing))


def _validate_license(content: bytes) -> None:
    text = content.decode("utf-8")
    if "SIL OPEN FONT LICENSE" not in text or "Version 1.1" not in text:
        raise SystemExit("Bundled font license is not the expected OFL 1.1 text")


def _validate_gallery_assets(notice_content: bytes, license_content: bytes) -> None:
    notice = notice_content.decode("utf-8")
    license_text = license_content.decode("utf-8")
    if not all(marker in notice for marker in ("gallery-dl", "1.32.8", "GPL-2.0")):
        raise SystemExit("Bundled gallery-dl notice has unexpected content")
    if "GNU GENERAL PUBLIC LICENSE" not in license_text or "Version 2" not in license_text:
        raise SystemExit("Bundled gallery-dl license is not GPL version 2")


def _check_clean_install(wheel: Path, configured_uv: Path | None) -> None:
    uv = configured_uv or (Path(found) if (found := shutil.which("uv")) else None)
    if uv is None or not uv.is_file():
        raise SystemExit("uv is required for the clean wheel installation smoke test")
    with tempfile.TemporaryDirectory(prefix="tmb-package-smoke-") as directory:
        environment = Path(directory) / "venv"
        subprocess.run([str(uv), "venv", "--python", sys.executable, str(environment)], check=True)
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.run(
            [str(uv), "pip", "install", "--python", str(python), str(wheel)],
            check=True,
        )
        code = """
from importlib.resources import files
from telegram_media_bot.infrastructure.analytics.usage_chart_renderer import validate_chart_font
font = files("telegram_media_bot.assets.fonts").joinpath("NotoSans-Regular.ttf")
license_file = files("telegram_media_bot.assets.fonts").joinpath("OFL.txt")
licenses = files("telegram_media_bot.assets.licenses")
gallery_notice = licenses.joinpath("THIRD_PARTY_NOTICES.md")
gallery_license = licenses.joinpath("gallery-dl-GPL-2.0.txt")
assert font.is_file() and len(font.read_bytes()) > 0
assert license_file.is_file() and "SIL OPEN FONT LICENSE" in license_file.read_text(encoding="utf-8")
notice_text = gallery_notice.read_text(encoding="utf-8")
assert "gallery-dl" in notice_text and "1.32.8" in notice_text and "GPL-2.0" in notice_text
assert "GNU GENERAL PUBLIC LICENSE" in gallery_license.read_text(encoding="utf-8")
validate_chart_font()
print("Clean wheel font resource smoke passed")
"""
        subprocess.run([str(python), "-c", code], check=True)


if __name__ == "__main__":
    raise SystemExit(main())
