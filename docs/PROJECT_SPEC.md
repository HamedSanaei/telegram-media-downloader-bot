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
2. Bot validates access policy, canonicalizes a YouTube URL with a valid video ID to single-video
   intent (removing Mix/playlist context), and enqueues metadata inspection.
3. Bot displays normalized title, duration, source, and only semantic formats that are actually
   selectable, including selected resolution/FPS/HDR and exact, estimated, or unknown size.
4. For ordinary video, the public UI exposes only zero-transcode AV1/AAC or H.264/AAC MP4 and
   native VP9/Opus WebM,
   followed by unique qualities derived from the actual selected streams. Converted/generic
   video policies remain internal and cannot be reached by current or legacy callbacks. Instagram
   video posts, Reels, Stories, and Highlights skip both prompts and use the best original
   streams; `media.instagram.force_mp4` may constrain the native video/audio pair to MP4 + M4A
   without changing codecs.
5. Worker downloads into an isolated job directory.
6. Telegram and worker logs report throttled download/upload progress. Upload percentage covers
   bytes read into Local Bot API; the opaque Telegram phase reports only elapsed-time heartbeats.
7. Result is uploaded using the most suitable Telegram method.
8. Job state is persisted sufficiently for retries and operator inspection.
9. After every terminal outcome, the worker removes that job's media, archive volumes, sidecars,
   `.part` files, and temporary files from both workspace roots. Confirmed multipart parts are
   removed immediately after their durable Telegram receipt.

Configured administrators receive a persistent management keyboard from `/start` or `/menu`.
Its download prompt is only an alternate entry point to the same URL validation, inspection,
selection, queue, worker, delivery, and cleanup flow used by every user. Weekly/monthly PNG and
complete text reports exclude administrator IDs from public KPIs at query time without deleting
their durable jobs or usage events.

For explicitly enabled large-file delivery, results up to the Local Bot API ceiling are uploaded
directly. Every larger result is emitted as bounded multi-volume ZIP documents through the
configured 4096 MB aggregate ceiling.

The v1 implementation provides the complete two-step inspection, semantic selection, durable job,
progress/cancellation, delivery, and cleanup flow described above.

Every pre-enqueue selection page has deterministic Back navigation. It edits the existing Telegram
message and reuses the owner-bound persisted inspection; Back never repeats yt-dlp inspection,
creates a durable job, or enqueues Redis work.

YouTube `watch`, `youtu.be`, `shorts`, and `live` URLs containing a valid video ID always mean one
video unless the user enters an explicit playlist action. Mix parameters do not turn them into a
playlist. Genuine `/playlist?list=...` URLs retain the configured bounded-playlist policy.

## Required operational behavior

- One-command Docker Compose startup after local config creation.
- Optional fail-closed membership in every configured channel before protected operations.
- An optional HTTP(S)/SOCKS proxy scoped strictly to yt-dlp source traffic.
- Permanent user profiles and idempotent daily/success/failure/byte usage counters in SQLite.
- All secrets in ignored local YAML configuration.
- Separate bot and download worker processes.
- Bounded concurrency, retries, timeouts, size limits, and rate limits.
- Clean shutdown and recovery after restart.
- Zero-retention cleanup for successful, failed, cancelled, timed-out, and delivery-uncertain jobs,
  with a startup/maintenance sweeper for terminal and abandoned workspaces.
- Release updates may remove only unreferenced old images from this project's GHCR repository after
  candidate health/version/doctor verification; they never perform a global Docker prune.
- Structured logs with correlation/job IDs.
- Role-authorized management messages/callbacks, single-flight report rendering, and no
  administrator identity or media metadata in usage reports.
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
