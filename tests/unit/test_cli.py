from __future__ import annotations

import sys

import pytest

from telegram_media_bot import cli


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
