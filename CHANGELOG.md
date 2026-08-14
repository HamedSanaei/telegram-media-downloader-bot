# Changelog

## Unreleased

## 1.2.2 - 2026-08-14

### Fixed

- Validate an update with the prepared release image before stopping services, mounting the
  existing configuration and persistent `/data` filesystem read-only so runtime-valid cookie
  paths remain visible to gallery-dl checks under the configured runtime UID/GID.
- Add a non-mutating `config-check --read-only-runtime` mode for updater preflight. Cookie
  readability remains fail-closed, while Local Bot API directory validation does not write to
  SQLite, migration, download, cookie, or other persistent runtime state.
- Publish a checksummed standalone updater asset for the one-time v1.2.1 bootstrap, whose installed
  updater necessarily runs its old preflight before it can replace application files.
- Preserve the existing owner and restrictive mode of `.env` when an elevated updater changes the
  pinned image, and align privileged fixtures with the installer's UID/GID contract.
- Cover default Instagram fallback, explicit gallery-dl cookies, missing and unreadable files,
  disabled gallery-dl, byte-identical configuration/cookies, and previous-release updater layouts.

## 1.2.1 - 2026-08-13

### Fixed

- Resolve mixed Instagram carousel videos from yt-dlp's raw, unprocessed parent entries so photo
  children with no video formats cannot abort discovery before a valid video child is reached.
- Require the raw yt-dlp entry count and every detected video source ordinal to match gallery-dl's
  complete media plan before downloading any gallery image; then download only the validated
  public Instagram video-child URLs through the existing yt-dlp adapter.
- Preserve strict failure semantics without enabling yt-dlp `ignoreerrors`, and retain all source
  order, transient-URL isolation, cleanup, Twitter HLS, and exact-byte image behavior from 1.2.0.

## 1.2.0 - 2026-08-13

### Added

- Ask users whether Instagram images should be delivered as Telegram photos or byte-preserving
  documents before creating the download job; the typed choice survives SQLite, ARQ, worker
  recovery, download planning, and delivery.
- Deliver ordered Instagram carousels in deterministic groups of at most ten. Photo mode uses
  photo/video albums; document mode uses ordered document and video runs so no item is converted,
  reordered, or dropped by Telegram media-group type restrictions.
- Split mixed Instagram posts across the existing isolated engines: gallery-dl downloads only the
  original image assets while yt-dlp downloads every video from the canonical post URL. Exact
  source ordinals are reconciled fail-closed before delivery.
- Notify every uniquely configured Telegram administrator when an inspection or download reaches
  a final failure or uncertain-delivery state. Alerts expose only the opaque job ID, job kind,
  normalized source, terminal status, stable error category, and attempt number.

### Operations

- Reverified zero-retention cleanup for successful, failed, cancelled, timed-out, and
  delivery-uncertain workspaces, including both download and temporary job directories. Durable
  job records continue to follow the configured retention period.
- Keep administrator alert failures isolated from job state, user notifications, other
  administrators, and workspace cleanup; intermediate retries and user cancellations do not alert.

### Security

- Keep gallery-dl CDN and `ytdl:` pseudo-URLs transient, preserve signature/decompression and
  workspace checks, and never rewrite validated production image bytes.

## 1.1.1 - 2026-08-12

### Fixed

- Explicitly request gallery-dl JSON Lines for every machine-consumed inspection and strictly parse
  directory and URL message tuples instead of relying on the default pretty-printed outer array.
- Recognize gallery-dl `ytdl:` video events without persisting their pseudo-URLs, allowing
  video-only Instagram Reels to fall back to yt-dlp while image and mixed posts remain gallery-owned.

## 1.1.0 - 2026-08-01

### Added

- Added original image and ordered mixed-media downloads for single Instagram, TikTok, Twitter/X,
  and Pinterest posts through an isolated, exactly pinned gallery-dl 1.32.8 subprocess adapter.
- Added semantic single-image, all-images, all-media, images-only, videos-only, and original-image
  ZIP choices; YouTube thumbnails and SoundCloud artwork remain on the yt-dlp path.
- Added signature-based Pillow image validation, chunked Telegram photo/mixed albums, deterministic
  document fallback, and reuse of the existing multipart archive path for oversized ZIPs.
- Added per-source cookie settings with backward-compatible Instagram cookie fallback, runtime
  health/doctor/config checks, sanitized 1.32.8 fixtures, dependency-upgrade contract tooling,
  package/Docker license smokes, and isolated Renovate updates.

### Security

- Reject bulk social URLs, bound asset count/bytes/runtime/concurrency/output, terminate gallery-dl
  process groups on cancellation, confine outputs to the job workspace, and persist no signed CDN
  URL or raw vendor metadata.

### Fixed

- Preserve the current Twitter/X HLS audio-only metadata inference and exact H.264/AAC inspected
  stream IDs through durable selection and stream-copy download.
- Classify an empty native codec/container plan separately from deleted/private media, retain the
  normalized source on planning failure, and emit URL-free structured diagnostics.

## [1.0.11] - 2026-08-01

### Fixed

- Fixed admin media requests getting stuck after inspection was queued.
- Separated the persistent admin reply keyboard from editable inspection status messages.
- Added a fallback message when Telegram cannot edit an inspection status message.
- Reconciled existing inspection and Instagram download jobs with ARQ when Redis state is missing.

## 1.0.10 - 2026-07-30

- Fixed blank chart titles and labels by replacing the raw-pixel renderer with an in-memory Pillow
  dashboard containing the report title, exact date range, timezone, KPI labels/values, readable
  legend, numeric Y axis, adaptive date labels, and important bar values.
- Bundled deterministic Noto Sans and its SIL OFL 1.1 license inside the wheel, sdist, and
  production image. Rendering never downloads a font or depends on host font packages/fontconfig.
- Added weekly/monthly visual-region tests, clean-wheel resource validation, `tmb doctor` font and
  renderer checks, and non-root/read-only/offline Docker chart smoke artifacts.
- Preserved media zero-retention and all administrator, required-channel, configuration, and
  durable-state behavior unchanged.

## 1.0.9 - 2026-07-30

- Give configured administrators a persistent role-aware management keyboard from `/start`,
  `/menu`, or the backward-compatible `/panel`, with authorization repeated for every management
  message and report callback.
- Route both the admin download prompt and direct admin URLs through the unchanged public
  inspection, selection, queue, worker, delivery, cancellation, and zero-retention pipeline.
- Add single-flight weekly/monthly PNG reports and a complete usage report with Tehran-local daily
  breakdowns, while excluding current administrator IDs from public KPIs without deleting durable
  activity records.

## 1.0.8 - 2026-07-29

- Enforce job-scoped zero-retention after confirmed delivery, failure, cancellation, timeout, and
  uncertain delivery; cleanup is idempotent, symlink-safe, observable, and repeated by startup and
  maintenance sweepers without touching active jobs or shared state.
- Delete each delivered multipart volume immediately after its Telegram receipt is persisted,
  terminate active 7-Zip process groups on cancellation, and retain no media/archive workspace
  after a terminal job.
- Add safe `tmb cleanup [--dry-run]` workspace/container/image cleanup and remove only unused old
  images from `ghcr.io/hamedsanaei/telegram-media-downloader-bot` after successful health, runtime
  version, doctor, and status verification. Referenced/current images, other repositories, volumes,
  and global build caches are never pruned.

## 1.0.7 - 2026-07-29

- Canonicalize YouTube watch, short, live, and `youtu.be` video URLs containing Mix or playlist
  parameters before persistence, queueing, inspection, and download.
- Force single-video inspection and download with yt-dlp `noplaylist` whenever a valid YouTube
  video ID is present, while preserving genuine `/playlist?list=...` behavior.
- Prevent unnecessary YouTube Mix expansion and its associated Deno/CPU cost for links targeting
  one video, including retry, recovery, and legacy persisted-job execution.

## 1.0.6 - 2026-07-29

- Expanded MP4 Native to include real AV1/AAC and H.264/AAC plans; Native is now defined by zero
  video/audio transcoding, with stream-copy merge/remux remaining permitted.
- Added truthful codec-aware option labels and kept AV1 and H.264 plans distinct when they share a
  resolution. Selected codec families remain durable across SQLite, Redis, restart recovery, and
  download-time validation.
- Made the Best Original summary point to an actually selectable Native plan and report that
  plan's resolution, container, codec, and selected-stream size.
- Route non-inline AV1 MP4 through document delivery without re-encoding; runtime-image smoke tests
  assert both native AV1 and H.264 visibility, zero transcoding, no `libx264`, and stream-copy args.

## 1.0.5 - 2026-07-29

- Made every public video choice native-only: Telegram now exposes only H.264/AAC MP4 Native and
  VP9/Opus WebM Native options, while generic MP4/WEBM and every video plan requiring transcoding
  remain hidden.
- Added deterministic Back navigation that reuses the persisted inspection selection without
  creating another inspection, SQLite job, or Redis enqueue.
- Built labels from actual selected resolution, FPS, dynamic range, codecs, and selected-stream
  sizes; exact, approximate, and unknown sizes remain distinguishable.
- Deduplicated requested qualities and Best Original when they resolve to the same real streams,
  so lower-resolution native fallbacks cannot create false 2160p/1440p buttons.
- Versioned callback payloads use short opaque option identities. Legacy generic or converted
  callbacks are rejected safely and return users to the Native menu without starting FFmpeg.

## 1.0.4 - 2026-07-28

- Prepared patch `1.0.4`: ordinary MP4 choices now rank native H.264/AVC + AAC ahead of
  bitrate/quality, may deterministically fall back to a lower compatible resolution, and never
  hide an AV1/VP9-to-H.264 transcode.
- Added an opt-in, disabled-by-default converted-MP4 choice with a conservative preflight timeout
  estimate; native WebM and `best_original` remain codec-preserving.
- Added structured native-selection/fallback logging and deterministic production-like fixtures
  for MP4, WebM, stream-copy, timeout rejection, and legacy callback/config compatibility.

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
