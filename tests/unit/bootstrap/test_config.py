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
    assert not settings.telegram.local_bot_api.enabled
    assert "CHANGE_ME" not in repr(settings.telegram.bot_token)


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


def test_managed_local_api_requires_its_own_credentials(settings: Settings) -> None:
    raw = settings.model_dump()
    raw["telegram"]["local_api_base_url"] = "http://127.0.0.1:8081"
    raw["telegram"]["local_api_is_local"] = True
    raw["telegram"]["local_bot_api"]["enabled"] = True
    raw["telegram"]["local_bot_api"]["mode"] = "managed"
    raw["telegram"]["local_bot_api"]["executable"] = None
    raw["telegram"]["local_bot_api"]["api_id"] = None
    raw["telegram"]["local_bot_api"]["api_hash"] = None

    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


def test_external_local_api_does_not_require_api_id_or_hash(settings: Settings) -> None:
    raw = settings.model_dump()
    raw["telegram"]["local_api_base_url"] = "http://127.0.0.1:8081"
    raw["telegram"]["local_api_is_local"] = True
    raw["telegram"]["max_upload_size_mb"] = 1900
    raw["telegram"]["local_bot_api"]["enabled"] = True
    raw["telegram"]["local_bot_api"]["mode"] = "external"
    raw["telegram"]["local_bot_api"]["executable"] = None
    raw["telegram"]["local_bot_api"]["api_id"] = None
    raw["telegram"]["local_bot_api"]["api_hash"] = None

    configured = Settings.model_validate(raw)

    assert configured.telegram.max_upload_size_mb == 1900


def test_local_api_paths_are_resolved_relative_to_config_file(tmp_path: Path) -> None:
    raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    config_path = tmp_path / "nested" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    configured = load_settings(config_path)

    assert (
        configured.telegram.local_bot_api.working_directory
        == (config_path.parent / "data" / "telegram-bot-api").resolve()
    )
    assert (
        configured.telegram.local_bot_api.migration.state_file
        == (config_path.parent / "data" / "state" / "telegram-api-migration.json").resolve()
    )


def test_invalid_yaml_error_does_not_echo_secret_source_line(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text('telegram:\n  bot_token: "DO_NOT_ECHO"\n  broken: [\n', encoding="utf-8")

    with pytest.raises(ConfigurationError) as captured:
        load_settings(path)

    assert "DO_NOT_ECHO" not in str(captured.value)
