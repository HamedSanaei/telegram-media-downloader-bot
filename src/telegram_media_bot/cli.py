from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

from arq.worker import run_worker

from telegram_media_bot.bootstrap.config import load_settings
from telegram_media_bot.bootstrap.logging import configure_logging
from telegram_media_bot.domain.errors import ConfigurationError


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
            print(json.dumps(settings.model_dump(mode="json"), ensure_ascii=False, indent=2))
        elif args.command == "doctor":
            settings = load_settings(args.config, require_token=False)
            _run_doctor(settings)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _run_doctor(settings: object) -> None:
    del settings
    checks = {
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
    }
    failed = False
    for name, path in checks.items():
        if path:
            print(f"OK   {name}: {path}")
        else:
            failed = True
            print(f"FAIL {name}: not found")
    if failed:
        raise SystemExit(1)
