# T006 - Progress, cancellation, deduplication, and retries

**Status:** pending

## Deliverables

- Convert yt-dlp progress hooks to project progress events inside the adapter.
- Throttle Telegram edits by time and percentage delta.
- Add user cancellation and worker abort behavior.
- Add idempotency keys per user, URL, and selected mode for active jobs.
- Define retryable versus permanent errors.
- Ensure retries do not duplicate successful uploads.
- Clean all job directories after success, permanent failure, and cancellation.
- Add repeated concurrency and cancellation tests.
