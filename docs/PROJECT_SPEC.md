# Project specification

## Goal

Provide a Telegram bot that receives supported public media URLs, inspects them through a generic
media engine, lets the user choose a semantic output mode, queues the work, downloads safely in a
separate worker, uploads the result, and cleans temporary data.

## Primary non-functional goal

`yt-dlp` must be replaceable and updatable without spreading upstream types, options, exceptions,
or metadata dictionaries throughout the codebase.

## Initial supported-source policy

The operator enables sources in `config.yaml`. The initial example enables YouTube, SoundCloud,
Instagram, Twitter/X, Pinterest, and TikTok. This is a policy list, not a set of dedicated handlers.
Actual extraction support is determined by the installed `yt-dlp` version.

## User flow target

1. User sends one URL.
2. Bot validates access policy and enqueues metadata inspection.
3. Bot displays normalized title, duration, source, and semantic format buttons.
4. User chooses output mode.
5. Worker downloads into an isolated job directory.
6. Bot or worker reports progress without excessive Telegram edits.
7. Result is uploaded using the most suitable Telegram method.
8. Job state is persisted sufficiently for retries and operator inspection.
9. Temporary files are deleted according to policy.

The v1 implementation provides the complete two-step inspection, semantic selection, durable job,
progress/cancellation, delivery, and cleanup flow described above.

## Required operational behavior

- One-command Docker Compose startup after local config creation.
- All secrets in ignored local YAML configuration.
- Separate bot and download worker processes.
- Bounded concurrency, retries, timeouts, size limits, and rate limits.
- Clean shutdown and recovery after restart.
- Structured logs with correlation/job IDs.
- Controlled dependency updates and rollback through Git/lockfile.
- Unit tests by default; opt-in external contract tests.

## Out of scope unless separately approved

- DRM circumvention;
- arbitrary shell execution or user-controlled yt-dlp options;
- downloading local files or private-network URLs;
- automatic startup-time dependency self-updates;
- modifying the upstream yt-dlp source tree;
- guaranteed support for every upstream extractor;
- disguising alternate-source downloads as direct Spotify downloads.
