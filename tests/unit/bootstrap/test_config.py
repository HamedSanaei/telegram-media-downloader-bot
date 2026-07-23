from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from telegram_media_bot.bootstrap.config import Settings, load_settings
from telegram_media_bot.domain.errors import ConfigurationError


def test_example_configuration_is_valid() -> None:
    settings = load_settings(Path("config.example.yaml"), require_token=False)
    assert "youtube" in settings.media.enabled_sources
    assert settings.media.default_mode.value == "best"
    assert settings.media.max_source_size_mb == 1024
    assert settings.telegram.upload_timeout_seconds == 600


def test_unknown_configuration_key_is_rejected(tmp_path: Path) -> None:
    raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    raw["app"]["unknown"] = True
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_settings(path)


def test_storage_path_must_remain_under_root(settings: Settings) -> None:
    raw = settings.model_dump()
    raw["storage"]["downloads_directory"] = "/tmp/outside"
    invalid = Settings.model_validate(raw)

    with pytest.raises(ConfigurationError):
        invalid.storage.downloads_path()


def test_runtime_token_is_required_for_bot(settings: Settings) -> None:
    raw = settings.model_dump()
    raw["telegram"]["bot_token"] = "CHANGE_ME"
    invalid = Settings.model_validate(raw)
    with pytest.raises(ConfigurationError):
        invalid.validate_runtime(require_token=True)


def test_runtime_directories_are_created(settings: Settings) -> None:
    settings.create_runtime_directories()
    assert settings.storage.downloads_path().is_dir()
    assert settings.storage.temp_path().is_dir()
    assert settings.storage.state_path().is_dir()


def test_allowed_and_blocked_user_overlap_is_rejected(settings: Settings) -> None:
    raw = settings.model_dump()
    raw["security"]["allowed_user_ids"] = [1]
    raw["security"]["blocked_user_ids"] = [1]
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


def test_caption_template_rejects_unknown_fields(settings: Settings) -> None:
    raw = settings.model_dump()
    raw["telegram"]["caption_template"] = "{title} {token}"
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


def test_upload_timeout_rejects_values_below_session_floor(settings: Settings) -> None:
    raw = settings.model_dump()
    raw["telegram"]["upload_timeout_seconds"] = 59
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


def test_source_limit_must_cover_final_media_limit(settings: Settings) -> None:
    raw = settings.model_dump()
    raw["media"]["max_file_size_mb"] = 100
    raw["media"]["max_source_size_mb"] = 99
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


def test_database_filename_cannot_escape_state_directory(settings: Settings) -> None:
    raw = settings.model_dump()
    raw["persistence"]["database_filename"] = "../jobs.sqlite3"
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


def test_enabled_modes_require_best_fallback(settings: Settings) -> None:
    raw = settings.model_dump()
    raw["media"]["enabled_modes"] = ["audio_mp3"]
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)
