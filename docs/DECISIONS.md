# Architecture decision record

## ADR-001: Python 3.14 baseline with no artificial upper bound

**Status:** accepted

Use Python 3.14, the latest stable CPython generation selected for this project. The project requires
`>=3.14` and intentionally has no artificial upper bound, so a newer installed stable Python can be
adopted after the quality gates pass. Docker defaults to Python 3.14 through the configurable
`PYTHON_VERSION` build argument. Preview and beta interpreters are not production defaults.

## ADR-002: Embed yt-dlp through one adapter

**Status:** accepted

Use the Python embedding API, but permit imports only in `infrastructure/ytdlp`. Upstream metadata is
sanitized and mapped immediately to project models.

## ADR-003: Bot and worker are separate processes

**Status:** accepted

Use aiogram for polling and ARQ/Redis for asynchronous jobs. Downloads must not block the Telegram
polling process.

## ADR-004: YAML is the local operator configuration

**Status:** accepted

Secrets and runtime settings are stored in ignored `config.yaml`. Pydantic validates the file with
unknown keys forbidden. Environment variables may only select the config path.

## ADR-005: uv lockfile controls updates

**Status:** accepted

Do not self-update yt-dlp at startup. Update the uv lock entry, run adapter and project tests, then
rebuild. Roll back by reverting the update commit.

## ADR-006: Initial worker uploads results

**Status:** provisional

For the starter, the worker uses the Telegram token to upload the completed file. A later task may
introduce a delivery port or local Bot API without changing the download-engine boundary.
