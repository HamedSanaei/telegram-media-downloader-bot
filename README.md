# Telegram Media Downloader Bot

A production-oriented Telegram bot that inspects public media URLs through an isolated yt-dlp
adapter, offers operator-configured semantic formats, downloads in an ARQ worker, delivers through a
typed Telegram adapter, persists state, and cleans every job directory.

## Architectural promise

Only `src/telegram_media_bot/infrastructure/ytdlp/` imports `yt_dlp` inside the application. Raw
upstream dictionaries, exceptions, format IDs, and hooks never cross that adapter. Telegram handlers
do no media extraction or download work. The external plugin SDK is a separate distribution below
`plugins/`, as required by yt-dlp's plugin namespace.

## Runtime

- Python 3.14.5 or a newer stable compatible release;
- aiogram polling bot and separate ARQ worker;
- Redis for queue/rate limiting and SQLite/WAL for durable job state;
- ffmpeg/ffprobe and pinned Deno 2.9.3 for yt-dlp EJS;
- Docker Compose startup after one ignored local YAML configuration is created.

## First run

```bash
./manage.sh init
# set telegram.bot_token and operator policy in config.yaml
./manage.sh config-check
./manage.sh up
```

PowerShell:

```powershell
.\manage.ps1 init
.\manage.ps1 config-check
.\manage.ps1 up
```

The user flow is: URL -> queued inspection -> normalized metadata -> semantic inline choice ->
durable download -> throttled progress/cancel -> audio/video/document delivery -> cleanup.

## Development and release gates

```bash
uv lock --check
uv sync --frozen --group dev
uv run python scripts/check_architecture.py
uv run python scripts/check_text_integrity.py
uv run python scripts/generate_file_manifest.py --check
uv run pre-commit run detect-secrets --all-files
uv run pip check
uv run pip-audit --local --skip-editable --progress-spinner off
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest -m "not contract" --cov=telegram_media_bot --cov-report=term-missing
uv build
docker build -t telegram-media-downloader-bot:review .
```

External contract tests are opt-in and require operator-maintained safe public fixtures. See
`docs/OPERATIONS.md` for upgrades, canary promotion, rollback, alert thresholds, and incident
diagnosis. See `docs/CONFIGURATION.md` for every runtime option.

## Intentional boundaries

The v1 supported topology is one worker container with bounded internal concurrency. Local Telegram
Bot API is supported but not bundled. Spotify, Castbox, DRM circumvention, local/private URLs,
startup self-updates, and user-controlled yt-dlp options are not implemented.
