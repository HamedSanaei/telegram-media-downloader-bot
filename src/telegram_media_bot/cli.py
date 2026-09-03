from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
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

from telegram_media_bot.bootstrap.config import Settings, default_config_path, load_settings
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

    config_edit = subparsers.add_parser(
        "config-edit",
        help="Safely read/edit typed local configuration (operator control plane)",
    )
    config_edit.add_argument("--config", type=Path, default=None)
    config_edit_actions = config_edit.add_subparsers(dest="config_edit_action", required=True)
    config_edit_get = config_edit_actions.add_parser(
        "get", help="Print a sanitized configuration value"
    )
    _add_config_edit_config_argument(config_edit_get)
    config_edit_get.add_argument("key", help="Dotted configuration key")
    config_edit_set = config_edit_actions.add_parser(
        "set", help="Set a configuration value atomically ('-' reads a secret from stdin)"
    )
    _add_config_edit_config_argument(config_edit_set)
    config_edit_set.add_argument("key", help="Dotted configuration key")
    config_edit_set.add_argument("value", help="New value, or '-' to read a secret from stdin")
    for action_name in ("list-add", "list-remove"):
        action_parser = config_edit_actions.add_parser(
            action_name, help=f"{action_name} an integer list entry"
        )
        _add_config_edit_config_argument(action_parser)
        action_parser.add_argument("key", help="Dotted list configuration key")
        action_parser.add_argument("value", help="Integer list entry")
    channel_add = config_edit_actions.add_parser("channel-add", help="Add a required channel")
    _add_config_edit_config_argument(channel_add)
    channel_add.add_argument("--chat-id", type=int, required=True)
    channel_add.add_argument("--title", required=True)
    channel_add.add_argument("--join-url", required=True)
    channel_remove = config_edit_actions.add_parser(
        "channel-remove", help="Remove a required channel by chat ID"
    )
    _add_config_edit_config_argument(channel_remove)
    channel_remove.add_argument("chat_id", type=int)
    channel_update = config_edit_actions.add_parser(
        "channel-update", help="Update a required channel title/join URL"
    )
    _add_config_edit_config_argument(channel_update)
    channel_update.add_argument("chat_id", type=int)
    channel_update.add_argument("--title", default=None)
    channel_update.add_argument("--join-url", default=None)
    channel_status = config_edit_actions.add_parser(
        "channel-status", help="List required channels (optionally probe access)"
    )
    _add_config_edit_config_argument(channel_status)
    channel_status.add_argument("--probe", action="store_true")
    for action_name in ("logger-add", "logger-remove"):
        action_parser = config_edit_actions.add_parser(
            action_name, help=f"{action_name} a logger destination channel"
        )
        _add_config_edit_config_argument(action_parser)
        action_parser.add_argument("chat_id", type=int)
    logger_status = config_edit_actions.add_parser(
        "logger-status", help="Show safe Operator Logger aggregate health"
    )
    _add_config_edit_config_argument(logger_status)
    telegram_status = config_edit_actions.add_parser(
        "telegram-status", help="Show safe Telegram configuration status"
    )
    _add_config_edit_config_argument(telegram_status)
    telegram_status.add_argument("--probe", action="store_true")
    cookie_status = config_edit_actions.add_parser(
        "cookie-status", help="Show passive local cookie file health"
    )
    _add_config_edit_config_argument(cookie_status)
    local_api_status = config_edit_actions.add_parser(
        "local-api-status", help="Show Local Bot API and migration state"
    )
    _add_config_edit_config_argument(local_api_status)

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


def _add_config_edit_config_argument(parser: argparse.ArgumentParser) -> None:
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
        elif args.command == "config-edit":
            settings = load_settings(args.config, require_token=False)
            asyncio.run(_run_config_edit(settings, args))
        elif args.command == "local-api":
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
    logger_healthy, logger_detail = _logger_doctor_health(
        settings, runtime_filesystem_read_only=runtime_filesystem_read_only
    )
    print(f"{'OK  ' if logger_healthy else 'FAIL'} operator_logger: {logger_detail}")
    failed = failed or not logger_healthy
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


def _logger_doctor_health(
    settings: Settings,
    *,
    runtime_filesystem_read_only: bool,
) -> tuple[bool, str]:
    logger_settings = settings.telegram.logger
    if not logger_settings.enabled:
        return True, "disabled"
    database = settings.database_path()
    if not database.is_file():
        return False, "enabled;durable_state=missing"
    if runtime_filesystem_read_only:
        return _logger_doctor_readonly_filesystem(settings, database)
    from telegram_media_bot.infrastructure.persistence.sqlite_audit import SqliteAuditRepository

    try:
        snapshot = SqliteAuditRepository(database).health_snapshot()
    except Exception:
        return False, "enabled;durable_state=unavailable"
    healthy = snapshot.active_destinations > 0
    detail = (
        f"enabled;configured={len(logger_settings.channels)};"
        f"effective={snapshot.effective_destinations};active={snapshot.active_destinations};"
        f"unreachable={snapshot.unreachable_destinations};"
        f"forbidden={snapshot.forbidden_destinations};disabled={snapshot.disabled_destinations};"
        f"pending={snapshot.pending_effects};retryable={snapshot.retryable_effects};"
        f"uncertain={snapshot.uncertain_effects};terminal={snapshot.terminal_effects};"
        f"oldest_pending_seconds={snapshot.oldest_pending_age_seconds};"
        f"alerts={int(logger_settings.alerts_enabled)};"
        f"mirror={int(logger_settings.submission_mirror_enabled)}"
    )
    return healthy, detail


def _logger_doctor_readonly_filesystem(
    settings: Settings,
    database: Path,
) -> tuple[bool, str]:
    """Read-only pre-stop preflight: filesystem-level validation only.

    A WAL-backed SQLite database whose `-wal`/`-shm` files are absent cannot be
    opened on a read-only bind mount (SQLITE_CANTOPEN: creating the shared-memory
    file requires a write). The pre-stop candidate preflight therefore must NOT
    open the durable database at all: it verifies readability of the existing
    files, proves no file is created or modified, and defers the real snapshot
    check to the strong post-stop verification.
    """
    try:
        if not database.is_file():
            return False, "enabled;durable_state=missing"
        if not os.access(database, os.R_OK):
            return False, "enabled;durable_state=unreadable"
        state_dir = database.parent
        if not state_dir.is_dir() or not os.access(state_dir, os.R_OK | os.X_OK):
            return False, "enabled;durable_state=state_unreadable"
        before = _database_files_state(database)
        with database.open("rb") as handle:
            header = handle.read(16)
        if len(header) < 16:
            return False, "enabled;durable_state=corrupt"
        after = _database_files_state(database)
        if after != before:
            return False, "enabled;durable_state=mutated"
    except OSError:
        return False, "enabled;durable_state=unavailable"
    return True, "enabled;durable_state=deferred-readonly"


def _database_files_state(
    database: Path,
) -> tuple[tuple[str, int, int], ...]:
    """Capture (name, size, mtime_ns) for the DB and any WAL/SHM sidecars."""
    state: list[tuple[str, int, int]] = []
    for candidate in (
        database,
        database.with_suffix(database.suffix + "-wal"),
        database.with_suffix(database.suffix + "-shm"),
    ):
        if candidate.exists():
            stat = candidate.stat()
            state.append((candidate.name, stat.st_size, stat.st_mtime_ns))
    return tuple(state)


def _package_version_health(expected_version: str | None) -> tuple[bool, str]:
    from telegram_media_bot import __version__
    from telegram_media_bot.versions import versions_equal

    try:
        installed_version = distribution_version("telegram-media-downloader-bot")
    except PackageNotFoundError:
        return False, "distribution metadata unavailable"
    healthy = versions_equal(installed_version, __version__) and (
        expected_version is None or versions_equal(installed_version, expected_version)
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


# --------------------------------------------------------------------------- #
# Operator control-plane configuration editing (config-edit)
# --------------------------------------------------------------------------- #
# The Bash `tmb` manager is a frontend over these typed operations. Business
# validation stays in the Pydantic configuration models; every write is atomic,
# validated against the complete settings before publication, and protected by a
# rollback copy. Secret values never appear in arguments or output.
# --------------------------------------------------------------------------- #

_CONFIG_EDIT_ROLLBACK_KEEP = 5


def _config_edit_fields() -> dict[str, tuple[object, ...]]:
    """Supported dotted keys: kind followed by kind-specific constraints.

    Kinds: ``secret``, ``str``, ``nullable_str``, ``bool``, ``int(min, max)``,
    ``choice(value, ...)``.
    """
    return {
        "app.log_level": ("choice", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        "telegram.bot_token": ("secret",),
        "telegram.support_username": ("nullable_str",),
        "telegram.polling_timeout_seconds": ("int", 5, 60),
        "telegram.upload_as_document": ("bool",),
        "telegram.max_upload_size_mb": ("int", 1, 1900),
        "telegram.upload_timeout_seconds": ("int", 60, 86400),
        "telegram.local_api_base_url": ("nullable_str",),
        "telegram.local_api_is_local": ("bool",),
        "telegram.local_bot_api.enabled": ("bool",),
        "telegram.local_bot_api.mode": ("choice", "managed", "external"),
        "telegram.local_bot_api.api_id": ("int", 1, 2**31 - 1),
        "telegram.local_bot_api.api_hash": ("secret",),
        "telegram.local_bot_api.host": ("str",),
        "telegram.local_bot_api.port": ("int", 1, 65535),
        "telegram.local_bot_api.local_mode": ("bool",),
        "telegram.local_bot_api.auto_start": ("bool",),
        "telegram.required_channels.enabled": ("bool",),
        "telegram.logger.enabled": ("bool",),
        "telegram.logger.alerts_enabled": ("bool",),
        "telegram.logger.submission_mirror_enabled": ("bool",),
        "telegram.logger.payment_events_enabled": ("bool",),
        "telegram.logger.operator_privacy_attested": ("bool",),
        "media.max_file_size_mb": ("int", 1, 8192),
        "media.max_source_size_mb": ("int", 1, 8192),
        "media.allow_playlists": ("bool",),
        "media.default_mode": (
            "choice",
            "best",
            "best_original",
            "video_2160",
            "video_1440",
            "video_1080",
            "video_720",
            "video_480",
            "audio_best",
            "audio_mp3",
        ),
        "queue.max_jobs": ("int", 1, 100),
        "queue.max_tries": ("int", 1, 10),
        "queue.job_timeout_seconds": ("int", 30, 86400),
        "storage.delete_after_upload": ("bool",),
        "storage.job_retention_days": ("int", 1, 3650),
        "multipart.enabled": ("bool",),
        "yt_dlp.proxy_enabled": ("bool",),
        "yt_dlp.proxy": ("secret",),
        "operations.update.prune_old_project_images_after_success": ("bool",),
    }


def _config_edit_list_fields() -> dict[str, str]:
    """Supported integer-list dotted keys."""
    return {
        "telegram.admin_ids": "int",
        "telegram.logger.channels": "int",
    }


def _config_edit_section(raw: dict[str, object], key: str) -> object:
    current: object = raw
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _config_edit_key_path(raw: dict[str, object], key: str) -> tuple[dict[str, object], str]:
    """Return (parent mapping, leaf name) for a dotted key, creating missing maps."""
    parts = key.split(".")
    if len(parts) < 2:
        raise ConfigurationError(f"configuration key must be dotted: {key}")
    current: dict[str, object] = raw
    for part in parts[:-1]:
        existing = current.get(part)
        if existing is None:
            created: dict[str, object] = {}
            current[part] = created
            current = created
        elif isinstance(existing, dict):
            current = existing
        else:
            raise ConfigurationError(f"configuration key {key} conflicts with a scalar value")
    return current, parts[-1]


def _config_edit_parse_value(key: str, value: str) -> object:
    spec = _config_edit_fields().get(key)
    if spec is None:
        raise ConfigurationError(f"unsupported configuration key: {key}")
    kind = spec[0]
    if kind == "secret":
        return value
    if kind == "str":
        return value
    if kind == "nullable_str":
        return value or None
    if kind == "bool":
        normalized = value.casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        raise ConfigurationError(f"{key} expects a boolean value")
    if kind == "int":
        minimum, maximum = spec[1], spec[2]
        if not isinstance(minimum, int) or not isinstance(maximum, int):
            raise ConfigurationError(f"{key} has an invalid numeric range")
        if not value.lstrip("-").isdigit():
            raise ConfigurationError(f"{key} expects an integer value")
        parsed = int(value)
        if not minimum <= parsed <= maximum:
            raise ConfigurationError(f"{key} must be between {minimum} and {maximum}")
        return parsed
    if kind == "choice":
        choices = tuple(str(item) for item in spec[1:])
        if value not in choices:
            raise ConfigurationError(f"{key} must be one of: {', '.join(choices)}")
        return value
    raise ConfigurationError(f"unsupported configuration field kind: {kind}")


def _config_edit_format_value(key: str, value: object) -> str:
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value) if value else "(none)"
    if key in _config_edit_fields() and _config_edit_fields()[key][0] == "secret":
        return "configured" if value not in (None, "", "CHANGE_ME") else "not configured"
    if value is None:
        return "(none)"
    return str(value)


def _create_config_rollback_copy(path: Path) -> Path | None:
    if not path.is_file():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rollback = path.with_name(f"{path.name}.tmb-rollback-{stamp}")
    shutil.copy2(path, rollback)
    if os.name != "nt":
        rollback.chmod(0o600)
    siblings = sorted(path.parent.glob(f"{path.name}.tmb-rollback-*"))
    for stale in siblings[: max(0, len(siblings) - _CONFIG_EDIT_ROLLBACK_KEEP)]:
        with suppress(OSError):
            stale.unlink()
    return rollback


def _edit_config_file(path: Path, mutator: Callable[[dict[str, object]], None]) -> None:
    resolved = path.expanduser().resolve()
    try:
        with resolved.open("r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Configuration file not found: {resolved}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError("Invalid YAML configuration") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("Configuration root must be a mapping")
    before = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    mutator(raw)
    after = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    if after == before:
        return
    _create_config_rollback_copy(resolved)
    temporary = resolved.with_suffix(f"{resolved.suffix}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(after)
        if os.name != "nt":
            temporary.chmod(0o600)
        load_settings(temporary, require_token=False)
        os.replace(temporary, resolved)
    finally:
        temporary.unlink(missing_ok=True)


def _config_edit_resolved_path(args: argparse.Namespace) -> Path:
    configured = getattr(args, "config", None)
    if configured is not None:
        return Path(configured).expanduser().resolve()
    return default_config_path().expanduser().resolve()


def _config_edit_get(args: argparse.Namespace, key: str) -> None:
    if key not in _config_edit_fields() and key not in _config_edit_list_fields():
        raise ConfigurationError(f"unsupported configuration key: {key}")
    path = _config_edit_resolved_path(args)
    with path.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        raise ConfigurationError("Configuration root must be a mapping")
    value = _config_edit_section(raw, key)
    print(f"{key}: {_config_edit_format_value(key, value)}")


def _config_edit_set(args: argparse.Namespace, key: str, value: str) -> None:
    if value == "-":
        value = sys.stdin.read().rstrip("\n")
        if not value:
            raise ConfigurationError(f"{key} cannot be empty")
    elif key in _config_edit_fields() and _config_edit_fields()[key][0] == "secret":
        raise ConfigurationError(
            f"{key} is a secret; pass '-' as the value and provide it on stdin"
        )

    def mutate(raw: dict[str, object]) -> None:
        parent, leaf = _config_edit_key_path(raw, key)
        parent[leaf] = _config_edit_parse_value(key, value)

    path = _config_edit_resolved_path(args)
    _edit_config_file(path, mutate)
    parsed = _config_edit_parse_value(key, value)
    print(f"{key}: {_config_edit_format_value(key, parsed)}")


def _config_edit_list(args: argparse.Namespace, key: str, value: str, *, add: bool) -> None:
    if key not in _config_edit_list_fields():
        raise ConfigurationError(f"unsupported list configuration key: {key}")
    if not value.lstrip("-").isdigit():
        raise ConfigurationError(f"{key} expects integer entries")
    parsed = int(value)

    def mutate(raw: dict[str, object]) -> None:
        parent, leaf = _config_edit_key_path(raw, key)
        current = parent.get(leaf)
        items = list(current) if isinstance(current, (list, tuple)) else []
        if any(not isinstance(item, int) for item in items):
            raise ConfigurationError(f"{key} must contain only integers")
        if add:
            if parsed in items:
                return
            items.append(parsed)
        else:
            if parsed not in items:
                return
            items.remove(parsed)
        parent[leaf] = items

    path = _config_edit_resolved_path(args)
    _edit_config_file(path, mutate)
    print(f"{key}: {'added' if add else 'removed'} {parsed}")


def _config_edit_channel_add(
    args: argparse.Namespace, chat_id: int, title: str, join_url: str
) -> None:
    if not title.strip():
        raise ConfigurationError("channel title cannot be empty")
    if len(title) > 128:
        raise ConfigurationError("channel title is too long")
    if not str(chat_id).lstrip("-").isdigit():
        raise ConfigurationError("channel chat ID must be an integer")

    def mutate(raw: dict[str, object]) -> None:
        telegram = raw.get("telegram")
        if not isinstance(telegram, dict):
            raise ConfigurationError("configuration is missing the telegram section")
        required = telegram.get("required_channels")
        if not isinstance(required, dict):
            required = {}
            telegram["required_channels"] = required
        channels = required.get("channels")
        entries = list(channels) if isinstance(channels, list) else []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("chat_id") == chat_id:
                raise ConfigurationError(f"channel {chat_id} is already configured")
        entries.append({"chat_id": chat_id, "title": title.strip(), "join_url": join_url})
        required["channels"] = entries

    _edit_config_file(_config_edit_resolved_path(args), mutate)
    print(f"channel {chat_id}: added")


def _config_edit_channel_remove(args: argparse.Namespace, chat_id: int) -> None:
    def mutate(raw: dict[str, object]) -> None:
        telegram = raw.get("telegram")
        if not isinstance(telegram, dict):
            raise ConfigurationError("configuration is missing the telegram section")
        required = telegram.get("required_channels")
        if not isinstance(required, dict):
            return
        channels = required.get("channels")
        if not isinstance(channels, list):
            return
        remaining = [
            entry
            for entry in channels
            if not (isinstance(entry, dict) and entry.get("chat_id") == chat_id)
        ]
        if len(remaining) == len(channels):
            raise ConfigurationError(f"channel {chat_id} is not configured")
        required["channels"] = remaining

    _edit_config_file(_config_edit_resolved_path(args), mutate)
    print(f"channel {chat_id}: removed")


def _config_edit_channel_update(
    args: argparse.Namespace, chat_id: int, title: str | None, join_url: str | None
) -> None:
    if title is None and join_url is None:
        raise ConfigurationError("channel-update requires --title and/or --join-url")
    if title is not None and (not title.strip() or len(title) > 128):
        raise ConfigurationError("channel title must be 1-128 characters")

    def mutate(raw: dict[str, object]) -> None:
        telegram = raw.get("telegram")
        if not isinstance(telegram, dict):
            raise ConfigurationError("configuration is missing the telegram section")
        required = telegram.get("required_channels")
        if not isinstance(required, dict):
            raise ConfigurationError(f"channel {chat_id} is not configured")
        channels = required.get("channels")
        if not isinstance(channels, list):
            raise ConfigurationError(f"channel {chat_id} is not configured")
        for entry in channels:
            if isinstance(entry, dict) and entry.get("chat_id") == chat_id:
                if title is not None:
                    entry["title"] = title.strip()
                if join_url is not None:
                    entry["join_url"] = join_url
                return
        raise ConfigurationError(f"channel {chat_id} is not configured")

    _edit_config_file(_config_edit_resolved_path(args), mutate)
    print(f"channel {chat_id}: updated")


def _config_edit_logger_channel(args: argparse.Namespace, chat_id: int, *, add: bool) -> None:
    def mutate(raw: dict[str, object]) -> None:
        telegram = raw.get("telegram")
        if not isinstance(telegram, dict):
            raise ConfigurationError("configuration is missing the telegram section")
        logger = telegram.get("logger")
        if not isinstance(logger, dict):
            logger = {}
            telegram["logger"] = logger
        channels = logger.get("channels")
        entries = list(channels) if isinstance(channels, list) else []
        if any(not isinstance(item, int) for item in entries):
            raise ConfigurationError("logger channels must contain only integers")
        if add:
            if chat_id in entries:
                return
            entries.append(chat_id)
        else:
            if chat_id not in entries:
                return
            entries.remove(chat_id)
        logger["channels"] = entries

    _edit_config_file(_config_edit_resolved_path(args), mutate)
    print(f"logger destination {chat_id}: {'added' if add else 'removed'}")


def _config_edit_logger_status(settings: Settings) -> None:
    logger = settings.telegram.logger
    print(f"enabled: {str(logger.enabled).lower()}")
    print(f"configured_destinations: {len(logger.channels)}")
    print(f"alerts_enabled: {str(logger.alerts_enabled).lower()}")
    print(f"submission_mirror_enabled: {str(logger.submission_mirror_enabled).lower()}")
    print(f"payment_events_enabled: {str(logger.payment_events_enabled).lower()}")
    print(f"operator_privacy_attested: {str(logger.operator_privacy_attested).lower()}")
    if not logger.enabled:
        print("outbox: disabled")
        return
    database = settings.database_path()
    if not database.is_file():
        print("outbox: durable_state=missing")
        return
    from telegram_media_bot.infrastructure.persistence.sqlite_audit import SqliteAuditRepository

    try:
        snapshot = SqliteAuditRepository(database).health_snapshot()
    except Exception as exc:
        raise ConfigurationError(f"logger durable state unavailable: {type(exc).__name__}") from exc
    print(f"effective_destinations: {snapshot.effective_destinations}")
    print(f"active_destinations: {snapshot.active_destinations}")
    print(f"unreachable_destinations: {snapshot.unreachable_destinations}")
    print(f"forbidden_destinations: {snapshot.forbidden_destinations}")
    print(f"disabled_destinations: {snapshot.disabled_destinations}")
    print(f"pending_effects: {snapshot.pending_effects}")
    print(f"retryable_effects: {snapshot.retryable_effects}")
    print(f"uncertain_effects: {snapshot.uncertain_effects}")
    print(f"terminal_effects: {snapshot.terminal_effects}")
    print(f"oldest_pending_age_seconds: {snapshot.oldest_pending_age_seconds}")


async def _probe_bot_identity(settings: Settings) -> tuple[bool, str | None]:
    if settings.telegram.token() in {"", "CHANGE_ME"}:
        return False, None
    from telegram_media_bot.telegram.bot_factory import create_bot

    bot = create_bot(settings)
    try:
        identity = await bot.get_me()
        username = getattr(identity, "username", None)
        return True, f"@{username}" if username else None
    except Exception:
        return False, None
    finally:
        await bot.session.close()


async def _probe_channel_access(settings: Settings, chat_id: int) -> bool:
    from telegram_media_bot.telegram.bot_factory import create_bot

    bot = create_bot(settings)
    try:
        identity = await bot.get_me()
        member = await bot.get_chat_member(chat_id, identity.id)
        status = getattr(member.status, "value", str(member.status))
        return status in {"creator", "administrator"}
    except Exception:
        return False
    finally:
        await bot.session.close()


async def _config_edit_telegram_status(settings: Settings, *, probe: bool) -> None:
    token = settings.telegram.token()
    configured = token not in {"", "CHANGE_ME"}
    print(f"bot_token: {'configured' if configured else 'not configured'}")
    admins = settings.telegram.admin_ids
    print(f"admin_ids: {','.join(str(item) for item in admins) if admins else '(none)'}")
    support = settings.telegram.support_username
    print(f"support_username: {support if support else '(none)'}")
    print("api_mode: " + ("local" if settings.telegram.local_api_is_local else "cloud"))
    local = settings.telegram.local_bot_api
    print(f"local_bot_api: {'enabled' if local.enabled else 'disabled'}")
    required = settings.telegram.required_channels
    print(
        "required_channels: "
        f"{'enabled' if required.enabled else 'disabled'} ({len(required.channels)} channel(s))"
    )
    logger = settings.telegram.logger
    print(f"logger: {'enabled' if logger.enabled else 'disabled'}")
    print(f"polling_timeout_seconds: {settings.telegram.polling_timeout_seconds}")
    if probe:
        if not configured:
            print("connection: skipped (token not configured)")
        else:
            reachable, username = await _probe_bot_identity(settings)
            print(f"connection: {'OK' if reachable else 'FAIL'}")
            print(f"bot_username: {username if username else 'unknown'}")
    else:
        print("connection: (not probed)")


async def _config_edit_channel_status(settings: Settings, *, probe: bool) -> None:
    channels = settings.telegram.required_channels.channels
    if not channels:
        print("channels: (none)")
        return
    for channel in channels:
        print(f"channel {channel.chat_id}: {channel.title} ({channel.join_url})")
    if not probe:
        return
    reachable, _username = await _probe_bot_identity(settings)
    if not reachable:
        for channel in channels:
            print(f"channel {channel.chat_id}: probe failed (bot unreachable)")
        return
    for channel in channels:
        accessible = await _probe_channel_access(settings, channel.chat_id)
        print(f"channel {channel.chat_id}: {'OK' if accessible else 'ACCESS DENIED'}")


def _config_edit_cookie_status(settings: Settings) -> None:
    from telegram_media_bot.domain.cookies import CookieService
    from telegram_media_bot.infrastructure.cookies.manager import NetscapeCookieManager

    path = settings.effective_cookie_file()
    if path is None:
        print("cookie_file: (not configured)")
        return
    if not path.is_file():
        print("cookie_file: configured")
        print("exists: false")
        return
    stat = path.stat()
    print("cookie_file: configured")
    print("exists: true")
    print(f"size_bytes: {stat.st_size}")
    print(f"mode: {oct(stat.st_mode & 0o777)}")
    manager = NetscapeCookieManager(path)
    provider_states = []
    for service in CookieService:
        check = manager.static_health(service)
        provider_states.append(f"{service.value}={check.status.value}")
    print("providers: " + ",".join(provider_states))


def _config_edit_local_api_status(settings: Settings) -> None:
    manager = LocalBotApiManager(settings)
    _print_local_api_status(manager)


async def _run_config_edit(settings: Settings, args: argparse.Namespace) -> None:
    action = args.config_edit_action
    if action == "get":
        _config_edit_get(args, args.key)
    elif action == "set":
        _config_edit_set(args, args.key, args.value)
    elif action in ("list-add", "list-remove"):
        _config_edit_list(args, args.key, args.value, add=action == "list-add")
    elif action == "channel-add":
        _config_edit_channel_add(args, args.chat_id, args.title, args.join_url)
    elif action == "channel-remove":
        _config_edit_channel_remove(args, args.chat_id)
    elif action == "channel-update":
        _config_edit_channel_update(args, args.chat_id, args.title, args.join_url)
    elif action == "channel-status":
        await _config_edit_channel_status(settings, probe=bool(args.probe))
    elif action in ("logger-add", "logger-remove"):
        _config_edit_logger_channel(args, args.chat_id, add=action == "logger-add")
    elif action == "logger-status":
        _config_edit_logger_status(settings)
    elif action == "telegram-status":
        await _config_edit_telegram_status(settings, probe=bool(args.probe))
    elif action == "cookie-status":
        _config_edit_cookie_status(settings)
    elif action == "local-api-status":
        _config_edit_local_api_status(settings)
    else:
        raise ConfigurationError(f"Unknown config-edit action: {action}")
