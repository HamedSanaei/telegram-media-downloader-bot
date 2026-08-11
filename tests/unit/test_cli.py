from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml

from telegram_media_bot import cli
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import ConfigurationError


def test_config_check_does_not_print_configuration_or_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    raw["yt_dlp"]["cookies_file"] = None
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["telegram-media-bot", "config-check", "--config", str(config)],
    )

    cli.main()

    captured = capsys.readouterr()
    assert captured.out == "Configuration is valid.\n"
    assert "CHANGE_ME" not in captured.out
    assert captured.err == ""


def test_config_check_rejects_unreadable_explicit_gallery_cookie(
    settings: Settings, tmp_path: Path
) -> None:
    raw = settings.model_dump()
    raw["yt_dlp"]["cookies_file"] = None
    raw["gallery_dl"]["cookies"]["twitter"] = str(tmp_path / "missing.txt")

    with pytest.raises(ConfigurationError, match="gallery_dl_cookie_twitter"):
        cli._run_config_check(Settings.model_validate(raw))


def test_local_api_status_parser_does_not_require_migration_confirmation_flag() -> None:
    args = cli.build_parser().parse_args(["local-api", "status"])

    assert args.command == "local-api"
    assert args.local_api_action == "status"
    assert not hasattr(args, "yes")


@pytest.mark.parametrize(
    "arguments",
    [
        ["local-api", "--config", "custom.yaml", "status"],
        ["local-api", "status", "--config", "custom.yaml"],
    ],
)
def test_local_api_config_path_is_accepted_before_or_after_action(
    arguments: list[str],
) -> None:
    args = cli.build_parser().parse_args(arguments)

    assert args.config == Path("custom.yaml")


@pytest.mark.parametrize("removed_command", ["uploader", "mtproto"])
def test_removed_premium_commands_are_rejected(removed_command: str) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([removed_command])


async def test_local_api_status_is_safe(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    await cli._run_local_api(settings, "status", False)

    output = capsys.readouterr().out
    assert "migration_phase: cloud" in output
    assert settings.telegram.token() not in output
    assert str(settings.telegram.local_bot_api.migration.state_file) not in output


def test_local_api_serve_and_configure_commands_are_explicit() -> None:
    serve = cli.build_parser().parse_args(["local-api", "serve"])
    configure = cli.build_parser().parse_args(["configure", "--config", "custom.yaml"])
    assert serve.local_api_action == "serve"
    assert configure.config == Path("custom.yaml")


def test_cleanup_workspace_parser_supports_dry_run() -> None:
    args = cli.build_parser().parse_args(
        ["cleanup-workspaces", "--config", "custom.yaml", "--dry-run"]
    )

    assert args.command == "cleanup-workspaces"
    assert args.config == Path("custom.yaml")
    assert args.dry_run is True


def test_interactive_configure_never_prints_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "config.yaml"
    shutil.copyfile("config.example.yaml", target)
    hidden = iter(
        [
            "123456:BOT_TOKEN_SECRET",
            "API_HASH_SECRET",
            "",
        ]
    )
    visible = iter(["12345", "", "n", ""])
    monkeypatch.setattr(cli, "getpass", lambda _prompt: next(hidden))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(visible))

    cli._run_interactive_configure(target)

    output = capsys.readouterr().out
    assert "BOT_TOKEN_SECRET" not in output
    assert "API_HASH_SECRET" not in output
    assert "BOT_TOKEN_SECRET" in target.read_text(encoding="utf-8")


def test_interactive_configure_removes_secret_temporary_file_on_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "config.yaml"
    shutil.copyfile("config.example.yaml", target)
    hidden = iter(["123456:BOT_TOKEN_SECRET", "API_HASH_SECRET", "not-a-proxy"])
    visible = iter(["12345", "", "n", ""])
    monkeypatch.setattr(cli, "getpass", lambda _prompt: next(hidden))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(visible))

    with pytest.raises(ConfigurationError):
        cli._run_interactive_configure(target)

    assert not target.with_suffix(".yaml.tmp").exists()
    output = capsys.readouterr()
    assert "BOT_TOKEN_SECRET" not in output.out + output.err
    assert "API_HASH_SECRET" not in output.out + output.err
