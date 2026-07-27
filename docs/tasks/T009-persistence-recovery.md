# T009 - Durable job state and restart recovery

**Status:** complete (cancel-safe reconciliation expansion 2026-07-27)

SQLite/WAL stores selections, jobs, transitions, attempts, error categories, cancellation, blocks,
and Telegram delivery IDs. Startup reconciliation requeues interrupted pre-delivery jobs and
quarantines uncertain deliveries. Scheduled maintenance purges expired metadata and safe orphan job
directories. Concurrency and restart tests cover deduplication and recovery.

Reconciliation treats `cancel_requested` as authoritative for queued, running, and retrying jobs:
they become terminal cancelled records and are excluded from every requeue path. Existing healthy
abandoned-job and delivery-uncertainty behavior remains unchanged.

## Deliverables

- Introduce a storage port and durable job records.
- Persist state transitions with timestamps and error categories.
- Reconcile abandoned running jobs at startup.
- Add scheduled cleanup for orphaned files and expired metadata.
- Make worker retries and delivery idempotent across process restarts.
- Add restart/recovery integration tests.
