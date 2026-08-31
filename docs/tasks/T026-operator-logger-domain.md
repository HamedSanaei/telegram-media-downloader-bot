# T026 - Operator Logger domain and event-routing foundation

**Status:** complete

## Implementation record

Implemented in `domain/audit.py`, `application/ports/audit.py`, `audit_service.py`, and
`audit_sanitizer.py`. Events use typed categories/severity/UTC identities and approved numeric
Telegram user IDs. Central sanitization fails closed on credentials, headers, payment/session
material, paths, and raw tracebacks. Business emitters know no destination IDs. Unit regression
tests cover typed metadata and the complete secret-exclusion boundary.

## Goal

Define the project-owned audit vocabulary and routing policy for the Operator Logger and
private Telegram audit channels. Establish one typed event contract instead of scattered alert
strings or direct administrator fan-out.

## Why

Terminal worker failures and Cookie Health transitions currently know about `telegram.admin_ids`.
Submission auditing, operational alerts, and future account/payment events need one redacted,
correlatable boundary that does not make the user workflow depend on Telegram logger delivery.

## Dependencies

- Existing `FailureContext`, `error_policy.py`, `CookieHealthService`, `JobRecord`, and metrics.
- Durable inbound-update inbox and `EffectLedgerService` semantics.
- ADR-036 (proposed).

## Scope

- Add the domain model for `AuditEvent`, `AuditCategory` (`ERROR`, `COOKIE_HEALTH`,
  `USER_SUBMISSION`, `SYSTEM`), severity, event identity, UTC timestamp, correlation/request/update
  ID, optional job ID, numeric Telegram user ID, content type, provider classification, safe
  message, and source-message reference.
- Define `USER_SUBMISSION_RECEIVED` as the explicit accepted-download event; control interactions
  such as `/start`, `/menu`, help, callbacks, payment navigation, and back actions are excluded.
- Define project-owned `AuditSink`, `LoggerPort`, and `AuditService` responsibilities for
  eligibility, sanitization, fan-out, aggregate metrics, and no-destination behavior.
- Classify terminal operational failures and Cookie Health transitions separately from ordinary
  user-facing validation, cancellation, denial, and rate-limit responses.
- Specify bounded metric labels: category, severity, outcome, destination health, and outbox depth.

## Non-goals

- No Python implementation, schema, migration, dependency, Compose service, or runtime flag.
- No automatic operator alerts for every user-visible error.
- No fallback to all configured administrators when logger destinations are absent.

## Architecture

Application services emit typed events to `AuditService`. The service sanitizes once, records a
stable event identity, and hands delivery to independent destination sinks. Telegram, persistence,
and future web/payment adapters remain infrastructure concerns. Structured application logging and
health metrics are the only fallback when no logger destination is usable.

## Persistence

The event/outbox design preserves the event ID, category, safe metadata, source
reference, and delivery correlation without storing credentials, raw exceptions, or downloaded
media bytes. Retention is explicitly indefinite for the first implementation; any future manual
purge must be bounded and idempotent.

## Configuration

The strict `telegram.logger` section supplies enablement and configured destinations. This
task owns the typed defaults and validation contract but does not change `Settings` yet.

## Security and privacy

Numeric Telegram user IDs are intentionally included in the private audit metadata envelope. URLs,
message text, and media are private-channel content, not structured logs or metric labels. Cookies,
passwords, 2FA, authorization headers, bot tokens, filesystem paths, payment secrets, and raw
exception text are forbidden.

## Failure semantics

Sanitization failure drops the unsafe event and records a bounded health signal; it never leaks the
original value. A missing or unhealthy sink does not change the user/job outcome. Duplicate event
submission uses the stable event identity and is harmless to the durable outbox.

## Telegram behavior

This task defines event semantics only. It must not add handler blocking, direct Bot API calls from
handlers, or user-visible changes. Telegram delivery is implemented behind the later sink/outbox
tasks.

## Backward compatibility and migration

Existing jobs, inbound updates, effect-ledger rows, Cookie Health rows, and administrator UX remain
readable. The event model is additive and disabled until the later rollout task enables it.

## Tests

- Event category/severity validation, UTC timestamps, correlation fields, and stable identities.
- Sanitization of URLs, exceptions, headers, cookies, and credential-like values.
- Eligibility tests for terminal errors, Cookie Health, system events, and excluded user errors.
- Numeric user-ID inclusion and bounded metric-label assertions.
- Duplicate event creation and no-destination fallback behavior.

## Operational considerations

Expose only aggregate event/outbox/health metrics. Document which events are intentionally silent,
how operators correlate a logger message with a job, and how sanitization failures are surfaced.

## Acceptance gates

- Every event has one typed category, severity, correlation identity, and redacted payload.
- No business service branches on destination names or Telegram administrator IDs.
- No logger destination is treated as an implicit administrator fallback.

## Definition of done

The domain contract, routing matrix, redaction rules, metrics vocabulary, and tests are documented
and approved for implementation without inventing additional event semantics.
