"""Companion settings least-privilege tests (T016)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from telegram_media_bot.bootstrap.companion import (
    CompanionSettings,
    build_companion_app,
    load_companion_settings,
)
from telegram_media_bot.domain.errors import ConfigurationError


def _write_config(
    path: Path, *, enabled: bool = False, verification_key: str | None = None
) -> None:
    section: dict[str, object] = {"enabled": enabled, "host": "127.0.0.1", "port": 8090}
    if verification_key is not None:
        section["handoff_verification_key"] = verification_key
    config = {
        "storage": {"root_directory": str(path), "state_directory": "state"},
        "persistence": {"database_filename": "jobs.sqlite3"},
        "web_companion": section,
        # Deliberately present: the companion must ignore the bot token and signing key.
        "telegram": {"bot_token": "123:SUPER_SECRET_TOKEN"},
    }
    (path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def test_load_companion_settings_excludes_bot_token(tmp_path: Path) -> None:
    _write_config(tmp_path)
    settings = load_companion_settings(tmp_path / "config.yaml")
    assert not settings.enabled
    assert not hasattr(settings, "bot_token")
    assert not hasattr(settings, "telegram")
    assert not hasattr(settings, "handoff_signing_key")
    assert settings.database_path is not None
    assert settings.database_path.name == "jobs.sqlite3"


def test_companion_model_forbids_extra_keys() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CompanionSettings.model_validate({"bot_token": "x"})


def test_enabled_requires_verification_key(tmp_path: Path) -> None:
    _write_config(tmp_path, enabled=True)
    with pytest.raises(ValidationError):
        CompanionSettings.model_validate(
            {
                "enabled": True,
                "handoff_verification_key": None,
            }
        )


def test_enabled_does_not_require_signing_key() -> None:
    # A companion enabled with only the verification key (and no signing key) is valid.
    settings = CompanionSettings.model_validate(
        {
            "enabled": True,
            "handoff_verification_key": "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----",
        }
    )
    assert settings.enabled
    assert not hasattr(settings, "handoff_signing_key")


def test_build_companion_app_refuses_when_disabled(tmp_path: Path) -> None:
    _write_config(tmp_path)
    settings = load_companion_settings(tmp_path / "config.yaml")
    with pytest.raises(ConfigurationError):
        build_companion_app(settings)


def test_build_companion_app_rejects_bad_key(tmp_path: Path) -> None:
    _write_config(tmp_path, enabled=True, verification_key="not-a-key")
    settings = load_companion_settings(tmp_path / "config.yaml")
    with pytest.raises(ConfigurationError):
        build_companion_app(settings)
