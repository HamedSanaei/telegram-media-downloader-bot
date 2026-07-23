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

## ADR-006: Delivery port with worker-owned upload

**Status:** accepted; supersedes the starter wording

The worker owns delivery but depends on a project `DeliveryGateway` port. The Telegram adapter
selects audio/video/document and supports an operator-selected local Bot API endpoint without
coupling application or download-engine contracts to aiogram.

## ADR-007: SQLite/WAL is the durable local job store

**Status:** accepted

Use one SQLite database below `storage.state_directory` for jobs, selections, cancellation, dynamic
blocks, and delivery receipts. WAL plus short transactions supports the bot and one worker container
concurrently. Redis remains the ARQ/rate-limit backend. Multi-host worker replicas require a future
leased database adapter before enablement.

## ADR-008: Quarantine uncertain Telegram deliveries

**Status:** accepted

Persist `delivering` before calling Telegram and persist returned file IDs immediately after. If a
process exits in that gap or Telegram returns an ambiguous transport failure, transition to
`delivery_uncertain`, include it in idempotency matching, and require operator review. Never retry it
automatically.

## ADR-009: Deno is the pinned yt-dlp JavaScript runtime

**Status:** accepted

Pin Deno 2.9.3 from the official binary image. yt-dlp recommends Deno and the locked
`yt-dlp[default]` dependency supplies `yt-dlp-ejs`. Runtime upgrades follow the reviewed
lock/image/canary process and `doctor` reports the executable version.

## ADR-010: Bounded playlists are delivered as one ZIP document

**Status:** accepted

The stable engine contract returns one final file. When playlist policy is enabled and multiple
files are produced, the adapter verifies aggregate size, creates a ZIP below the job directory,
deletes the individual files, and returns it as `MediaKind.PLAYLIST`.
