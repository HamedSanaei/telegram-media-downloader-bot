from __future__ import annotations

import argparse
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
from telegram_media_bot.infrastructure.persistence.sqlite_audit import SqliteAuditRepository
from telegram_media_bot.infrastructure.telegram.local_api import LocalBotApiManager
from telegram_media_bot.infrastructure.ytdlp.engine import YtDlpEngine


def test_logger_doctor_uses_safe_aggregate_durable_state(
    settings: Settings, tmp_path: Path
) -> None:
    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
    raw["telegram"]["logger"].update(
        {
            "enabled": True,
            "channels": [-1001234567890],
            "alerts_enabled": True,
        }
    )
    configured = Settings.model_validate(raw)
    missing_healthy, missing_detail = cli._logger_doctor_health(
        configured, runtime_filesystem_read_only=False
    )
    assert not missing_healthy
    assert missing_detail == "enabled;durable_state=missing"

    repository = SqliteAuditRepository(configured.database_path())
    repository.initialize()
    repository.reconcile_config(configured.telegram.logger.channels)
    healthy, detail = cli._logger_doctor_health(configured, runtime_filesystem_read_only=False)

    assert healthy
    assert "effective=1" in detail
    assert "active=1" in detail
    assert "alerts=1" in detail
    assert "-1001234567890" not in detail


def _readonly_logger_settings(settings: Settings, tmp_path: Path) -> Settings:
    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
    raw["telegram"]["logger"].update(
        {
            "enabled": True,
            "channels": [-1001234567890],
            "alerts_enabled": True,
        }
    )
    return Settings.model_validate(raw)


def test_read_only_logger_doctor_defers_to_filesystem_check(
    settings: Settings, tmp_path: Path
) -> None:
    configured = _readonly_logger_settings(settings, tmp_path)
    database = configured.database_path()
    repository = SqliteAuditRepository(database)
    repository.initialize()
    repository.reconcile_config(configured.telegram.logger.channels)
    # Simulate the cleanly-closed WAL state seen on the read-only bind mount:
    # a real sqlite open would need to create -shm/-wal and fail (SQLITE_CANTOPEN).
    wal = database.with_suffix(database.suffix + "-wal")
    shm = database.with_suffix(database.suffix + "-shm")
    wal.unlink(missing_ok=True)
    shm.unlink(missing_ok=True)

    healthy, detail = cli._logger_doctor_health(configured, runtime_filesystem_read_only=True)

    assert healthy
    assert detail == "enabled;durable_state=deferred-readonly"
    # No new files and no byte/metadata mutation.
    assert not wal.exists()
    assert not shm.exists()
    stat_before = database.stat()
    assert (stat_before.st_size, stat_before.st_mtime_ns) == (
        database.stat().st_size,
        database.stat().st_mtime_ns,
    )


def test_read_only_logger_doctor_fails_safely_when_database_missing(
    settings: Settings, tmp_path: Path
) -> None:
    configured = _readonly_logger_settings(settings, tmp_path)
    healthy, detail = cli._logger_doctor_health(configured, runtime_filesystem_read_only=True)
    assert not healthy
    assert detail == "enabled;durable_state=missing"


def test_read_only_logger_doctor_never_opens_sqlite(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = _readonly_logger_settings(settings, tmp_path)
    database = configured.database_path()
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b"SQLite format 3\x00" + b"\x00" * 15)

    def _fail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("read-only preflight must not open the SQLite database")

    monkeypatch.setattr(
        "telegram_media_bot.infrastructure.persistence.sqlite_audit.SqliteAuditRepository.health_snapshot",
        _fail,
    )
    healthy, detail = cli._logger_doctor_health(configured, runtime_filesystem_read_only=True)
    assert healthy
    assert detail == "enabled;durable_state=deferred-readonly"


def test_logger_doctor_disabled_never_checks_database(settings: Settings, tmp_path: Path) -> None:
    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
    raw["telegram"]["logger"]["enabled"] = False
    configured = Settings.model_validate(raw)
    healthy, detail = cli._logger_doctor_health(configured, runtime_filesystem_read_only=True)
    assert healthy
    assert detail == "disabled"


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


@pytest.mark.parametrize(
    ("installed", "package_version", "expected", "healthy"),
    [
        # The RC package installs under its normalized PEP 440 form (`1.4.0rc1`), while
        # pyproject/py __version__ carry `1.4.0-rc.1`; these must compare as equal.
        ("1.4.0rc1", "1.4.0-rc.1", "1.4.0-rc.1", True),
        ("1.4.0-rc.1", "1.4.0rc1", None, True),
        ("1.4.0rc1", "1.4.0-rc.1", None, True),
        # Stable versions still compare correctly.
        ("1.3.8", "1.3.8", "1.3.8", True),
        ("1.4.0", "1.4.0", None, True),
        ("1.4.0rc1", "1.4.0-rc.1", "1.4.0", False),
        # Genuinely different versions still fail.
        ("1.3.8", "1.4.0-rc.1", "1.4.0-rc.1", False),
        ("999.0.0", "1.4.0-rc.1", None, False),
        # Malformed versions fail safely.
        ("not-a-version", "1.4.0-rc.1", None, False),
        ("1.4.0rc1", "1.4.0-rc.1", "not-a-version", False),
    ],
)
def test_package_version_health_uses_pep440_equivalence(
    monkeypatch: pytest.MonkeyPatch,
    installed: str,
    package_version: str,
    expected: str | None,
    healthy: bool,
) -> None:
    monkeypatch.setattr(cli, "distribution_version", lambda _name: installed)
    monkeypatch.setattr("telegram_media_bot.__version__", package_version)

    result_healthy, detail = cli._package_version_health(expected)

    assert result_healthy is healthy
    assert detail == installed


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


@pytest.mark.parametrize(
    "action",
    ["status", "start", "stop", "serve", "migrate-to-local", "migrate-to-cloud"],
)
def test_local_api_dispatch_loads_settings_and_reaches_handler_without_unbound_local(
    action: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the local-api branch once used `settings` before loading it
    (UnboundLocalError in production). Every action must load settings and
    reach its intended handler."""
    config = tmp_path / "config.yaml"
    shutil.copyfile("config.example.yaml", config)
    calls: list[tuple[object, str, bool]] = []

    async def _spy(loaded_settings: object, loaded_action: str, confirmed: bool) -> None:
        calls.append((loaded_settings, loaded_action, confirmed))

    monkeypatch.setattr(cli, "_run_local_api", _spy)
    monkeypatch.setattr(
        sys,
        "argv",
        ["telegram-media-bot", "local-api", action, "--config", str(config)],
    )

    cli.main()

    assert len(calls) == 1
    loaded_settings, loaded_action, _confirmed = calls[0]
    assert loaded_action == action
    assert isinstance(loaded_settings, Settings)


@pytest.mark.parametrize(
    "action",
    ["status", "start", "stop", "serve", "migrate-to-local", "migrate-to-cloud"],
)
def test_local_api_dispatch_without_config_path_loads_default_settings(
    action: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, str, bool]] = []

    async def _spy(loaded_settings: object, loaded_action: str, confirmed: bool) -> None:
        calls.append((loaded_settings, loaded_action, confirmed))

    monkeypatch.setattr(cli, "_run_local_api", _spy)
    monkeypatch.setattr(
        "telegram_media_bot.bootstrap.config.default_config_path",
        lambda: tmp_path / "invalid.yaml",
    )
    (tmp_path / "invalid.yaml").write_text("::not: [valid: yaml\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["telegram-media-bot", "local-api", action],
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 2
    # A broken default config must fail cleanly (ConfigurationError -> exit 2),
    # never with an UnboundLocalError traceback.
    assert not calls


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


# --------------------------------------------------------------------------- #
# Operator control-plane configuration editing (config-edit)
# --------------------------------------------------------------------------- #


def _config_edit_args(arguments: list[str], *, config: Path | None = None) -> argparse.Namespace:
    if config is not None:
        arguments = [arguments[0], "--config", str(config), *arguments[1:]]
    return cli.build_parser().parse_args(["config-edit", *arguments])


def _write_example_config(path: Path) -> None:
    shutil.copyfile("config.example.yaml", path)
    path.chmod(0o600)


def test_config_edit_parser_supports_all_actions() -> None:
    assert _config_edit_args(["get", "telegram.admin_ids"]).config_edit_action == "get"
    assert _config_edit_args(["set", "queue.max_jobs", "5"]).config_edit_action == "set"
    assert (
        _config_edit_args(["list-add", "telegram.admin_ids", "1"]).config_edit_action == "list-add"
    )
    assert (
        _config_edit_args(
            ["channel-add", "--chat-id", "-1001", "--title", "t", "--join-url", "https://t.me/x"]
        ).chat_id
        == -1001
    )
    assert _config_edit_args(["logger-add", "-1001234567890"]).chat_id == -1001234567890
    assert _config_edit_args(["channel-status", "--probe"]).probe is True
    assert _config_edit_args(["telegram-status"]).probe is False


def test_config_edit_set_is_atomic_and_preserves_unrelated_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "config.yaml"
    _write_example_config(target)
    before = yaml.safe_load(target.read_text(encoding="utf-8"))

    cli._config_edit_set(
        _config_edit_args(["set", "queue.max_jobs", "5"], config=target), "queue.max_jobs", "5"
    )

    after = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert after["queue"]["max_jobs"] == 5
    assert after["telegram"] == before["telegram"]
    assert after["yt_dlp"] == before["yt_dlp"]
    assert not target.with_suffix(".yaml.tmp").exists()
    rollback = sorted(tmp_path.glob("config.yaml.tmb-rollback-*"))
    assert len(rollback) == 1
    if os.name != "nt":
        assert rollback[0].stat().st_mode & 0o777 == 0o600
    assert yaml.safe_load(rollback[0].read_text(encoding="utf-8")) == before
    output = capsys.readouterr().out
    assert "queue.max_jobs: 5" in output


def test_config_edit_set_validation_failure_preserves_original(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    _write_example_config(target)
    original = target.read_text(encoding="utf-8")

    with pytest.raises(ConfigurationError):
        cli._config_edit_set(
            _config_edit_args(["set", "telegram.max_upload_size_mb", "99999"], config=target),
            "telegram.max_upload_size_mb",
            "99999",
        )

    assert target.read_text(encoding="utf-8") == original
    assert not target.with_suffix(".yaml.tmp").exists()


def test_config_edit_secret_requires_stdin_and_never_prints_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import io

    target = tmp_path / "config.yaml"
    _write_example_config(target)
    with pytest.raises(ConfigurationError):
        cli._config_edit_set(
            _config_edit_args(["set", "telegram.bot_token", "literal"], config=target),
            "telegram.bot_token",
            "literal",
        )

    monkeypatch.setattr(sys, "stdin", io.StringIO("123456:STDIN_TOKEN_SECRET\n"))
    cli._config_edit_set(
        _config_edit_args(["set", "telegram.bot_token", "-"], config=target),
        "telegram.bot_token",
        "-",
    )
    settings = Settings.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")))
    assert settings.telegram.token() == "123456:STDIN_TOKEN_SECRET"
    output = capsys.readouterr().out
    assert "STDIN_TOKEN_SECRET" not in output
    assert "bot_token: configured" in output


def test_config_edit_get_never_prints_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "config.yaml"
    _write_example_config(target)

    cli._config_edit_get(
        _config_edit_args(["get", "telegram.bot_token"], config=target), "telegram.bot_token"
    )
    cli._config_edit_get(
        _config_edit_args(["get", "telegram.local_bot_api.api_hash"], config=target),
        "telegram.local_bot_api.api_hash",
    )
    cli._config_edit_get(
        _config_edit_args(["get", "telegram.admin_ids"], config=target), "telegram.admin_ids"
    )

    output = capsys.readouterr().out
    assert "bot_token: not configured" in output
    assert "api_hash: not configured" in output
    assert "admin_ids: (none)" in output
    assert "CHANGE_ME" not in output


def test_config_edit_get_rejects_unsupported_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "config.yaml"
    _write_example_config(target)
    with pytest.raises(ConfigurationError):
        cli._config_edit_get(
            _config_edit_args(["get", "telegram.caption_template"], config=target),
            "telegram.caption_template",
        )


def test_config_edit_list_add_and_remove_admin_ids(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    _write_example_config(target)
    cli._config_edit_list(
        _config_edit_args(["list-add", "telegram.admin_ids", "111"], config=target),
        "telegram.admin_ids",
        "111",
        add=True,
    )
    cli._config_edit_list(
        _config_edit_args(["list-add", "telegram.admin_ids", "222"], config=target),
        "telegram.admin_ids",
        "222",
        add=True,
    )
    settings = Settings.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")))
    assert settings.telegram.admin_ids == (111, 222)

    cli._config_edit_list(
        _config_edit_args(["list-remove", "telegram.admin_ids", "111"], config=target),
        "telegram.admin_ids",
        "111",
        add=False,
    )
    settings = Settings.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")))
    assert settings.telegram.admin_ids == (222,)


def test_config_edit_channel_add_and_remove_validate_through_model(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    _write_example_config(target)
    cli._config_edit_channel_add(
        _config_edit_args(
            [
                "channel-add",
                "--chat-id",
                "-1001",
                "--title",
                "One",
                "--join-url",
                "https://t.me/one",
            ],
            config=target,
        ),
        -1001,
        "One",
        "https://t.me/one",
    )
    settings = Settings.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")))
    added = [c for c in settings.telegram.required_channels.channels if c.chat_id == -1001]
    assert len(added) == 1
    assert added[0].title == "One"

    with pytest.raises(ConfigurationError):
        cli._config_edit_channel_add(
            _config_edit_args(
                [
                    "channel-add",
                    "--chat-id",
                    "-1002",
                    "--title",
                    "Bad",
                    "--join-url",
                    "https://evil.example/join",
                ],
                config=target,
            ),
            -1002,
            "Bad",
            "https://evil.example/join",
        )
    settings = Settings.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")))
    assert len(settings.telegram.required_channels.channels) == 2

    with pytest.raises(ConfigurationError):
        cli._config_edit_channel_add(
            _config_edit_args(
                [
                    "channel-add",
                    "--chat-id",
                    "-1001",
                    "--title",
                    "Dup",
                    "--join-url",
                    "https://t.me/one",
                ],
                config=target,
            ),
            -1001,
            "Dup",
            "https://t.me/one",
        )
    cli._config_edit_channel_update(
        _config_edit_args(
            ["channel-update", "-1001", "--title", "Renamed", "--join-url", "https://t.me/renamed"],
            config=target,
        ),
        -1001,
        "Renamed",
        "https://t.me/renamed",
    )
    settings = Settings.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")))
    updated = [c for c in settings.telegram.required_channels.channels if c.chat_id == -1001]
    assert updated[0].title == "Renamed"
    assert updated[0].join_url == "https://t.me/renamed"

    cli._config_edit_channel_remove(
        _config_edit_args(["channel-remove", "-1001"], config=target), -1001
    )
    settings = Settings.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")))
    assert all(channel.chat_id != -1001 for channel in settings.telegram.required_channels.channels)


def test_config_edit_logger_channel_rejects_public_chat_ids(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    _write_example_config(target)
    with pytest.raises(ConfigurationError):
        cli._config_edit_logger_channel(
            _config_edit_args(["logger-add", "12345"], config=target),
            12345,
            add=True,
        )

    cli._config_edit_logger_channel(
        _config_edit_args(["logger-add", "-1001234567890"], config=target),
        -1001234567890,
        add=True,
    )
    settings = Settings.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")))
    assert settings.telegram.logger.channels == (-1001234567890,)


def test_config_edit_logger_status_shows_safe_aggregates(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
    raw["telegram"]["logger"]["enabled"] = True
    raw["telegram"]["logger"]["channels"] = [-1001234567890]
    configured = Settings.model_validate(raw)

    cli._config_edit_logger_status(configured)
    output = capsys.readouterr().out
    assert "enabled: true" in output
    assert "outbox: durable_state=missing" in output
    assert "-1001234567890" not in output

    repository = SqliteAuditRepository(configured.database_path())
    repository.initialize()
    repository.reconcile_config(configured.telegram.logger.channels)
    cli._config_edit_logger_status(configured)
    output = capsys.readouterr().out
    assert "effective_destinations: 1" in output
    assert "active_destinations: 1" in output
    assert "oldest_pending_age_seconds: 0" in output


async def test_config_edit_telegram_status_never_prints_token(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    await cli._config_edit_telegram_status(settings, probe=False)
    output = capsys.readouterr().out
    assert settings.telegram.token() not in output
    assert "bot_token: configured" in output
    assert "api_mode: cloud" in output
    assert "connection: (not probed)" in output


def test_config_edit_cookie_status_is_passive_and_safe(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = settings.model_dump()
    raw["storage"]["root_directory"] = str(tmp_path)
    raw["yt_dlp"]["cookies_file"] = None
    configured = Settings.model_validate(raw)
    cli._config_edit_cookie_status(configured)
    assert "cookie_file: (not configured)" in capsys.readouterr().out

    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    raw["yt_dlp"]["cookies_file"] = str(cookie)
    configured = Settings.model_validate(raw)
    cli._config_edit_cookie_status(configured)
    output = capsys.readouterr().out
    assert "exists: true" in output
    assert "providers: " in output


def test_config_edit_local_api_status_is_safe(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    cli._config_edit_local_api_status(settings)
    output = capsys.readouterr().out
    assert "migration_phase: cloud" in output
    assert settings.telegram.token() not in output
