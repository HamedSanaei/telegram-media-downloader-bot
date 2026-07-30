from __future__ import annotations

import tomllib
from pathlib import Path


def test_hatchling_bundles_font_and_license_in_wheel_and_sdist() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    font = "src/telegram_media_bot/assets/fonts/NotoSans-Regular.ttf"
    license_file = "src/telegram_media_bot/assets/fonts/OFL.txt"
    wheel = project["tool"]["hatch"]["build"]["targets"]["wheel"]
    sdist = project["tool"]["hatch"]["build"]["targets"]["sdist"]

    assert wheel["force-include"][font] == "telegram_media_bot/assets/fonts/NotoSans-Regular.ttf"
    assert wheel["force-include"][license_file] == "telegram_media_bot/assets/fonts/OFL.txt"
    assert sdist["force-include"][font] == font
    assert sdist["force-include"][license_file] == license_file


def test_chart_fix_has_no_system_font_or_runtime_download_dependency() -> None:
    renderer = Path(
        "src/telegram_media_bot/infrastructure/analytics/usage_chart_renderer.py"
    ).read_text(encoding="utf-8")
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "importlib.resources" in renderer
    assert "ImageFont.truetype" in renderer
    assert "ImageFont.load_default" not in renderer
    assert "fonts-" not in dockerfile
    assert "/usr/share/fonts" not in renderer
