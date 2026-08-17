---
name: worker-job-change
description: Scope worker inspection/download jobs, retries, progress, cancellation, delivery receipts, admin failure alerts, reconciliation, or cleanup. Use for changes to terminal outcomes or job lifecycle orchestration.
---

# Worker job change

Run `graphify query "Trace JOB_BEHAVIOR through workers/jobs.py, queue, persistence transitions,
delivery, cleanup, and tests"`. Inspect `workers/jobs.py`, queue/repository ports and adapters,
workspace cleanup, and `tests/unit/workers/test_jobs.py`; load T006/T009 and ADR-008, ADR-017,
ADR-023 when relevant.

Persist authoritative state before transient coordination. Alert only on the established terminal
conditions and keep payloads/logs redacted. Do not turn intermediate retries into terminal effects.
Preserve delivery-uncertainty quarantine, exact receipt ordering, retry idempotency, cancellation
precedence, process cleanup, and terminal download/temp cleanup even on secondary failures.

