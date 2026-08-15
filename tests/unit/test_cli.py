from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from telegram_media_bot import cli
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import ConfigurationError
from telegram_media_bot.domain.models import ComponentHealth
from telegram_media_bot.infrastructure.analytics import usage_chart_doctor
from telegram_media_bot.infrastructure.gallerydl.adapter import GalleryDlEngine
from telegram_media_bot.infrastructure.telegram.local_api import LocalBotApiManager
from telegram_media_bot.infrastructure.ytdlp.engine import YtDlpEngine


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


def test_config_check_accepts_legacy_instagram_cookie_fallback_without_gallery_section(
    settings: Settings,
    tmp_path: Path,
) -> None:
    cookie = tmp_path / "cookies.txt"
    cookie.write_bytes(b"fixture-cookie")
    raw = settings.model_dump()
    raw.pop("gallery_dl")
    raw["yt_dlp"]["cookies_file"] = str(cookie)

    cli._run_config_check(Settings.model_validate(raw))


def test_config_check_rejects_missing_legacy_instagram_cookie_without_gallery_section(
    settings: Settings,
    tmp_path: Path,
) -> None:
    raw = settings.model_dump()
    raw.pop("gallery_dl")
    raw["yt_dlp"]["cookies_file"] = str(tmp_path / "missing.txt")

    with pytest.raises(ConfigurationError, match="gallery_dl_cookie_instagram"):
        cli._run_config_check(Settings.model_validate(raw))


def test_config_check_rejects_existing_but_unreadable_gallery_cookie(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cookie = tmp_path / "cookies.txt"
    cookie.write_bytes(b"fixture-cookie")
    raw = settings.model_dump()
    raw["yt_dlp"]["cookies_file"] = None
    raw["gallery_dl"]["cookies"]["instagram"] = str(cookie)
    original_access = os.access
    monkeypatch.setattr(
        os,
        "access",
        lambda path, mode: False if Path(path) == cookie else original_access(path, mode),
    )

    with pytest.raises(ConfigurationError, match="gallery_dl_cookie_instagram"):
        cli._run_config_check(Settings.model_validate(raw))


def test_config_check_validates_ytdlp_cookie_when_gallery_is_disabled(
    settings: Settings,
    tmp_path: Path,
) -> None:
    raw = settings.model_dump()
    raw["yt_dlp"]["cookies_file"] = str(tmp_path / "missing.txt")
    raw["gallery_dl"]["enabled"] = False

    with pytest.raises(ConfigurationError, match="yt_dlp_cookie"):
        cli._run_config_check(Settings.model_validate(raw))


def test_config_check_accepts_readable_explicit_instagram_cookie(
    settings: Settings,
    tmp_path: Path,
) -> None:
    cookie = tmp_path / "instagram.txt"
    cookie.write_bytes(b"fixture-cookie")
    raw = settings.model_dump()
    raw["yt_dlp"]["cookies_file"] = None
    raw["gallery_dl"]["cookies"]["instagram"] = str(cookie)

    cli._run_config_check(Settings.model_validate(raw))


def test_doctor_and_config_check_probe_the_same_canonical_runtime_cookie(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cookie = (tmp_path / "combined.txt").resolve()
    cookie.write_bytes(b"fixture-cookie")
    raw = settings.model_dump()
    raw["yt_dlp"]["cookies_file"] = str(cookie)
    raw["gallery_dl"]["cookies"] = {
        "instagram": str(cookie),
        "tiktok": str(cookie),
        "twitter": str(cookie),
        "pinterest": str(cookie),
    }
    configured = Settings.model_validate(raw)
    config_check_paths: list[Path | None] = []
    doctor_paths: list[Path | None] = []

    def record_config_check(path: Path | None) -> bool:
        config_check_paths.append(path)
        return True

    def record_doctor(path: Path | None) -> bool:
        doctor_paths.append(path)
        return True

    monkeypatch.setattr(
        GalleryDlEngine,
        "health",
        lambda _self: ComponentHealth("gallery-dl", True, "1.32.8"),
    )
    monkeypatch.setattr(
        YtDlpEngine,
        "health",
        lambda _self: ComponentHealth("yt-dlp", True, "fixture"),
    )
    monkeypatch.setattr(usage_chart_doctor, "check_usage_chart_runtime", lambda: {})
    monkeypatch.setattr(shutil, "which", lambda _name: "fixture-tool")
    monkeypatch.setattr(cli, "_binary_version", lambda _path: "fixture")
    monkeypatch.setattr(cli, "resolve_seven_zip", lambda _path: "fixture-7z")
    monkeypatch.setattr(
        cli,
        "_cookie_file_readable",
        record_config_check,
    )

    cli._run_config_check(configured)

    monkeypatch.setattr(
        cli,
        "_cookie_file_readable",
        record_doctor,
    )
    cli._run_doctor(configured)

    expected = [cookie] * 5
    assert config_check_paths == expected
    assert doctor_paths == expected
    output = capsys.readouterr().out
    assert "OK   yt_dlp_cookie" in output
    for source in ("instagram", "tiktok", "twitter", "pinterest"):
        assert f"OK   gallery_dl_cookie_{source}" in output


def test_read_only_config_check_uses_non_mutating_directory_validation(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = settings.model_dump()
    raw["yt_dlp"]["cookies_file"] = None
    raw["telegram"]["local_api_base_url"] = "http://local-api:8081"
    raw["telegram"]["local_api_is_local"] = True
    raw["telegram"]["local_bot_api"]["enabled"] = True
    raw["telegram"]["local_bot_api"]["mode"] = "external"
    configured = Settings.model_validate(raw)
    monkeypatch.setattr(
        cli,
        "_directory_writable",
        lambda _path: pytest.fail("read-only config check attempted a write probe"),
    )
    monkeypatch.setattr(cli, "_directory_readable", lambda _path: True)

    cli._run_config_check(configured, runtime_filesystem_read_only=True)


def test_local_api_status_parser_does_not_require_migration_confirmation_flag() -> None:
    args = cli.build_parser().parse_args(["local-api", "status"])

    assert args.command == "local-api"
    assert args.local_api_action == "status"
    assert not hasattr(args, "yes")


def test_config_check_parser_supports_read_only_runtime() -> None:
    args = cli.build_parser().parse_args(
        ["config-check", "--config", "custom.yaml", "--read-only-runtime"]
    )

    assert args.config == Path("custom.yaml")
    assert args.read_only_runtime is True


def test_doctor_parser_supports_explicit_offline_and_online_phases() -> None:
    offline = cli.build_parser().parse_args(
        [
            "doctor",
            "--config",
            "custom.yaml",
            "--offline",
            "--expected-version",
            "1.3.2",
            "--read-only-runtime",
        ]
    )
    online = cli.build_parser().parse_args(
        [
            "doctor",
            "--config",
            "custom.yaml",
            "--online-service",
            "bot",
            "--online-service",
            "local-api",
        ]
    )

    assert offline.offline is True
    assert offline.expected_version == "1.3.2"
    assert offline.read_only_runtime is True
    assert online.offline is False
    assert online.online_service == ["bot", "local-api"]


def test_offline_doctor_never_calls_live_service_checks(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "_run_static_doctor_checks",
        lambda _settings, **_kwargs: False,
    )
    monkeypatch.setattr(
        cli,
        "_run_default_live_doctor_checks",
        lambda _settings: pytest.fail("offline doctor ran default live checks"),
    )
    monkeypatch.setattr(
        cli,
        "_run_selected_live_doctor_checks",
        lambda _settings, _services: pytest.fail("offline doctor ran selected live checks"),
    )

    cli._run_doctor(
        settings,
        mode=cli.DoctorMode.OFFLINE,
        expected_version="1.3.2",
        runtime_filesystem_read_only=True,
    )

    output = capsys.readouterr().out
    assert "local_api_reachable" not in output
    assert "required_channels" not in output
    assert "bot_reachable" not in output


def test_default_doctor_retains_static_and_live_checks(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def static_check(_settings: Settings, **_kwargs: object) -> bool:
        calls.append("static")
        return False

    def live_check(_settings: Settings) -> bool:
        calls.append("live")
        return False

    monkeypatch.setattr(
        cli,
        "_run_static_doctor_checks",
        static_check,
    )
    monkeypatch.setattr(
        cli,
        "_run_default_live_doctor_checks",
        live_check,
    )
    monkeypatch.setattr(
        cli,
        "_run_selected_live_doctor_checks",
        lambda _settings, _services: pytest.fail("default doctor used selected online checks"),
    )

    cli._run_doctor(settings)

    assert calls == ["static", "live"]


def test_offline_doctor_fails_closed_on_expected_package_version(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        GalleryDlEngine,
        "health",
        lambda _self: ComponentHealth("gallery_dl", True, "1.32.8"),
    )
    monkeypatch.setattr(
        YtDlpEngine,
        "health",
        lambda _self: ComponentHealth("yt_dlp", True, "fixture"),
    )
    monkeypatch.setattr(usage_chart_doctor, "check_usage_chart_runtime", lambda: {})
    monkeypatch.setattr(shutil, "which", lambda _name: "fixture-tool")
    monkeypatch.setattr(cli, "_binary_version", lambda _path: "fixture")
    monkeypatch.setattr(cli, "resolve_seven_zip", lambda _path: "fixture-7z")

    with pytest.raises(SystemExit) as raised:
        cli._run_doctor(
            settings,
            mode=cli.DoctorMode.OFFLINE,
            expected_version="999.0.0",
        )

    assert raised.value.code == 1
    assert "FAIL package:" in capsys.readouterr().out


def test_online_doctor_runs_only_selected_restored_service_checks(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = settings.model_dump()
    raw["telegram"]["local_api_base_url"] = "http://local-api:8081"
    raw["telegram"]["local_api_is_local"] = True
    raw["telegram"]["local_bot_api"]["enabled"] = True
    raw["telegram"]["local_bot_api"]["mode"] = "external"
    raw["telegram"]["required_channels"]["enabled"] = True
    configured = Settings.model_validate(raw)
    monkeypatch.setattr(
        cli,
        "_run_static_doctor_checks",
        lambda _settings, **_kwargs: pytest.fail("online doctor ran static checks"),
    )
    monkeypatch.setattr(LocalBotApiManager, "endpoint_reachable", lambda _self: True)

    async def healthy_bot(_settings: Settings) -> dict[str, bool]:
        return {"bot_reachable": True, "required_channels": True}

    monkeypatch.setattr(cli, "_bot_online_diagnostics", healthy_bot)

    cli._run_doctor(
        configured,
        mode=cli.DoctorMode.ONLINE,
        online_services=(
            cli.DoctorOnlineService.BOT,
            cli.DoctorOnlineService.LOCAL_API,
        ),
    )

    output = capsys.readouterr().out
    assert "OK   bot_reachable" in output
    assert "OK   required_channels" in output
    assert "OK   local_api_configured" in output
    assert "OK   local_api_reachable" in output
    assert "yt_dlp" not in output
    assert "ffmpeg" not in output


def test_online_bot_only_does_not_probe_stopped_local_api(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        LocalBotApiManager,
        "endpoint_reachable",
        lambda _self: pytest.fail("bot-only online doctor probed Local API"),
    )

    async def healthy_bot(_settings: Settings) -> dict[str, bool]:
        return {"bot_reachable": True, "required_channels": True}

    monkeypatch.setattr(cli, "_bot_online_diagnostics", healthy_bot)

    cli._run_doctor(
        settings,
        mode=cli.DoctorMode.ONLINE,
        online_services=(cli.DoctorOnlineService.BOT,),
    )

    output = capsys.readouterr().out
    assert "OK   bot_reachable" in output
    assert "local_api_reachable" not in output


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
