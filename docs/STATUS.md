# Project status

Last updated: 2026-07-27

## Release state

Tasks T001 through T012 are implemented. The v1 flow is URL validation -> queued inspection ->
owner-bound semantic selection -> durable download job -> throttled progress/cancellation -> typed
Telegram delivery -> terminal state and cleanup.

## Implemented production controls

- Python 3.14.5, committed `uv.lock`, immutable Docker build, non-root/read-only app containers, and
  pinned Deno 2.9.3 plus ffmpeg.
- Secret scanning and a locked-environment `pip-audit` vulnerability gate in local release checks
  and CI.
- Strict local YAML configuration with schema, path containment, unknown-key rejection, and no
  secret-bearing environment variables.
- Separate aiogram bot and ARQ worker; no download or direct yt-dlp call in polling handlers.
- Project-owned engine, persistence, queue, delivery, URL-validation, and rate-limit contracts.
- SQLite/WAL durable state, active-job deduplication, transition history fields, restart recovery,
  delivery-uncertainty quarantine, dynamic blocks, and scheduled cleanup.
- Public-network URL/DNS enforcement before enqueue and inside the yt-dlp adapter for extracted URLs.
- Semantic format UI, bounded playlist ZIP delivery, media-method fallback, local Bot API support,
  explicit size/duration/playlist limits, and filename/caption sanitization.
- Structured redacted logs, request/job correlation, admin commands, internal health/readiness, and
  bounded-label Prometheus metrics.
- Managed and external Telegram Local Bot API modes with a 1900 MB practical ceiling, config-only
  credentials, explicit durable migration/rollback, shared Bot/Worker endpoint leases, lifecycle
  CLI, and Local API readiness.
- Controlled yt-dlp upgrade reports, per-source opt-in contracts, canary failure-rate gate, and an
  independent external extractor plugin template.
- Fail-closed all-channel membership with Redis positive/negative cache and admin bypass.
- yt-dlp-only HTTP(S)/SOCKS proxy switching with legacy behavior and secret-safe configuration.
- Two-stage MP4/WebM quality selection, codec-verified fallback conversion, automatic best-MP4
  Instagram multi-video delivery, and runtime bot attribution in every caption.
- Permanent SQLite user profiles, daily counters, and job-idempotent delivery byte accounting.
- Docker-first Linux/Windows installers with SHA-256-verified release archives, version-pinned
  images, interactive `tmb` management, official pinned Local Bot API source build, dedicated
  Local API service, and a tag-gated GHCR amd64 release workflow.
- Failed interactive configuration writes remove their secret-bearing temporary file; the exact
  temporary filename is also ignored by Git.
- `tmb update` now preserves Redis/ARQ state, backs up SQLite/WAL only after graceful writer
  shutdown, verifies releases in staging, and restores exactly the previously running application
  services on download/checksum/pull failure.

## Verification

The final exact command results and coverage are recorded in `docs/HANDOFF_REPORT.md` after the last
gate run. External contracts remain opt-in and require operator-maintained public URLs.

## Recent fixes

- 2026-07-27: Fixed Instagram `best_original` inflation by making native-only an enforced domain
  invariant, aligning durable/queued auto-download policy, preserving native MP4+M4A or unconstrained
  source containers according to `force_mp4`, routing non-streamable VP9 MP4 through document
  delivery, and changing transcoding to CRF-first with size limiting only as a fallback.
- 2026-07-26: Restricted production container publication and GitHub Releases to matching `v*`
  tag pushes, added stable/prerelease-aware GHCR tags, post-push image smoke testing, and verified
  reproducible source assets. Manual branch dispatches now publish nothing.
- 2026-07-26: Made managed Local Bot API process flags type-safe on non-Windows hosts and fixed
  the Windows CI analyzer to pass each management script as a scalar PSScriptAnalyzer path.
- 2026-07-26: Pinned the official Telegram Bot API parent repository to the verified full commit
  `adfd7f6a8e990272851777eeb3ae0def4216f161`, checks it out before synchronizing submodules, and
  added static plus real Compose-build CI regression gates.
- 2026-07-25: Made successful delivery completion and permanent byte accounting one atomic SQLite
  transaction; persistence uncertainty is quarantined without automatic Telegram retry. Added WAL
  contention coverage and backward-compatible legacy job idempotency keys.
- 2026-07-25: Added required-channel membership, yt-dlp-only proxy control, MP4/WebM container
  selection, Instagram Story/Highlight/multi-video policy, dynamic bot captions, durable user
  usage, and cross-platform Docker installation/management.
- 2026-07-24: Inspection now evaluates each enabled semantic selector against the real formats,
  hides unavailable exact heights, and displays resolution/FPS/HDR plus exact, estimated, or
  unknown video+audio size. Direct and multipart uploads report tracked byte progress in Telegram
  and structured logs, followed by an honest elapsed-time Telegram-processing heartbeat. Per-volume
  receipts are persisted immediately; upload chunks default to 1024 KiB and per-part timeout to
  14400 seconds.
- 2026-07-24: Simplified large-file delivery: every result above the configured direct Local Bot
  API ceiling now becomes stored 1850 MB ZIP volumes through 4096 MB. Removed the Premium account,
  Telethon dependency, MTProto session/CLI, staging channel, uploader queue/process, and `copyMessage`
  route.
- 2026-07-24: Added `1440p`, `2160p`, and non-transcoding `best_original` modes. Delivery now routes
  up to 1900 MB through Local Bot API and every larger result through 4096 MB as stored 1850 MB ZIP
  volumes with SHA-256 manifests.
- 2026-07-24: Local API CLI actions now accept `--config` both before and after the action name, so
  `telegram-media-bot local-api status --config ./config.yaml` and the original ordering are both
  valid.
- 2026-07-24: Added production Local Bot API lifecycle and migration. Bot/Worker now share one
  config-derived endpoint, mixed cloud/local clients are rejected, managed credentials never enter
  process command lines, normal startup never calls `logOut`, and files under the configured local
  upload ceiling are delivered without forced transcoding.
- 2026-07-24: Generic inspection size estimates are now advisory because upstream may report the
  best/default format before semantic selection. The selected download and final post-processed file
  remain strictly bounded by `media.max_file_size_mb`.
- 2026-07-24: Replaced yt-dlp's per-stream `max_filesize` behavior with complete-format selection and
  bounded FFmpeg transcoding. Explicit video modes now preserve both audio and their distinct target
  resolution instead of collapsing every oversized choice to the same lower native stream.
- 2026-07-24: Telegram file uploads now use the dedicated configurable
  `telegram.upload_timeout_seconds` (14400 seconds by default), preventing large uploads from being
  cut off by aiogram's 60-second general session timeout while preserving uncertain-delivery
  quarantine for genuinely ambiguous transport failures.

## Known limitations

- The supported v1 topology is one worker container with bounded internal concurrency. Multi-host
  worker replicas need a leased/shared durable database adapter; SQLite is not presented as that.
- Telegram provides no upload idempotency key. Ambiguous delivery is quarantined for operator review
  instead of automatically retried.
- DNS and extracted URLs are revalidated, but no application can eliminate DNS rebinding between a
  validation lookup and an upstream library's socket connect without controlling that library's
  resolver/transport.
- The Docker image builds the official Local Bot API executable from pinned upstream source. The
  destructive real >200 MB upload test still requires an explicitly configured local bot/chat and
  remains skipped in the default suite.
- Instagram Stories/Highlights that require authentication depend on a current operator-supplied
  read-only cookies file; upstream login challenges can still invalidate it.
- Castbox and Spotify are not implemented; both remain outside the generic v1 engine policy.
- Multi-volume output requires 7-Zip on the server and on the recipient device. Real >2 GB and
  >3.9 GB tests are destructive opt-in tests and remain skipped in the default suite.
