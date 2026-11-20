from __future__ import annotations

import json
from pathlib import Path
from time import time

import pytest
import yaml

from telegram_media_bot import cli
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.infrastructure.persistence.sqlite_instagram_admission import (
    SqliteInstagramAdmissionRepository,
)
from telegram_media_bot.infrastructure.security.instagram_admission import RedisInstagramPermitLock


def test_status_and_review_are_offline_explicit_and_preserve_history(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = settings.model_dump(mode="json")
    # Do not serialize a masked SecretStr token back into the strict settings model.
    raw["telegram"]["bot_token"] = "123456:TEST_TOKEN_FOR_UNIT_TESTS"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    store = SqliteInstagramAdmissionRepository(settings.database_path())
    store.initialize()
    identity = "a" * 64
    now = time()
    assert store.admit(
        identity,
        "operator",
        "crashed",
        now=now,
        spacing=30,
        burst_window=3600,
        burst_limit=6,
        daily_limit=30,
    ).allowed
    store.pause(identity, reason="challenge_required", until=None)
    monkeypatch.setattr(
        "sys.argv", ["telegram-media-bot", "instagram-admission-status", "--config", str(path)]
    )
    cli.main()
    status = json.loads(capsys.readouterr().out)
    assert status["credentials"][0]["reason"] == "challenge_required"
    assert status["credentials"][0]["active"]
    assert status["credentials"][0]["daily_operations"] == 1
    cleared: list[str] = []
    monkeypatch.setattr(
        RedisInstagramPermitLock, "clear_reviewed", lambda self, key: cleared.append(key)
    )
    command = ["telegram-media-bot", "instagram-admission-review", identity, "--config", str(path)]
    monkeypatch.setattr("sys.argv", command)
    with pytest.raises(SystemExit) as missing_flag:
        cli.main()
    assert missing_flag.value.code == 2 and not cleared
    capsys.readouterr()
    monkeypatch.setattr("sys.argv", [*command, "--workers-stopped"])
    cli.main()
    assert cleared == [identity]
    (state,) = store.snapshot(now=time(), burst_window=3600)
    assert not state.active and state.reason == "ready" and state.daily_operations == 1
    store.pause(identity, reason="rate_limited", until=time() + 10000)
    with pytest.raises(SystemExit) as early:
        cli.main()
    assert early.value.code == 2 and cleared == [identity]
    capsys.readouterr()
    # A missing/malformed canonical file must not hide the durable pause history.
    raw["yt_dlp"]["cookies_file"] = str(tmp_path / "missing.txt")
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv", ["telegram-media-bot", "instagram-admission-status", "--config", str(path)]
    )
    cli.main()
    unavailable = json.loads(capsys.readouterr().out)
    assert unavailable["current_operator_identity"] is None
    assert unavailable["identity_state"] == "credential_identity_unavailable"
    assert unavailable["credentials"][0]["reason"] == "rate_limited"
