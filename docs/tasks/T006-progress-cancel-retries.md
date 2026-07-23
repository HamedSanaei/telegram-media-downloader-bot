# T006 - Progress, cancellation, deduplication, and retries

**Status:** complete (2026-07-23)

The adapter maps upstream hooks to `ProgressEvent`; the worker uses a bounded latest-value queue and
time/percentage throttling. Durable cancellation is polled by adapter hooks. Active-job SHA-256
idempotency keys, classified retries, uncertain-delivery quarantine, and bounded cleanup prevent
uncontrolled duplicate uploads and abandoned partial files.

## Deliverables

- Convert yt-dlp progress hooks to project progress events inside the adapter.
- Throttle Telegram edits by time and percentage delta.
- Add user cancellation and worker abort behavior.
- Add idempotency keys per user, URL, and selected mode for active jobs.
- Define retryable versus permanent errors.
- Ensure retries do not duplicate successful uploads.
- Clean all job directories after success, permanent failure, and cancellation.
- Add repeated concurrency and cancellation tests.
