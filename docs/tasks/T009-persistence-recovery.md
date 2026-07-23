# T009 - Durable job state and restart recovery

**Status:** complete (2026-07-23)

SQLite/WAL stores selections, jobs, transitions, attempts, error categories, cancellation, blocks,
and Telegram delivery IDs. Startup reconciliation requeues interrupted pre-delivery jobs and
quarantines uncertain deliveries. Scheduled maintenance purges expired metadata and safe orphan job
directories. Concurrency and restart tests cover deduplication and recovery.

## Deliverables

- Introduce a storage port and durable job records.
- Persist state transitions with timestamps and error categories.
- Reconcile abandoned running jobs at startup.
- Add scheduled cleanup for orphaned files and expired metadata.
- Make worker retries and delivery idempotent across process restarts.
- Add restart/recovery integration tests.
