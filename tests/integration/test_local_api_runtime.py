from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from telegram_media_bot.bootstrap.config import load_settings
from telegram_media_bot.infrastructure.telegram.local_api import LocalBotApiManager
from telegram_media_bot.telegram.bot_factory import create_telegram_runtime


def _example_configuration() -> dict[str, Any]:
    raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    return cast(dict[str, Any], raw)


@pytest.mark.integration
async def test_config_migration_state_and_shared_factory_select_local_endpoint(
    tmp_path: Path,
) -> None:
    raw = _example_configuration()
    raw["telegram"]["bot_token"] = "123456:LOCAL_RUNTIME_TEST_TOKEN"
    raw["telegram"]["local_api_base_url"] = "http://127.0.0.1:18081"
    raw["telegram"]["local_api_is_local"] = True
    raw["telegram"]["max_upload_size_mb"] = 1900
    raw["telegram"]["local_bot_api"]["enabled"] = True
    raw["telegram"]["local_bot_api"]["mode"] = "external"
    raw["telegram"]["local_bot_api"]["executable"] = None
    raw["telegram"]["local_bot_api"]["api_id"] = None
    raw["telegram"]["local_bot_api"]["api_hash"] = None
    raw["media"]["max_file_size_mb"] = 1900
    raw["media"]["max_source_size_mb"] = 2000
    raw["storage"]["root_directory"] = str(tmp_path / "storage")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    settings = load_settings(config_path, require_token=True)
    LocalBotApiManager(settings).migration_store.write("local")

    runtime = create_telegram_runtime(settings, manage_lifecycle=False)
    try:
        assert runtime.endpoint == "local"
        assert runtime.settings.telegram.max_upload_size_mb == 1900
        assert runtime.bot.session.api.is_local
    finally:
        await runtime.bot.session.close()


@pytest.mark.integration
async def test_pre_migration_runtime_stays_cloud_and_caps_upload(
    tmp_path: Path,
) -> None:
    raw = _example_configuration()
    raw["telegram"]["bot_token"] = "123456:CLOUD_RUNTIME_TEST_TOKEN"
    raw["telegram"]["local_api_base_url"] = "http://127.0.0.1:18081"
    raw["telegram"]["local_api_is_local"] = True
    raw["telegram"]["max_upload_size_mb"] = 1900
    raw["telegram"]["local_bot_api"]["enabled"] = True
    raw["telegram"]["local_bot_api"]["mode"] = "external"
    raw["telegram"]["local_bot_api"]["executable"] = None
    raw["telegram"]["local_bot_api"]["api_id"] = None
    raw["telegram"]["local_bot_api"]["api_hash"] = None
    raw["media"]["max_file_size_mb"] = 1900
    raw["media"]["max_source_size_mb"] = 2000
    raw["storage"]["root_directory"] = str(tmp_path / "storage")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    settings = load_settings(config_path, require_token=True)

    runtime = create_telegram_runtime(settings, manage_lifecycle=False)
    try:
        assert runtime.endpoint == "cloud"
        assert runtime.settings.telegram.max_upload_size_mb == 50
        assert not runtime.bot.session.api.is_local
    finally:
        await runtime.bot.session.close()
