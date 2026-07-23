# Changelog

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
