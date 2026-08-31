# T030 - Durable accepted-submission audit mirror

**Status:** complete (2026-08-31)

## Goal

Mirror every successfully accepted actual download submission into enabled private logger channels
without delaying or changing the normal user/job workflow.

## Why

The audit requirement concerns the original Telegram submission, not merely the downloaded result.
Operators need the exact source message, caption, media, and album ordering for support and security
correlation.

## Dependencies

- T026 and T027.
- `handlers.submit_url()`, durable inbound updates, `JobService`, `TelegramDeliveryGateway`, and
  existing Bot API copy/delivery tests.
- Proposed ADR-036 and ADR-038.

## Scope

- Emit `USER_SUBMISSION_RECEIVED` only after the request is durably accepted and associated with its
  inspection/job identity.
- Cover URL text, photo, video, document, audio, animation, supported media attachments, captions,
  and future file-processing submissions.
- Prefer Telegram-native `copyMessage`/`copyMessages` from the original chat/message IDs, keeping
  the original content and caption without unnecessary forward attribution/profile exposure.
- Aggregate media groups as one logical submission while preserving every item, caption, and source
  order; use one correlation identity for all copied messages.
- Preserve the original user-entered URL in the private copy and record canonical URL/provider
  classification separately for internal correlation.
- Attach a separate safe metadata envelope/message where needed so exact original captions are not
  modified. Include timestamp, update/request ID, job ID when available, content type, provider, and
  the explicitly selected numeric Telegram user ID.
- Write durable outbox work before normal processing continues. Logger delivery must never determine
  whether the user’s inspection/download succeeds.

## Non-goals

- No mirroring of `/start`, `/menu`, help, admin callbacks, payment navigation, back actions, or
  unsupported/non-accepted messages.
- No downloaded-result duplication and no Telegram forward-style social attribution requirement.
- No reading an entire Telegram file into Python memory; native server-side copy is preferred.

## Architecture

The handler records acceptance and safe source references, then enqueues a logger effect. A separate
dispatcher performs copy operations and metadata delivery per destination. The normal queue,
inspection, worker, delivery, cleanup, and cancellation paths remain independent.

## Persistence

Outbox rows store bounded source chat/message/group references, event metadata, state, retry count,
and correlation IDs, not downloaded bytes or credentials. Indefinite retention applies to the audit
copy and its safe metadata; leases and uncertain effects remain explicitly bounded/reconciled.

## Configuration

The mirror activates only when logger enablement, privacy notice, and at least one valid destination
are satisfied. Destination selection is the T027 config/runtime union.

## Security and privacy

Private-channel access is operator-controlled. The mirrored content may include the user’s submitted
URL and media, but never cookies, passwords, 2FA, authorization headers, bot tokens, filesystem
paths, Instagram session material, payment secrets, or signed login tokens.

## Failure semantics

PENDING/COMPLETED/UNCERTAIN (or equivalent) effect states prevent uncontrolled duplicates. A failed
copy, unsupported Bot API operation, channel permission error, or timeout is isolated to that
destination and retried/quarantined without failing the accepted job.

## Telegram behavior

Native copies preserve media bytes, captions, and ordering. Mixed media groups use the supported
aggregate API or a deterministic ordered fallback. The original user message is never edited,
deleted, or replaced.

## Backward compatibility and migration

Current URL submissions and admin download submissions continue to use the same `submit_url` path.
The event is additive and can be disabled without changing existing jobs or inbound-update replay.

## Tests

- URL text, photo, video, document, audio, animation, caption, and media-group equivalence.
- Group ordering, all-item preservation, one logical identity, and canonical correlation fields.
- Accepted user and administrator-originated download submissions are mirrored; control messages are
  not.
- Logger outage, one failed destination, restart, worker restart, Redis loss, duplicate event, and
  uncertain copy behavior leave the user job unaffected.
- Secret and whole-file-memory assertions.

## Operational considerations

Monitor accepted-event/outbox depth, copy outcomes, uncertain effects, and per-destination health.
Document Telegram Bot API copy limits and operator handling for quarantined uncertain sends.

## Acceptance gates

- Durable acceptance precedes asynchronous logger work.
- Every supported accepted content type is copied faithfully and correlated.
- User-facing job success/failure is independent of logger delivery.

## Definition of done

The source-message capture contract, media-group aggregation, native-copy strategy, metadata
envelope, outbox/effect state machine, exclusions, tests, and operational runbook are complete.

## Implementation evidence

- `AcceptedSubmissionAuditService` is invoked only after `JobService.create_inspection()` returns a
  durable accepted job. Invalid URLs and control handlers never reach the emission boundary, and
  audit exceptions are isolated from the normal queue path.
- Durable inbox snapshots resolve bounded album message IDs in Telegram order. A restart-safe
  group mapping merges late members before a short outbox settle deadline without changing the
  logical event identity.
- `TelegramAuditDelivery` uses native `copyMessage` for one source and `copyMessages` for ordered
  groups, then sends only the safe metadata envelope. Forbidden destinations terminate; ambiguous
  network/API outcomes become `UNCERTAIN` and are never automatically resent.
- Focused tests cover text, photo, video, document, audio, animation, captions, albums, duplicate
  updates/restarts, multiple destinations, independent flags, failure isolation, and control
  exclusions. Runtime outbox draining and operational health are completed by T032.
