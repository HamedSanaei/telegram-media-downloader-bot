# Changelog

## Unreleased

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
