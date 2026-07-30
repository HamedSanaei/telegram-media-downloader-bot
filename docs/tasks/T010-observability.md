# T010 - Observability

**Status:** complete (2026-07-23)

Structured logs carry request/job/user/chat/source context and pass through recursive credential
redaction. The worker exposes `/health`, `/ready`, and Prometheus `/metrics`; readiness covers Redis,
SQLite, writable storage, Telegram, ffmpeg, and the engine. Admin failure summaries contain only
opaque job IDs and stable error categories.
Cleanup emits bounded structured events and counters for deleted files/bytes, failures, and sweep
duration without logging arbitrary media paths.
Weekly/monthly PNG and complete text usage reports use Tehran-local periods, serialize rendering
per administrator, and exclude administrators from public KPIs without exposing IDs or deleting
audit records.

## Deliverables

- Structured logs with request, user, chat, source, and job IDs.
- Metrics for queue depth, duration, bytes, failures, source, and error category.
- Health/readiness checks for Redis, writable storage, Telegram connectivity, ffmpeg, and engine.
- Operator-facing failure summaries with sensitive data redacted.
- Documentation for alert thresholds and incident diagnosis.
