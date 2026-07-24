from __future__ import annotations

import sys
from pathlib import Path

import pytest

from telegram_media_bot import cli
from telegram_media_bot.bootstrap.config import Settings


def test_config_check_does_not_print_configuration_or_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["telegram-media-bot", "config-check", "--config", "config.example.yaml"],
    )

    cli.main()

    captured = capsys.readouterr()
    assert captured.out == "Configuration is valid.\n"
    assert "CHANGE_ME" not in captured.out
    assert captured.err == ""


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
