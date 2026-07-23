# T009 - Durable job state and restart recovery

**Status:** pending

## Deliverables

- Introduce a storage port and durable job records.
- Persist state transitions with timestamps and error categories.
- Reconcile abandoned running jobs at startup.
- Add scheduled cleanup for orphaned files and expired metadata.
- Make worker retries and delivery idempotent across process restarts.
- Add restart/recovery integration tests.
