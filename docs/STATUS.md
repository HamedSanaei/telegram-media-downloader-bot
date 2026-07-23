# Project status

Last updated: 2026-07-23

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
- Controlled yt-dlp upgrade reports, per-source opt-in contracts, canary failure-rate gate, and an
  independent external extractor plugin template.

## Verification

The final exact command results and coverage are recorded in `docs/HANDOFF_REPORT.md` after the last
gate run. External contracts remain opt-in and require operator-maintained public URLs.

## Known limitations

- The supported v1 topology is one worker container with bounded internal concurrency. Multi-host
  worker replicas need a leased/shared durable database adapter; SQLite is not presented as that.
- Telegram provides no upload idempotency key. Ambiguous delivery is quarantined for operator review
  instead of automatically retried.
- DNS and extracted URLs are revalidated, but no application can eliminate DNS rebinding between a
  validation lookup and an upstream library's socket connect without controlling that library's
  resolver/transport.
- A local Telegram Bot API endpoint is supported but intentionally not bundled or enabled by default.
- Castbox and Spotify are not implemented; both remain outside the generic v1 engine policy.
