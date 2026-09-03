# Telegram Media Downloader Bot

A production-oriented Telegram bot that inspects public media URLs through isolated yt-dlp and
gallery-dl adapters, offers semantic video/image/bundle formats, downloads in an ARQ worker,
delivers through a typed Telegram adapter, persists state, and cleans every job directory.

Everything an operator needs is managed with one command:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/HamedSanaei/telegram-media-downloader-bot/main/install.sh)
```

then:

```bash
tmb
```

`tmb` is the single operator control plane: an interactive menu **and** a scriptable command line.
You should almost never need to edit YAML, run raw Docker commands, or remember internal
application commands.

## What it does

- Inspects public media URLs in a worker process (never in the Telegram polling loop) using
  yt-dlp and gallery-dl behind project-owned adapters.
- Delivers semantic quality choices (`best`, `720p`, `audio_mp3`, ...) as MP4/WebM, images, and
  multi-volume ZIP bundles, with progress and cancellation.
- Persists durable job state in SQLite/WAL with Redis for queueing, and cleans every job directory.
- Supports Cloud Bot API and an opt-in Local Bot API mode (files up to 1900 MB).

## Requirements

- A Linux server (x86_64 or aarch64). Windows is supported via `install.ps1` + `tmb.ps1`.
- Docker Engine with the Compose plugin. The installer can install Docker for you.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).

## Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/HamedSanaei/telegram-media-downloader-bot/main/install.sh)
```

The installer detects an existing installation, verifies the release checksum, installs the
application to `/opt/telegram-media-downloader-bot` by default, links the `tmb` command, and
launches the configuration wizard. After the wizard finishes, run:

```bash
tmb
```

## Managing the bot with tmb

`tmb` opens an interactive menu. Every menu item has the same non-interactive command:

```bash
tmb status        # dashboard: versions, services, disk, Telegram state
tmb logs worker   # logs: tmb logs [bot|worker|local-api|redis] [--tail N] [--since 2h] [errors]
tmb doctor        # health checks
tmb telegram      # bot token, admins, polling (secrets are never echoed)
tmb channels      # required-channel policy
tmb logger        # operator logger destinations and outbox health
tmb local-api     # Local Bot API status/configure/migrate
tmb storage       # disk overview and project-scoped cleanup
tmb backup        # create/list/inspect/verify/delete consistent backups
tmb migration     # export a portable migration bundle / import it on a new server
tmb docker        # project-scoped image and container management
tmb update        # transactional verified updater with automatic rollback
tmb bundle        # sanitized diagnostic support bundle
tmb version       # application version, image, digest
tmb help          # full command reference
```

Backup and restore:

```bash
tmb backup create          # consistent operational backup (config + durable state)
tmb migration export       # portable bundle for moving to a new server
tmb restore --dry-run FILE # validate before touching anything
tmb restore FILE           # transactional restore with automatic rollback
```

Update:

```bash
tmb update
```

updates through the verified transactional updater; if anything fails, the previous release,
image, permissions, and exact service state are restored automatically.

See `docs/MANAGEMENT.md` for the complete `tmb` reference, `docs/INSTALLATION.md` for installer
details, `docs/CONFIGURATION.md` for every runtime option, and `docs/OPERATIONS.md` for upgrades,
rollback, alerting, and incident diagnosis.

## Architecture promise

Only `src/telegram_media_bot/infrastructure/ytdlp/` imports `yt_dlp` inside the application. Raw
upstream dictionaries, exceptions, format IDs, and hooks never cross that adapter. Telegram
handlers do no media extraction or download work. Only
`src/telegram_media_bot/infrastructure/telegram/mtproto/` may import telethon, and no
Userbot/MTProto automation is implemented. The external plugin SDK is a separate distribution
below `plugins/`, as required by yt-dlp's plugin namespace.

## For developers and contributors

Local development uses `uv` and `./manage.sh`:

```bash
./manage.sh init
# set telegram.bot_token and operator policy in config.yaml
./manage.sh config-check
./manage.sh up
```

Release gates:

```bash
uv lock --check
uv sync --frozen --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest -m "not contract" --cov=telegram_media_bot --cov-report=term-missing
uv build
```

See `docs/ARCHITECTURE.md`, `docs/CODE_MAP.md`, and `docs/DECISIONS.md`. The `tmb` control plane
lives in `scripts/tmb.sh` + `scripts/lib/` and is tested by `scripts/tests/test_tmb.sh` and
`scripts/tests/test_tmb_update.sh`.

## Intentional boundaries

The v1 supported topology is one worker container with bounded internal concurrency. The official
Local Bot API executable is built from pinned upstream source in the Docker image. Spotify,
Castbox, DRM circumvention, local/private media URLs, startup self-updates, Userbot/MTProto
automation, and user-controlled yt-dlp options are not implemented.