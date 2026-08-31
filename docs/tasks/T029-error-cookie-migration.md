# T029 - Operational error and Cookie Health notification migration

**Status:** complete

## Goal

Move eligible unsolicited operational notifications from private administrator chats to the
Operator Logger while preserving manual admin inspection and existing deduplication.

## Why

Direct fan-out from workers to every `telegram.admin_ids` mixes operator alerting with role UX and
cannot support multiple destinations, durable retries, or explicit privacy boundaries.

## Dependencies

- T026 through T028.
- `workers/jobs.py`, `FailureContext`, `error_policy.py`, `CookieHealthService`, and Cookie Health
  persistence/tests.
- Proposed ADR-036 and ADR-038.

## Scope

- Inventory every `_notify_admins_of_terminal_failure` and `_notify_admins_of_cookie_alert` caller,
  plus recovery and worker-startup notification paths.
- Route terminal operational errors and Cookie Health transition/reminder events as typed logger
  events; remove automatic sends to `telegram.admin_ids`.
- Preserve manual `/menu` Cookie Health inspection and admin-only refresh actions.
- Keep stable error categories, job/provider/category/time fields, Persian rendering, and central
  sanitization; omit tracebacks, URLs, paths, headers, cookies, and raw exception text.
- Preserve Cookie Health transition deduplication: one healthy-to-expired alert, no repeated storm,
  optional recovery, and a new alert after a later re-expiration.
- Distinguish operator-cookie alerts from future per-user credential events.

## Non-goals

- No alert for invalid/unsupported user URLs, cancellation, normal permission/VIP denial, ordinary
  rate limits, or user-facing errors unless an existing policy explicitly marks them operational.
- No scheduled provider probe or Cookie Health redesign.

## Architecture

Worker terminal paths emit `ERROR` events after durable terminal transition. Cookie Health emits
`COOKIE_HEALTH` events after persisted transition markers are updated. The logger service owns
fan-out and delivery; workers never enumerate recipients.

## Persistence

Existing Cookie Health `last_notified_state` and reminder timestamps remain authoritative. Logger
event/outbox rows are additive and retained indefinitely; uncertain sends are quarantined rather
than silently retried into duplicates.

## Configuration

Logger enablement/destinations come from T027. With no usable destination, the application emits a
structured log and bounded health/metric signal only; it never falls back to administrator IDs.

## Security and privacy

Sanitize Persian alerts centrally. Include only safe opaque job/provider/category/time metadata and
the explicitly approved numeric Telegram user ID where applicable. Never expose cookies or account
secrets in Telegram, logs, metrics, or errors.

## Failure semantics

Logger failure cannot change the job terminal status, cancellation precedence, cleanup, or Cookie
Health state. Per-destination failure is isolated. Duplicate transitions remain suppressed across
restart.

## Telegram behavior

Acceptance requires `cookie expires → logger receives alert → administrator private chats receive
nothing automatically`. Manual admin health viewing remains available and role-protected.

## Backward compatibility and migration

Existing jobs and Cookie Health rows remain readable. During staged rollout, the old direct fan-out
is removed only after logger routing and no-admin-DM tests pass. No historical alert messages are
deleted.

## Tests

- Critical terminal worker error routes to logger.
- Invalid URL, cancellation, normal denial, and rate-limit paths do not alert operators.
- Healthy→expired, expired→expired, expired→healthy, and healthy→expired-again transitions.
- No automatic admin DM, no raw exception/path/credential leakage, and restart deduplication.
- Logger unavailable leaves job outcome, cleanup, and Cookie Health persistence unchanged.

## Operational considerations

Update alert runbooks, dashboard names, and escalation ownership from administrator DMs to logger
destinations. Retain `/failed`, Cookie Health UI, and structured logs as operator diagnostics.

## Acceptance gates

- No unsolicited operational path calls `telegram.admin_ids` for notification delivery.
- All migrated events have a typed category and durable correlation identity.
- Cookie transition/reminder behavior remains unchanged except for its destination.

## Implementation summary

- Replaced `_notify_admins_of_terminal_failure` (admin-DM broadcast) with
  `_emit_terminal_failure_event`: a typed `ERROR`/`TERMINAL_OPERATIONAL_ERROR` audit event
  (CRITICAL, or WARNING for `DELIVERY_UNCERTAIN`) enqueued to the durable logger outbox with the
  job id, kind, and sanitized Persian `render_failure_notification` text.
- Replaced `_notify_admins_of_cookie_alert` with `_emit_cookie_health_event`: a
  `COOKIE_HEALTH`/`COOKIE_HEALTH_CHANGED` event (INFO on recovery, else WARNING). Correlation
  includes the persisted reminder timestamp so transitions stay deduplicated across restart
  (upstream `last_notified_state`/`last_reminder_at` markers are the authority) while a later
  re-expiration after recovery still alerts again.
- Both emitters log a structured `..._audit_unavailable` warning when no audit service is wired
  and NEVER enumerate `telegram.admin_ids`; no fallback admin broadcast exists. Emit failures are
  caught and logged so a logger storage fault can never change the user job outcome, cleanup, or
  Cookie Health state.
- Wired `AuditService` into the worker composition (`workers/settings.py`).
- Removed the now-dead `_cookie_health_reply_markup`/`_context_is_cookie_failure` helpers and the
  `Bot`-based admin fan-out.
- Tests: rewrote the five legacy admin-DM tests to assert typed outbox events (redaction, retry
  exhaustion, delivery-uncertain isolation, unique-event semantics), added direct
  terminal-failure and Cookie Health emit tests, an identical-alert enqueue-once test, a
  storage-failure-never-breaks-job test, and a full runtime-auth-failure → COOKIE_HEALTH event
  chain test. Full non-contract suite 1135 passed, 11 skipped.
