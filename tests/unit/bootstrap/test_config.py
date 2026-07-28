from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from telegram_media_bot.bootstrap.config import Settings, load_settings
from telegram_media_bot.domain.errors import ConfigurationError
from telegram_media_bot.domain.models import Mp4NativeFallback


def test_example_configuration_is_valid() -> None:
    settings = load_settings(Path("config.example.yaml"), require_token=False)
    assert "youtube" in settings.media.enabled_sources
    assert settings.media.default_mode.value == "best"
    assert settings.media.max_source_size_mb == 1024
    assert settings.telegram.upload_timeout_seconds == 14400
    assert settings.telegram.upload_chunk_size_kb == 1024
    assert settings.telegram.upload_heartbeat_interval_seconds == 30
    assert not settings.telegram.local_bot_api.enabled
    assert "CHANGE_ME" not in repr(settings.telegram.bot_token)


def test_v1_0_0_configuration_remains_valid_without_manual_rewrite() -> None:
    settings = load_settings(
        Path("tests/fixtures/config-v1.0.0.yaml"),
        require_token=False,
    )

    assert settings.media.instagram.force_mp4 is True
    assert settings.telegram.upload_as_document is True
    assert settings.media.formats.best_original == "bv*+ba/b"
    assert settings.media.transcode.enabled
    assert settings.media.transcode.threads == 2
    assert settings.media.transcode.max_concurrent == 1
    assert settings.media.transcode.timeout_seconds == 1500
    assert not settings.media.transcode.explicit_mp4_enabled
    assert settings.media.mp4_native_fallback is Mp4NativeFallback.LOWER_RESOLUTION


def test_v1_0_1_configuration_remains_valid_without_manual_rewrite() -> None:
    raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    raw["media"].pop("transcode")

    settings = Settings.model_validate(raw)

    assert settings.media.transcode.enabled
    assert settings.media.transcode.threads == 2
    assert settings.media.transcode.max_concurrent == 1
    assert settings.media.transcode.timeout_seconds == 1500
    assert settings.media.transcode.progress_interval_seconds == 10
    assert not settings.media.transcode.explicit_mp4_enabled
    assert settings.media.mp4_native_fallback is Mp4NativeFallback.LOWER_RESOLUTION


def test_v1_0_2_configuration_remains_valid_without_manual_rewrite() -> None:
    raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    raw["media"].pop("mp4_native_fallback")
    raw["media"]["transcode"].pop("explicit_mp4_enabled")

    settings = Settings.model_validate(raw)

    assert settings.media.transcode.threads == 2
    assert settings.media.transcode.max_concurrent == 1
    assert settings.media.transcode.timeout_seconds == 1500


def test_v1_0_3_configuration_remains_valid_without_manual_rewrite() -> None:
    raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    raw["media"].pop("mp4_native_fallback")
    raw["media"]["transcode"].pop("explicit_mp4_enabled")

    settings = Settings.model_validate(raw)

    assert settings.media.mp4_native_fallback is Mp4NativeFallback.LOWER_RESOLUTION
    assert not settings.media.transcode.explicit_mp4_enabled


def test_unknown_configuration_key_is_rejected(tmp_path: Path) -> None:
    raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    raw["app"]["unknown"] = True
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_settings(path)


def test_retired_premium_uploader_configuration_is_rejected() -> None:
    raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    raw["telegram"]["premium_uploader"] = {"enabled": False}

    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


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


def test_required_channels_reject_duplicates_and_invalid_join_url(
    settings: Settings,
) -> None:
    raw = settings.model_dump()
    raw["telegram"]["required_channels"] = {
        "enabled": True,
        "channels": [
            {
                "chat_id": -1001,
                "title": "One",
                "join_url": "https://t.me/one",
            },
            {
                "chat_id": -1001,
                "title": "Two",
                "join_url": "http://example.com/two",
            },
        ],
    }
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


@pytest.mark.parametrize(
    "proxy",
    [
        "http://127.0.0.1:8080",
        "https://127.0.0.1:8080",
        "socks4://127.0.0.1:1080",
        "socks4a://127.0.0.1:1080",
        "socks5://127.0.0.1:1080",
        "socks5h://127.0.0.1:1080",
    ],
)
def test_supported_proxy_schemes_are_valid(settings: Settings, proxy: str) -> None:
    raw = settings.model_dump()
    raw["yt_dlp"]["proxy_enabled"] = True
    raw["yt_dlp"]["proxy"] = proxy
    configured = Settings.model_validate(raw)
    assert configured.yt_dlp.effective_proxy() == proxy


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


@pytest.mark.parametrize(
    ("multipart_enabled", "part_size_mb"),
    [(False, 49), (True, 50)],
)
def test_oversized_media_requires_usable_multipart_route(
    settings: Settings,
    multipart_enabled: bool,
    part_size_mb: int,
) -> None:
    raw = settings.model_dump()
    raw["media"]["max_file_size_mb"] = 100
    raw["media"]["max_source_size_mb"] = 100
    raw["multipart"]["enabled"] = multipart_enabled
    raw["multipart"]["part_size_mb"] = part_size_mb
    configured = Settings.model_validate(raw)

    with pytest.raises(ConfigurationError):
        configured.validate_runtime(require_token=False)


def test_local_api_paths_are_resolved_relative_to_config_file(tmp_path: Path) -> None:
    raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    raw["multipart"]["seven_zip_executable"] = "./tools/7zz"
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
    assert (
        configured.multipart.seven_zip_executable
        == (config_path.parent / "tools" / "7zz").resolve()
    )


def test_invalid_yaml_error_does_not_echo_secret_source_line(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text('telegram:\n  bot_token: "DO_NOT_ECHO"\n  broken: [\n', encoding="utf-8")

    with pytest.raises(ConfigurationError) as captured:
        load_settings(path)

    assert "DO_NOT_ECHO" not in str(captured.value)
