# Changelog

## Unreleased

## 1.0.3 - 2026-07-27

- Prevented the updater from replacing its own executing script, added mandatory full-script Bash
  syntax validation, and restored executable modes plus the verified global `tmb` symlink after
  release extraction.
- Made updates transactional: validate staged scripts/Compose/config before stopping services,
  replace application entries through a rollback snapshot, and restore the prior source, image,
  usable permissions, command link, and service set after any post-stop failure.
- Repaired SQLite/data ownership and private modes using the resolved runtime UID/GID, then added a
  real runtime-user write probe and SQLite `PRAGMA journal_mode = WAL` probe before service start.
- Added bounded container restart attempts and post-start Bot/Worker/Local API health verification;
  a crash/restart state now stops the affected service and rolls the update back.
- Added filesystem-level and privileged Docker upgrade regression coverage for lost archive modes,
  real SQLite/WAL access, preserved config/state/media, command repair, and rollback behavior.

## 1.0.2 - 2026-07-27

- Made user cancellation durable and idempotent across SQLite and ARQ: cancelled queued/running/
  retrying jobs are never recovered, official ARQ abort is enabled, stale transient keys are
  finalized, and shutdown races no longer requeue an already user-cancelled FFmpeg job.
- Added conservative FFmpeg controls (two encoder threads, one concurrent transcode, 25-minute
  timeout, an operator disable switch), process-tree termination without orphan FFmpeg processes,
  structured transcode progress, and optional `TMB_WORKER_CPUS`.
- Made `tmb update` normalize database and Local Bot API ownership/restrictive permissions before
  restart, repair the global `tmb` command, and leave services stopped with the prior image restored
  if permission migration fails. Existing configuration, `.env`, SQLite/Redis state, cookies,
  downloads, and Local Bot API state remain preserved during the v1.0.1-to-v1.0.2 upgrade.
- Guaranteed compatible `7zz`/`7z` commands in the runtime image and added a real multipart archive
  smoke test to CI and the release workflow.
- Shared the expensive Telegram Local Bot API BuildKit stage between CI and release builds while
  retaining full runtime-image, Compose, dependency-doctor, and multipart smoke validation.

## 1.0.1 - 2026-07-27

- Prevented Instagram `best_original` VP9/MP4 downloads from being re-encoded toward the upload
  ceiling; original mode is now always native-only in durable jobs, queue payloads, and engine
  requests.
- Made Instagram `force_mp4` select and merge native MP4 video plus M4A audio, while disabling it
  preserves the source-selected container.
- Separated native-container, inline-video, and document compatibility and route non-streamable
  native MP4 files to document delivery without changing their source bytes.
- Changed forced video conversion to quality-first CRF encoding with bitrate-limited fallback only
  for actual ceiling or anti-inflation violations, with structured source/target/size logging.
- Prevented a several-megabyte Instagram input from expanding toward the configured upload ceiling
  solely because that ceiling is large.

## 1.0.0 - 2026-07-23

- Added queued metadata inspection and owner-bound expiring semantic format selection.
- Added progress throttling, cancellation, active-job deduplication, classified retries, and safe
  uncertain-delivery handling.
- Added public-network URL/DNS enforcement, Redis rate limiting, durable blocks, and admin commands.
- Added typed audio/video/document delivery with fallback, sanitization, upload limits, local Bot API
  support, bounded playlist ZIPs, ffmpeg, and pinned Deno.
- Added SQLite/WAL job persistence, restart recovery, scheduled cleanup, structured redacted logs,
  health/readiness, Prometheus metrics, controlled upgrades, canary comparison, and plugin SDK.
- Added fail-fast cross-platform release scripts, secret scanning, and dependency vulnerability
  auditing; upgraded pytest to its fixed 9.x line.
- Kept generic inspection size estimates advisory while enforcing configured limits on the selected
  download and final post-processed file.
- Prevented partial audio-only delivery for oversized video selections by choosing the best complete
  configured video/audio pair below the aggregate size limit.
- Added a dedicated configurable Telegram file-upload timeout instead of the 60-second general
  aiogram session default.
- Preserve distinct requested video resolutions with SDR source selection and bounded H.264/AAC
  transcoding when the native result exceeds Telegram's upload ceiling.
- Added managed/external Telegram Local Bot API lifecycle, config-only credentials, explicit
  idempotent migration/rollback, shared Bot/Worker endpoint leases, safe diagnostics, and a
  practical 1900 MB upload ceiling without forced transcoding below that ceiling.

## 0.1.0 - 2026-07-23

- Added the initial layered project foundation.
- Added strict local YAML configuration.
- Added isolated yt-dlp engine adapter.
- Added aiogram bot, ARQ worker, Redis Compose service, and management scripts.
- Added Codex implementation specifications, security rules, tests, and CI.

## 0.1.1 - 2026-07-23

- Changed the Python baseline from 3.12 to Python 3.14 or newer.
- Added configurable Docker `PYTHON_VERSION` through `.env`.
- Updated CI, uv lock helper images, Ruff, mypy, documentation, and Codex instructions.
