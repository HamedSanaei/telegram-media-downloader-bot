from contextlib import closing
from pathlib import Path

import pytest
import yaml

import telegram_media_bot.bootstrap.instagram_setup as provisioning_module
from telegram_media_bot import cli
from telegram_media_bot.application.services.credential_vault import CredentialVault
from telegram_media_bot.bootstrap.companion import load_companion_settings
from telegram_media_bot.bootstrap.config import Settings, load_settings
from telegram_media_bot.bootstrap.instagram import instagram_connection_issues
from telegram_media_bot.bootstrap.instagram_setup import (
    configure_instagram_connection,
    instagram_profile_issues,
)
from telegram_media_bot.domain.errors import ConfigurationError
from telegram_media_bot.infrastructure.credentials.key_ring import CredentialCryptor, VaultKeyRing
from telegram_media_bot.infrastructure.persistence.sqlite_instagram_credentials import (
    SqliteInstagramCredentialRepository,
)


@pytest.fixture
def config(tmp_path: Path) -> Path:
    raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    raw["storage"]["root_directory"] = str(tmp_path / "data")
    raw["telegram"]["bot_token"] = "123:BOT_TOKEN_SENTINEL"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_setup_preserves_keys_and_exports_no_bot_secrets(config: Path) -> None:
    before = config.read_bytes()
    backup = configure_instagram_connection(config, public_base_url="https://connect.example.test/")
    assert backup is not None and (backup / "config.yaml").read_bytes() == before
    settings = load_settings(config)
    assert instagram_connection_issues(settings) == ()
    profile = config.with_name("companion.local.yaml")
    assert "BOT_TOKEN_SENTINEL" not in profile.read_text(encoding="utf-8")
    assert "handoff_signing_key" not in profile.read_text(encoding="utf-8")
    reduced = load_companion_settings(profile)
    assert reduced.host == "0.0.0.0" and reduced.port == 8090
    assert reduced.database_path == settings.database_path()
    assert instagram_profile_issues(settings, profile) == ()
    assert not hasattr(reduced, "telegram")
    assert (
        configure_instagram_connection(config, public_base_url="https://connect.example.test")
        is None
    )
    assert load_settings(config).vault == settings.vault
    if __import__("os").name != "nt":
        assert config.stat().st_mode & 0o777 == profile.stat().st_mode & 0o777 == 0o600


def test_setup_does_not_invalidate_an_existing_encrypted_session(config: Path) -> None:
    configure_instagram_connection(config, public_base_url="https://connect.example.test")
    settings = load_settings(config)
    repository = SqliteInstagramCredentialRepository(settings.database_path())
    repository.initialize()
    vault = CredentialVault(repository, CredentialCryptor(VaultKeyRing.from_config(settings.vault)))
    vault.store_session(7, b"fixture-session")
    old = config.read_bytes()
    configure_instagram_connection(config, public_base_url="https://new.example.test")
    after = load_settings(config)
    assert after.vault == settings.vault
    assert after.web_companion.handoff_signing_key == settings.web_companion.handoff_signing_key
    assert config.read_bytes() != old
    assert vault.get_view(7) is not None


@pytest.mark.parametrize(
    "origin",
    [
        "http://connect.example.test",
        "https://user:secret@example.test",  # pragma: allowlist secret - rejected URL fixture
        "https://example.test/path",
        "https://example.test/?token=bad",
        "https://example.test/#fragment",
        "https://example.test:bad",
        "https://example.test:0",
        "https://example.test:65536",
        "https://example.test/\n",
        "https://example.test\\path",
    ],
)
def test_setup_rejects_unsafe_origins_before_writing(config: Path, origin: str) -> None:
    before = config.read_bytes()
    with pytest.raises(ConfigurationError):
        configure_instagram_connection(config, public_base_url=origin)
    assert config.read_bytes() == before
    assert not config.with_name("companion.local.yaml").exists()


def test_check_refuses_a_profile_with_bot_credentials(config: Path) -> None:
    configure_instagram_connection(config, public_base_url="https://connect.example.test")
    profile = config.with_name("companion.local.yaml")
    raw = yaml.safe_load(profile.read_text(encoding="utf-8"))
    raw["telegram"] = {"bot_token": "not-allowed"}
    profile.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert instagram_profile_issues(load_settings(config), profile) == (
        "restricted_companion_profile_contains_forbidden_keys",
    )


def test_setup_refuses_mismatched_keys(config: Path) -> None:
    configure_instagram_connection(config, public_base_url="https://connect.example.test")
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw["web_companion"]["handoff_verification_key"] = "not-a-public-key"
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    before = config.read_bytes()
    with pytest.raises(ConfigurationError, match="invalid"):
        configure_instagram_connection(config, public_base_url="https://connect.example.test")
    assert config.read_bytes() == before


def test_setup_restores_profile_when_main_write_fails(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = config.read_bytes()
    writer = provisioning_module._atomic_write

    def fail_main(path: Path, content: bytes, reference: Path) -> None:
        if path == config:
            raise OSError("fixture failure")
        writer(path, content, reference)

    monkeypatch.setattr(provisioning_module, "_atomic_write", fail_main)
    with pytest.raises(ConfigurationError, match="preserved"):
        configure_instagram_connection(config, public_base_url="https://connect.example.test")
    assert config.read_bytes() == before
    assert not config.with_name("companion.local.yaml").exists()
    assert list((config.parent / "backups").glob("instagram-setup-*/config.yaml"))


def test_setup_refuses_fresh_key_when_encrypted_credentials_exist(config: Path) -> None:
    from sqlite3 import connect

    settings = Settings.model_validate(yaml.safe_load(config.read_text(encoding="utf-8")))
    settings.database_path().parent.mkdir(parents=True)
    with closing(connect(settings.database_path())) as db:
        db.execute("CREATE TABLE instagram_credentials(envelope_json TEXT)")
        db.execute("INSERT INTO instagram_credentials VALUES ('encrypted-fixture')")
        db.commit()
    before = config.read_bytes()
    with pytest.raises(ConfigurationError, match="restore the original"):
        configure_instagram_connection(config, public_base_url="https://connect.example.test")
    assert config.read_bytes() == before


def test_cli_setup_and_check_are_explicit_and_sanitized(
    config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    monkeypatch.setattr(
        sys, "argv", ["telegram-media-bot", "instagram-setup", "--config", str(config), "--check"]
    )
    before = config.read_bytes()
    with pytest.raises(SystemExit) as outcome:
        cli.main()
    assert outcome.value.code == 2
    assert config.read_bytes() == before
    output = capsys.readouterr().out
    assert "configuration_ready: false" in output
    assert "BOT_TOKEN_SENTINEL" not in output

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "telegram-media-bot",
            "instagram-setup",
            "--config",
            str(config),
            "--public-base-url",
            "https://connect.example.test",
        ],
    )
    cli.main()
    output = capsys.readouterr().out
    settings = load_settings(config)
    assert settings.vault.active_key is not None
    assert settings.vault.active_key.get_secret_value() not in output
    assert "BOT_TOKEN_SENTINEL" not in output

    monkeypatch.setattr(
        sys, "argv", ["telegram-media-bot", "instagram-setup", "--config", str(config), "--check"]
    )
    cli.main()
    assert capsys.readouterr().out.strip() == "configuration_ready: true"


def test_companion_compose_has_restricted_mounts_and_loopback_ingress() -> None:
    compose = yaml.safe_load(Path("docker-compose.companion.yml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {"companion"}
    service = compose["services"]["companion"]
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["ports"] == ["127.0.0.1:8090:8090"]
    mounts = service["volumes"]
    assert {mount["source"] for mount in mounts} == {"./companion.local.yaml", "./data/state"}
    assert mounts[0]["read_only"] is True
    assert all(mount["bind"]["create_host_path"] is False for mount in mounts)
