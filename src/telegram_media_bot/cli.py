from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from arq.worker import run_worker

from telegram_media_bot.bootstrap.config import Settings, load_settings
from telegram_media_bot.bootstrap.logging import configure_logging
from telegram_media_bot.domain.errors import ConfigurationError, LocalBotApiError
from telegram_media_bot.infrastructure.telegram.local_api import LocalBotApiManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="telegram-media-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bot = subparsers.add_parser("bot", help="Run Telegram polling process")
    bot.add_argument("--config", type=Path, default=None)

    worker = subparsers.add_parser("worker", help="Run ARQ download worker")
    worker.add_argument("--config", type=Path, default=None)

    config_check = subparsers.add_parser("config-check", help="Validate configuration")
    config_check.add_argument("--config", type=Path, default=None)

    doctor = subparsers.add_parser("doctor", help="Check local runtime prerequisites")
    doctor.add_argument("--config", type=Path, default=None)

    local_api = subparsers.add_parser("local-api", help="Manage Telegram Local Bot API")
    local_api.add_argument("--config", type=Path, default=None)
    local_actions = local_api.add_subparsers(dest="local_api_action", required=True)
    local_actions.add_parser("status", help="Show safe Local Bot API status")
    local_actions.add_parser("start", help="Start managed Local Bot API")
    local_actions.add_parser("stop", help="Stop managed Local Bot API")
    migrate_local = local_actions.add_parser(
        "migrate-to-local", help="Explicitly migrate the bot from cloud to local"
    )
    migrate_local.add_argument("--yes", action="store_true", help="Confirm non-interactively")
    migrate_cloud = local_actions.add_parser(
        "migrate-to-cloud", help="Explicitly migrate the bot from local to cloud"
    )
    migrate_cloud.add_argument("--yes", action="store_true", help="Confirm non-interactively")

    return parser


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
                import os

                os.environ["APP_CONFIG_PATH"] = str(args.config)
            settings = load_settings(args.config, require_token=True)
            configure_logging(settings)
            from telegram_media_bot.workers.settings import WorkerSettings

            run_worker(WorkerSettings)
        elif args.command == "config-check":
            settings = load_settings(args.config, require_token=False)
            _run_config_check(settings)
            print("Configuration is valid.")
        elif args.command == "doctor":
            settings = load_settings(args.config, require_token=False)
            _run_doctor(settings)
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
    except (ConfigurationError, LocalBotApiError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _run_doctor(settings: Settings) -> None:
    javascript_runtime = settings.yt_dlp.javascript_runtime
    executable = "qjs" if javascript_runtime == "quickjs" else javascript_runtime
    checks = {
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        javascript_runtime: shutil.which(executable),
    }
    print(f"OK   python: {sys.version.split()[0]}")
    from telegram_media_bot.infrastructure.ytdlp.engine import YtDlpEngine

    engine_health = YtDlpEngine(settings).health()
    print(f"OK   {engine_health.name}: {engine_health.detail}")
    failed = False
    for name, path in checks.items():
        if path:
            print(f"OK   {name}: {_binary_version(path)}")
        else:
            failed = True
            print(f"FAIL {name}: not found")
    local_api = settings.telegram.local_bot_api
    if local_api.enabled:
        diagnostics = _local_api_diagnostics(settings, require_reachable=True)
        for name, healthy in diagnostics.items():
            print(f"{'OK  ' if healthy else 'FAIL'} local_api_{name}")
            failed = failed or not healthy
    if failed:
        raise SystemExit(1)


def _run_config_check(settings: Settings) -> None:
    local_api = settings.telegram.local_bot_api
    if not local_api.enabled:
        return
    diagnostics = _local_api_diagnostics(settings, require_reachable=False)
    failed = [name for name, healthy in diagnostics.items() if not healthy]
    if failed:
        raise ConfigurationError(
            f"Local Bot API configuration checks failed: {', '.join(sorted(failed))}"
        )


def _local_api_diagnostics(
    settings: Settings,
    *,
    require_reachable: bool,
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
    directories_ok = all(_directory_writable(path) for path in directories)
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
    reachable_ok = manager.endpoint_reachable() if require_reachable else True
    return {
        "configuration": endpoint_ok,
        "credentials": credentials_ok,
        "directories": directories_ok,
        "executable": executable_ok,
        "migration": migration_ok,
        "reachable": reachable_ok,
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
    first_line = (completed.stdout or completed.stderr).splitlines()
    version = first_line[0][:200] if first_line else "version unavailable"
    return f"{path} ({version})"
