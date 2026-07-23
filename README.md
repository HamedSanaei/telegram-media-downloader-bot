# Telegram Media Downloader Bot

A production-oriented starter project for a Telegram media downloader whose media engine is
`yt-dlp`, but whose application code is isolated from `yt-dlp` internals.

The repository is intentionally a **foundation**, not a falsely complete product. It already
contains a working configuration system, a Telegram bot process, an ARQ/Redis worker process,
a typed download-engine port, a `yt-dlp` adapter, Docker Compose, tests, CI, and detailed Codex
implementation instructions.

## Architectural promise

Only `src/telegram_media_bot/infrastructure/ytdlp/` may import `yt_dlp`.
Telegram handlers, queue jobs, and application services only use project-owned models and ports.
Therefore, future `yt-dlp` updates should normally require either no application changes or a
small adjustment inside the adapter directory.


## Python version policy

The project now targets Python 3.14 or newer. Local `uv` commands use the project pin in
`.python-version`, currently `3.14`, so a matching Python 3.14 installation already present on the
system is reused. Docker cannot reuse the host interpreter directly; its Python generation is
controlled by `PYTHON_VERSION` in the local `.env` file and also defaults to `3.14`.

Python 3.15 beta or release-candidate builds are not selected as production defaults. When a newer
stable Python generation is approved, update `.python-version` and `PYTHON_VERSION`, regenerate the
lockfile, and run all quality gates.

## First run

1. Create the local configuration:

```bash
./manage.sh init
```

2. Edit `config.yaml` and set `telegram.bot_token`.

3. Start the full stack. If `uv.lock` is absent, the management script generates it once before building:

```bash
./manage.sh up
```

Windows PowerShell:

```powershell
.\manage.ps1 init
.\manage.ps1 up
```

The stack contains:

- `bot`: receives Telegram updates and enqueues jobs;
- `worker`: downloads through `YtDlpEngine` and uploads the result;
- `redis`: queue and transient job state.

## Local development

```bash
uv sync --frozen --group dev
uv run telegram-media-bot config-check --config config.example.yaml
uv run pytest -m "not contract"
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
```

## Codex continuation

Open [`docs/CODEX_EXECUTION.md`](docs/CODEX_EXECUTION.md). The ready-to-paste prompt is in
[`PROMPT_FOR_CODEX.md`](PROMPT_FOR_CODEX.md). Codex must read `AGENTS.md` before changing code.

## Current scope

The starter accepts a URL, enqueues a default download, downloads it in a worker process, and
sends the resulting file as a Telegram document. Quality-selection UI, progress editing,
persistent job history, playlists, richer media sending, admin controls, and source-specific
policies are deliberately specified in the roadmap for the next implementation pass.

## Important files

- `AGENTS.md`: binding implementation rules for coding agents;
- `docs/ARCHITECTURE.md`: boundaries and data flow;
- `docs/PROJECT_SPEC.md`: product requirements;
- `docs/ROADMAP.md`: implementation milestones;
- `docs/ACCEPTANCE_CRITERIA.md`: definition of done;
- `config.example.yaml`: all local runtime configuration and secret placeholders;
- `manage.sh` / `manage.ps1`: one-command operations.
