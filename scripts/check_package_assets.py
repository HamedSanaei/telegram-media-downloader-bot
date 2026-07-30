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


def _check_sdist(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        names = set(archive.getnames())
        prefix = next(
            (name.removesuffix(FONT) for name in names if name.endswith(FONT)),
            None,
        )
        if prefix is None:
            raise SystemExit(f"{FONT} is missing from sdist")
        _require_assets(names, prefix=prefix)
        font = archive.extractfile(prefix + FONT)
        license_file = archive.extractfile(prefix + LICENSE)
        if font is None or not font.read():
            raise SystemExit("Bundled sdist font is empty")
        if license_file is None:
            raise SystemExit("Bundled sdist font license is unreadable")
        _validate_license(license_file.read())


def _require_assets(names: set[str], *, prefix: str) -> None:
    missing = [asset for asset in (FONT, LICENSE) if prefix + asset not in names]
    if missing:
        raise SystemExit("Missing package assets: " + ", ".join(missing))


def _validate_license(content: bytes) -> None:
    text = content.decode("utf-8")
    if "SIL OPEN FONT LICENSE" not in text or "Version 1.1" not in text:
        raise SystemExit("Bundled font license is not the expected OFL 1.1 text")


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
assert font.is_file() and len(font.read_bytes()) > 0
assert license_file.is_file() and "SIL OPEN FONT LICENSE" in license_file.read_text(encoding="utf-8")
validate_chart_font()
print("Clean wheel font resource smoke passed")
"""
        subprocess.run([str(python), "-c", code], check=True)


if __name__ == "__main__":
    raise SystemExit(main())
