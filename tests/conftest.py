import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from telegram_media_bot.bootstrap.config import Settings

if importlib.util.find_spec("yt_dlp") is None:
    stub = ModuleType("yt_dlp")

    class MissingYoutubeDL:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("yt-dlp is not installed in this test environment")

    stub.YoutubeDL = MissingYoutubeDL  # type: ignore[attr-defined]
    sys.modules["yt_dlp"] = stub


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    raw["telegram"]["bot_token"] = "123456:TEST_TOKEN_FOR_UNIT_TESTS"
    raw["storage"]["root_directory"] = str(tmp_path)
    raw["yt_dlp"]["cookies_file"] = None
    return Settings.model_validate(raw)
