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
- Pillow with a package-bundled Noto Sans font for deterministic in-memory administrator charts;
- Docker Compose startup after one ignored local YAML configuration is created.

## First run

Production one-line installers:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/HamedSanaei/telegram-media-downloader-bot/main/install.sh)
```

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force; irm https://raw.githubusercontent.com/HamedSanaei/telegram-media-downloader-bot/main/install.ps1 | iex
```

They install/configure the Docker topology and expose the same `tmb` lifecycle menu on Linux and
Windows. See `docs/INSTALLATION.md`.

Developer/local build:

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

The ordinary-video flow is: URL -> queued inspection -> MP4/WebM -> semantic quality -> durable
download -> throttled progress/cancel -> audio/video/document delivery -> cleanup. Instagram video
posts, Reels, Stories, Highlights, and multi-video collections automatically use best MP4 and
deliver videos separately.

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
uv run python scripts/check_package_assets.py --install-smoke
docker build -t telegram-media-downloader-bot:review .
```

The usage-chart font and its SIL OFL 1.1 license are shipped inside the Python package. Runtime
rendering does not download assets or consult system fonts, fontconfig, a display server, or an
external chart API. CI publishes deterministic weekly and monthly fixture charts as the
`usage-chart-smoke` artifact.

External contract tests are opt-in and require operator-maintained safe public fixtures. See
`docs/OPERATIONS.md` for upgrades, canary promotion, rollback, alert thresholds, and incident
diagnosis. See `docs/CONFIGURATION.md` for every runtime option and `docs/LOCAL_BOT_API.md` for
managed/external Local Bot API setup, explicit migration, rollback, and files up to 1900 MB.
See `docs/MULTIPART_DELIVERY.md` for `1440p`, `2160p`, `best_original`, and multi-volume ZIP
delivery for every result above the direct upload limit through 4096 MB.

## Intentional boundaries

The v1 supported topology is one worker container with bounded internal concurrency. The official
Local Bot API executable is built from pinned upstream source in the Docker image. Spotify,
Castbox, DRM circumvention, local/private media URLs, startup self-updates, Userbot/MTProto
automation, and user-controlled yt-dlp options are not implemented.
