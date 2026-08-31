from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import signal
import subprocess
import sys
from contextlib import suppress
from datetime import UTC, datetime
from enum import StrEnum
from getpass import getpass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from arq.worker import run_worker

from telegram_media_bot.bootstrap.config import Settings, load_settings
from telegram_media_bot.bootstrap.logging import configure_logging
from telegram_media_bot.domain.errors import ConfigurationError, DeliveryError, LocalBotApiError
from telegram_media_bot.infrastructure.archive.multipart_zip import resolve_seven_zip
from telegram_media_bot.infrastructure.persistence.sqlite_repository import SqliteJobRepository
from telegram_media_bot.infrastructure.storage.workspace import sweep_workspaces
from telegram_media_bot.infrastructure.telegram.local_api import LocalBotApiManager


class DoctorMode(StrEnum):
    FULL = "full"
    OFFLINE = "offline"
    ONLINE = "online"


class DoctorOnlineService(StrEnum):
    BOT = "bot"
    LOCAL_API = "local-api"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="telegram-media-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bot = subparsers.add_parser("bot", help="Run Telegram polling process")
    bot.add_argument("--config", type=Path, default=None)

    worker = subparsers.add_parser("worker", help="Run ARQ download worker")
    worker.add_argument("--config", type=Path, default=None)

    companion = subparsers.add_parser(
        "companion", help="Run the optional secure web companion (T016)"
    )
    companion.add_argument("--config", type=Path, default=None)

    config_check = subparsers.add_parser("config-check", help="Validate configuration")
    config_check.add_argument("--config", type=Path, default=None)
    config_check.add_argument(
        "--read-only-runtime",
        action="store_true",
        help="Validate mounted runtime files without write probes",
    )

    doctor = subparsers.add_parser("doctor", help="Check local runtime prerequisites")
    doctor.add_argument("--config", type=Path, default=None)
    doctor_mode = doctor.add_mutually_exclusive_group()
    doctor_mode.add_argument(
        "--offline",
        action="store_true",
        help="Run only checks valid while project services are stopped",
    )
    doctor_mode.add_argument(
        "--online-service",
        action="append",
        choices=tuple(DoctorOnlineService),
        default=[],
        help="Run live checks for a restored service; may be repeated",
    )
    doctor.add_argument(
        "--expected-version",
        help="Require this installed package version in offline mode",
    )
    doctor.add_argument(
        "--read-only-runtime",
        action="store_true",
        help="Use non-mutating filesystem checks in offline mode",
    )

    cleanup = subparsers.add_parser(
        "cleanup-workspaces",
        help="Safely clean terminal and orphan job workspaces",
    )
    cleanup.add_argument("--config", type=Path, default=None)
    cleanup.add_argument("--dry-run", action="store_true")

    configure = subparsers.add_parser(
        "configure",
        help="Interactively create a Docker-oriented local configuration",
    )
    configure.add_argument("--config", type=Path, default=Path("config.yaml"))

    local_api = subparsers.add_parser("local-api", help="Manage Telegram Local Bot API")
    local_api.add_argument("--config", type=Path, default=None)
    local_actions = local_api.add_subparsers(dest="local_api_action", required=True)
    status = local_actions.add_parser("status", help="Show safe Local Bot API status")
    _add_local_api_config_argument(status)
    start = local_actions.add_parser("start", help="Start managed Local Bot API")
    _add_local_api_config_argument(start)
    stop = local_actions.add_parser("stop", help="Stop managed Local Bot API")
    _add_local_api_config_argument(stop)
    serve = local_actions.add_parser(
        "serve",
        help="Run the managed Local Bot API as a foreground service",
    )
    _add_local_api_config_argument(serve)
    migrate_local = local_actions.add_parser(
        "migrate-to-local", help="Explicitly migrate the bot from cloud to local"
    )
    _add_local_api_config_argument(migrate_local)
    migrate_local.add_argument("--yes", action="store_true", help="Confirm non-interactively")
    migrate_cloud = local_actions.add_parser(
        "migrate-to-cloud", help="Explicitly migrate the bot from local to cloud"
    )
    _add_local_api_config_argument(migrate_cloud)
    migrate_cloud.add_argument("--yes", action="store_true", help="Confirm non-interactively")

    return parser


def _add_local_api_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="Path to local YAML configuration",
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "bot":
            settings = load_settings(args.config, require_token=True)
            configure_logging(settings)
            from telegram_media_bot.telegram.bot_app import run_bot

            asyncio.run(run_bot(settings))
        elif args.command == "worker":
            if args.config is not None:
                os.environ["APP_CONFIG_PATH"] = str(args.config)
            settings = load_settings(args.config, require_token=True)
            configure_logging(settings)
            from telegram_media_bot.workers.settings import WorkerSettings

            run_worker(WorkerSettings)
        elif args.command == "companion":
            from aiohttp import web

            from telegram_media_bot.bootstrap.companion import (
                build_companion_app,
                load_companion_settings,
            )

            companion_settings = load_companion_settings(args.config)
            if not companion_settings.enabled:
                raise SystemExit("Companion is disabled in configuration.")
            application = build_companion_app(companion_settings)
            web.run_app(
                application,
                host=companion_settings.host,
                port=companion_settings.port,
                access_log=None,
            )
        elif args.command == "config-check":
            settings = load_settings(args.config, require_token=False)
            _run_config_check(
                settings,
                runtime_filesystem_read_only=bool(args.read_only_runtime),
            )
            print("Configuration is valid.")
        elif args.command == "doctor":
            settings = load_settings(args.config, require_token=False)
            if not args.offline and (args.expected_version or args.read_only_runtime):
                parser.error("--expected-version/--read-only-runtime require --offline")
            online_services = tuple(DoctorOnlineService(item) for item in args.online_service)
            mode = (
                DoctorMode.OFFLINE
                if args.offline
                else DoctorMode.ONLINE
                if online_services
                else DoctorMode.FULL
            )
            _run_doctor(
                settings,
                mode=mode,
                online_services=online_services,
                expected_version=args.expected_version,
                runtime_filesystem_read_only=bool(args.read_only_runtime),
            )
        elif args.command == "cleanup-workspaces":
            settings = load_settings(args.config, require_token=False)
            settings.create_runtime_directories()
            repository = SqliteJobRepository(settings.database_path())
            repository.initialize()
            report = sweep_workspaces(
                settings,
                repository,
                datetime.now(UTC),
                cleanup_reason="operator_dry_run" if args.dry_run else "operator",
                dry_run=bool(args.dry_run),
            )
            action = "Would reclaim" if args.dry_run else "Reclaimed"
            print(
                f"{action} {report.bytes_reclaimed} bytes; "
                f"{report.files_deleted} files and "
                f"{report.directories_deleted} directories; "
                f"{report.failed_paths_count} failures."
            )
        elif args.command == "configure":
            _run_interactive_configure(args.config)
        elif args.command == "local-api":
            settings = load_settings(args.config, require_token=True)
            configure_logging(settings)
            asyncio.run(
                _run_local_api(
                    settings,
                    args.local_api_action,
                    bool(getattr(args, "yes", False)),
                )
            )
    except (ConfigurationError, DeliveryError, LocalBotApiError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _run_doctor(
    settings: Settings,
    *,
    mode: DoctorMode = DoctorMode.FULL,
    online_services: tuple[DoctorOnlineService, ...] = (),
    expected_version: str | None = None,
    runtime_filesystem_read_only: bool = False,
) -> None:
    failed = False
    if mode in {DoctorMode.FULL, DoctorMode.OFFLINE}:
        failed = _run_static_doctor_checks(
            settings,
            expected_version=expected_version,
            runtime_filesystem_read_only=runtime_filesystem_read_only,
        )
    if mode is DoctorMode.FULL:
        failed = _run_default_live_doctor_checks(settings) or failed
    elif mode is DoctorMode.ONLINE:
        failed = _run_selected_live_doctor_checks(settings, online_services) or failed
    if failed:
        raise SystemExit(1)


def _run_static_doctor_checks(
    settings: Settings,
    *,
    expected_version: str | None,
    runtime_filesystem_read_only: bool,
) -> bool:
    javascript_runtime = settings.yt_dlp.javascript_runtime
    executable = "qjs" if javascript_runtime == "quickjs" else javascript_runtime
    checks = {
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        javascript_runtime: shutil.which(executable),
    }
    failed = sys.version_info < (3, 14)
    print(f"{'OK  ' if not failed else 'FAIL'} python: {sys.version.split()[0]}")
    package_healthy, package_detail = _package_version_health(expected_version)
    print(f"{'OK  ' if package_healthy else 'FAIL'} package: {package_detail}")
    failed = failed or not package_healthy
    from telegram_media_bot.infrastructure.ytdlp.engine import YtDlpEngine

    engine_health = YtDlpEngine(settings).health()
    print(
        f"{'OK  ' if engine_health.healthy else 'FAIL'} {engine_health.name}: {engine_health.detail}"
    )
    failed = failed or not engine_health.healthy
    from telegram_media_bot.infrastructure.gallerydl.adapter import GalleryDlEngine

    gallery_health = GalleryDlEngine(settings).health()
    state = "OK  " if gallery_health.healthy or not settings.gallery_dl.enabled else "FAIL"
    print(f"{state} {gallery_health.name}: {gallery_health.detail}")
    failed = failed or (settings.gallery_dl.enabled and not gallery_health.healthy)
    cookie_file = settings.effective_cookie_file()
    cookie_readable = _cookie_file_readable(cookie_file)
    print(f"{'OK  ' if cookie_readable else 'FAIL'} yt_dlp_cookie")
    failed = failed or not cookie_readable
    for source in sorted(settings.gallery_dl.enabled_platforms):
        cookie = settings.gallery_dl.cookie_for(source, cookie_file)
        readable = _cookie_file_readable(cookie)
        print(f"{'OK  ' if readable else 'FAIL'} gallery_dl_cookie_{source}")
        failed = failed or not readable
    from telegram_media_bot.infrastructure.analytics.usage_chart_doctor import (
        check_usage_chart_runtime,
    )

    for name, (healthy, detail) in check_usage_chart_runtime().items():
        print(f"{'OK  ' if healthy else 'FAIL'} {name}: {detail}")
        failed = failed or not healthy
    for name, path in checks.items():
        if path:
            print(f"OK   {name}: {_binary_version(path)}")
        else:
            failed = True
            print(f"FAIL {name}: not found")
    local_api = settings.telegram.local_bot_api
    if local_api.enabled:
        diagnostics = _local_api_static_diagnostics(
            settings,
            probe_directory_writes=not runtime_filesystem_read_only,
        )
        for name, healthy in diagnostics.items():
            print(f"{'OK  ' if healthy else 'FAIL'} local_api_{name}")
            failed = failed or not healthy
    if settings.multipart.enabled:
        seven_zip = resolve_seven_zip(settings.multipart.seven_zip_executable)
        if seven_zip:
            print(f"OK   7-Zip: {_binary_version(seven_zip)}")
        else:
            failed = True
            print("FAIL 7-Zip: neither configured executable nor compatible alias was found")
    return failed


def _package_version_health(expected_version: str | None) -> tuple[bool, str]:
    from telegram_media_bot import __version__

    try:
        installed_version = distribution_version("telegram-media-downloader-bot")
    except PackageNotFoundError:
        return False, "distribution metadata unavailable"
    healthy = installed_version == __version__ and (
        expected_version is None or installed_version == expected_version
    )
    return healthy, installed_version


def _run_default_live_doctor_checks(settings: Settings) -> bool:
    failed = False
    if settings.telegram.local_bot_api.enabled:
        reachable = LocalBotApiManager(settings).endpoint_reachable()
        print(f"{'OK  ' if reachable else 'FAIL'} local_api_reachable")
        failed = failed or not reachable
    if settings.telegram.required_channels.enabled:
        diagnostics = asyncio.run(_bot_online_diagnostics(settings))
        channels_ok = diagnostics["required_channels"]
        print(f"{'OK  ' if channels_ok else 'FAIL'} required_channels")
        failed = failed or not channels_ok
    return failed


def _run_selected_live_doctor_checks(
    settings: Settings,
    online_services: tuple[DoctorOnlineService, ...],
) -> bool:
    failed = False
    selected = set(online_services)
    if DoctorOnlineService.LOCAL_API in selected:
        configured = settings.telegram.local_bot_api.enabled
        print(f"{'OK  ' if configured else 'FAIL'} local_api_configured")
        reachable = configured and LocalBotApiManager(settings).endpoint_reachable()
        print(f"{'OK  ' if reachable else 'FAIL'} local_api_reachable")
        failed = failed or not configured or not reachable
    if DoctorOnlineService.BOT in selected:
        diagnostics = asyncio.run(_bot_online_diagnostics(settings))
        bot_reachable = diagnostics["bot_reachable"]
        print(f"{'OK  ' if bot_reachable else 'FAIL'} bot_reachable")
        failed = failed or not bot_reachable
        if settings.telegram.required_channels.enabled:
            channels_ok = diagnostics["required_channels"]
            print(f"{'OK  ' if channels_ok else 'FAIL'} required_channels")
            failed = failed or not channels_ok
    return failed


async def _bot_online_diagnostics(settings: Settings) -> dict[str, bool]:
    diagnostics = {"bot_reachable": False, "required_channels": False}
    if settings.telegram.token() in {"", "CHANGE_ME"}:
        return diagnostics
    from telegram_media_bot.telegram.bot_factory import create_bot

    bot = create_bot(settings)
    try:
        identity = await bot.get_me()
        diagnostics["bot_reachable"] = True
        if not settings.telegram.required_channels.enabled:
            diagnostics["required_channels"] = True
            return diagnostics
        for channel in settings.telegram.required_channels.channels:
            member = await bot.get_chat_member(channel.chat_id, identity.id)
            status = getattr(member.status, "value", str(member.status))
            if status not in {"creator", "administrator"}:
                return diagnostics
        diagnostics["required_channels"] = True
        return diagnostics
    except Exception:
        return diagnostics
    finally:
        await bot.session.close()


def _run_config_check(
    settings: Settings,
    *,
    runtime_filesystem_read_only: bool = False,
) -> None:
    gallery_failures: list[str] = []
    cookie_file = settings.effective_cookie_file()
    if not _cookie_file_readable(cookie_file):
        gallery_failures.append("yt_dlp_cookie")
    if settings.gallery_dl.enabled:
        from telegram_media_bot.infrastructure.gallerydl.adapter import GalleryDlEngine

        if not GalleryDlEngine(settings).health().healthy:
            gallery_failures.append("gallery_dl_runtime")
        for source in sorted(settings.gallery_dl.enabled_platforms):
            cookie = settings.gallery_dl.cookie_for(source, cookie_file)
            if not _cookie_file_readable(cookie):
                gallery_failures.append(f"gallery_dl_cookie_{source}")
    if gallery_failures:
        raise ConfigurationError(
            "Cookie/runtime configuration checks failed: " + ", ".join(gallery_failures)
        )
    local_api = settings.telegram.local_bot_api
    if not local_api.enabled:
        return
    diagnostics = _local_api_static_diagnostics(
        settings,
        probe_directory_writes=not runtime_filesystem_read_only,
    )
    failed = [name for name, healthy in diagnostics.items() if not healthy]
    if failed:
        raise ConfigurationError(
            f"Local Bot API configuration checks failed: {', '.join(sorted(failed))}"
        )


def _cookie_file_readable(cookie_file: Path | None) -> bool:
    return cookie_file is None or (cookie_file.is_file() and os.access(cookie_file, os.R_OK))


def _local_api_static_diagnostics(
    settings: Settings,
    *,
    probe_directory_writes: bool = True,
) -> dict[str, bool]:
    local_api = settings.telegram.local_bot_api
    base_url = settings.telegram.local_api_base_url
    parsed = urlsplit(base_url) if base_url else None
    executable_ok = True
    if local_api.mode == "managed":
        executable_ok = bool(
            local_api.executable and local_api.executable.expanduser().resolve().is_file()
        )
    directories = [local_api.migration.state_file.parent]
    if local_api.mode == "managed":
        directories.extend(
            (
                local_api.working_directory,
                local_api.temp_directory,
                local_api.log_file.parent,
            )
        )
    directories_ok = all(
        _directory_writable(path) if probe_directory_writes else _directory_readable(path)
        for path in directories
    )
    credentials_ok = True
    if local_api.mode == "managed":
        credentials_ok = bool(
            local_api.api_id
            and local_api.api_hash
            and local_api.api_hash.get_secret_value() not in {"", "CHANGE_ME"}
        )
    endpoint_ok = bool(
        parsed
        and parsed.scheme in {"http", "https"}
        and parsed.hostname
        and (parsed.port or local_api.port)
    )
    manager = LocalBotApiManager(settings)
    migration_ok = True
    try:
        manager.migration_store.read()
    except LocalBotApiError:
        migration_ok = False
    return {
        "configuration": endpoint_ok,
        "credentials": credentials_ok,
        "directories": directories_ok,
        "executable": executable_ok,
        "migration": migration_ok,
    }


def _directory_writable(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        probe = resolved / ".local-api-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    return True


def _directory_readable(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    return resolved.is_dir() and os.access(resolved, os.R_OK | os.X_OK)


async def _run_local_api(settings: Settings, action: str, confirmed: bool) -> None:
    manager = LocalBotApiManager(settings)
    if action == "status":
        _print_local_api_status(manager)
        return
    if action == "start":
        manager.start()
        _print_local_api_status(manager)
        return
    if action == "stop":
        manager.stop()
        _print_local_api_status(manager)
        return
    if action == "serve":
        manager.start()
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for selected_signal in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(selected_signal, stopped.set)
        try:
            await stopped.wait()
        finally:
            manager.stop()
        return
    if action == "migrate-to-local":
        manager.status()
        _require_migration_confirmation("MIGRATE-TO-LOCAL", confirmed)
        await manager.migrate_to_local()
        _print_local_api_status(manager)
        return
    if action == "migrate-to-cloud":
        manager.status()
        _require_migration_confirmation("MIGRATE-TO-CLOUD", confirmed)
        await manager.migrate_to_cloud()
        _print_local_api_status(manager)
        return
    raise LocalBotApiError("Unknown Local Bot API action")


def _require_migration_confirmation(phrase: str, confirmed: bool) -> None:
    if confirmed:
        return
    try:
        answer = input(f"Type {phrase} to continue: ").strip()
    except EOFError as exc:
        raise LocalBotApiError("Migration requires explicit confirmation") from exc
    if answer != phrase:
        raise LocalBotApiError("Migration confirmation did not match")


def _print_local_api_status(manager: LocalBotApiManager) -> None:
    status = manager.status()
    print(f"enabled: {str(status.enabled).lower()}")
    print(f"mode: {status.mode}")
    print(f"process_running: {str(status.process_running).lower()}")
    print(f"endpoint_reachable: {str(status.endpoint_reachable).lower()}")
    print(f"migration_phase: {status.migration_phase}")
    print(f"active_endpoint: {status.active_endpoint}")


def _binary_version(path: str) -> str:
    try:
        completed = subprocess.run(
            [path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return f"{path} (version unavailable)"
    first_line = [
        line for line in (completed.stdout or completed.stderr).splitlines() if line.strip()
    ]
    version = first_line[0][:200] if first_line else "version unavailable"
    return f"{path} ({version})"


def _run_interactive_configure(path: Path) -> None:
    target = path.expanduser().resolve()
    if target.exists():
        with target.open("r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    else:
        example = Path("config.example.yaml").resolve()
        if not example.is_file():
            raise ConfigurationError("config.example.yaml is unavailable")
        with example.open("r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        raise ConfigurationError("Configuration template is invalid")
    telegram = raw.get("telegram")
    media = raw.get("media")
    ytdlp = raw.get("yt_dlp")
    if not isinstance(telegram, dict) or not isinstance(media, dict) or not isinstance(ytdlp, dict):
        raise ConfigurationError("Configuration template is incomplete")

    token = getpass("Telegram bot token: ").strip()
    if not token:
        raise ConfigurationError("Telegram bot token cannot be empty")
    api_id = _prompt_positive_integer("Telegram API ID: ")
    api_hash = getpass("Telegram API hash: ").strip()
    if not api_hash:
        raise ConfigurationError("Telegram API hash cannot be empty")
    admin_ids = _prompt_integer_list("Admin user IDs (comma-separated, optional): ")

    telegram["bot_token"] = token
    telegram["admin_ids"] = admin_ids
    telegram["max_upload_size_mb"] = 1900
    telegram["local_api_base_url"] = "http://local-api:8081"
    telegram["local_api_is_local"] = True
    local_api = telegram.get("local_bot_api")
    if not isinstance(local_api, dict):
        local_api = {}
        telegram["local_bot_api"] = local_api
    local_api.update(
        {
            "enabled": True,
            "mode": "managed",
            "executable": "/usr/local/bin/telegram-bot-api",
            "api_id": api_id,
            "api_hash": api_hash,
            "host": "0.0.0.0",
            "port": 8081,
            "local_mode": True,
            "working_directory": "/data/telegram-bot-api",
            "temp_directory": "/data/telegram-bot-api/temp",
            "log_file": "/data/telegram-bot-api/telegram-bot-api.log",
            "auto_start": True,
            "lifecycle_owner": "service",
            "migration": {
                "auto_logout_from_cloud": False,
                "state_file": "/data/state/telegram-api-migration.json",
            },
        }
    )
    required = telegram.get("required_channels")
    if not isinstance(required, dict):
        required = {}
        telegram["required_channels"] = required
    channels = _prompt_channels()
    required.update(
        {
            "enabled": bool(channels),
            "positive_cache_ttl_seconds": 300,
            "negative_cache_ttl_seconds": 30,
            "channels": channels,
        }
    )

    proxy = getpass("yt-dlp proxy URL (optional, input hidden): ").strip()
    ytdlp["proxy_enabled"] = bool(proxy)
    ytdlp["proxy"] = proxy or None
    cookies = input("Instagram cookies file path (optional): ").strip()
    ytdlp["cookies_file"] = cookies or None
    media["max_file_size_mb"] = 4096
    media["max_source_size_mb"] = 4096

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            yaml.safe_dump(raw, stream, allow_unicode=True, sort_keys=False)
        if os.name != "nt":
            temporary.chmod(0o600)
        load_settings(temporary, require_token=True)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    print("Configuration written successfully; secrets were not displayed.")


def _prompt_positive_integer(prompt: str) -> int:
    value = input(prompt).strip()
    if not value.isdigit() or int(value) <= 0:
        raise ConfigurationError("A positive numeric API ID is required")
    return int(value)


def _prompt_integer_list(prompt: str) -> list[int]:
    value = input(prompt).strip()
    if not value:
        return []
    items = [item.strip() for item in value.split(",")]
    if any(not item.lstrip("-").isdigit() for item in items):
        raise ConfigurationError("Admin IDs must be comma-separated integers")
    return [int(item) for item in items]


def _prompt_channels() -> list[dict[str, object]]:
    channels: list[dict[str, object]] = []
    while input("Add a required channel? [y/N]: ").strip().casefold() == "y":
        chat_id = input("Channel chat ID (-100...): ").strip()
        title = input("Channel title: ").strip()
        join_url = input("Channel join URL (https://t.me/...): ").strip()
        if not chat_id.lstrip("-").isdigit() or not title or not join_url:
            raise ConfigurationError("Required channel values are incomplete")
        channels.append(
            {
                "chat_id": int(chat_id),
                "title": title,
                "join_url": join_url,
            }
        )
    return channels
