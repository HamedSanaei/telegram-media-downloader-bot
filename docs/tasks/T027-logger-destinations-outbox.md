# T027 - Durable logger destinations, configuration, and outbox

**Status:** planned

## Goal

Design durable multi-channel destination ownership and an asynchronous logger outbox that survives
restart and Redis loss while isolating each private Telegram channel.

## Why

Logger delivery must not block downloads, and one unreachable channel must not prevent healthy
channels from receiving events. Configuration and administrator-managed destinations also need
deterministic reconciliation.

## Dependencies

- T026.
- SQLite/WAL, `SqliteJobRepository`, `SqliteEffectLedger`, durable inbox, queue recovery, and
  existing metrics.
- ADR-007, ADR-008, ADR-017, ADR-023, and proposed ADR-037.

## Scope

- Plan additive `logger_destinations` persistence keyed by numeric `chat_id`, with source
  (`CONFIG`/`RUNTIME`), enabled state, timestamps, health state, and sanitized last-failure class.
- Plan durable event/outbox rows containing event identity, destination, safe metadata/source
  references, state, attempt count, next-attempt time, and completion/uncertainty timestamps.
- Reconcile configured and runtime destinations as a deduplicated effective union. Configuration
  remains authoritative for config-managed rows; UI removal cannot falsely remove one.
- Define `ACTIVE`, `UNREACHABLE`, `FORBIDDEN`, and `DISABLED` health transitions and per-destination
  retry/backoff/lease behavior.
- Keep logger outbox dispatch asynchronous and independent of ARQ download success/failure.

## Non-goals

- No schema or migration is implemented in this planning commit.
- No hardcoded single-channel setting or provider-name branching.
- No exactly-once Telegram guarantee.

## Architecture

The application writes an accepted typed event and destination work to SQLite before normal work
continues. A bounded dispatcher claims one destination/outbox item at a time, uses the Telegram
gateway, and records `PENDING`, `COMPLETED`, or `UNCERTAIN` (or an equivalent quarantined state).
Healthy destinations continue when another destination fails.

## Persistence

SQLite/WAL is durable truth. Redis/ARQ may wake the dispatcher but may not own destination state,
retry claims, or event identity. Terminal rows follow the explicit indefinite audit-retention
decision; leases and stale pending claims are bounded and reconciled without deleting audit content.

## Configuration

Plan strict `telegram.logger.enabled` and `telegram.logger.channels` settings. Channels use numeric
`-100...` IDs and no secrets. A valid configured destination plus enablement and privacy notice
activation turns on selected events; an empty list never falls back to `admin_ids`.

## Security and privacy

Validate chat IDs, channel type, bot membership, and posting permission before activation. Do not
persist credentials, invite links, authorization headers, raw Telegram errors, or arbitrary admin
input. Destination health shown to admins is sanitized.

## Failure semantics

Telegram timeout/network ambiguity records `UNCERTAIN` and prevents uncontrolled automatic duplicate
sends. Definitive forbidden/not-found responses mark only that destination unhealthy. Retry uses
bounded backoff and does not alter the originating job outcome.

## Telegram behavior

All Bot API calls occur in the infrastructure gateway/dispatcher, never in a blocking handler. The
dispatcher may use native copy operations for submission events as specified by T030.

## Backward compatibility and migration

Old databases and configurations start with no logger destinations and preserve current behavior.
Additive initialization is idempotent, restart-safe, and compatible with existing WAL/backup and
effect-ledger cleanup procedures.

## Tests

- One, multiple, duplicate, invalid, removed, forbidden, runtime, config, and overlapping channels.
- Config/runtime reconciliation and protection against falsely removing config-managed channels.
- Dispatcher restart, Redis loss, stale claim recovery, duplicate event, timeout, and uncertain send.
- Per-destination isolation, retry bounds, health transitions, and bounded metrics.

## Operational considerations

Provide outbox-depth, pending-age, completion, uncertainty, and destination-health dashboards with
bounded labels. Document alert thresholds and a runbook for bot permission changes or channel loss.

## Acceptance gates

- Durable event/outbox state is committed before dispatch.
- A failed destination never blocks another destination or the user-facing workflow.
- No logger path reads or writes `telegram.admin_ids` as a fallback recipient list.

## Definition of done

Persistence fields, reconciliation rules, state machine, retry/lease semantics, configuration
contract, rollback behavior, and deterministic tests are ready for a later additive implementation.
