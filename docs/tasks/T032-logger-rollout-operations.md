# T032 - End-to-end logger rollout, migration, and operations

**Status:** planned

## Goal

Stage the Operator Logger safely across persistence, configuration, Telegram delivery, recovery,
privacy, monitoring, and rollback without regressing existing bot reliability.

## Why

The subsystem crosses handlers, workers, SQLite, Redis, Bot API permissions, privacy, and future
VIP/payment boundaries. End-to-end gates are required before any operational alert or submission
mirror is enabled in production.

## Dependencies

- T026 through T031.
- T011 release/upgrade safeguards, ADR-015/018/023/031, and current Docker unless-stopped behavior.

## Scope

- Specify additive/idempotent initialization for destination, event, outbox, and effect state.
- Back up SQLite/WAL/SHM, canonical cookies, configuration, and any logger state before rollout.
- Test old configuration/database startup, clean install, restart, worker restart, Redis loss,
  Bot API permission changes, channel removal, and recovery of pending/uncertain events.
- Stage activation: persistence disabled, destination management, operational alerts, privacy-notice
  gated submission mirroring, then broader selected events.
- Define metrics, health/readiness indicators, alert thresholds, runbooks, incident response, and
  rollback that preserves forward-readable audit state.
- Confirm compatibility with VIP/Instagram/payment planning and the v1.3.7 blocked-release policy.

## Non-goals

- No runtime rollout, deployment, release, tag, history rewrite, repository recreation, or fake
  commit in this planning task.
- No deletion of audit, payment, credential, job, or effect state as a rollback shortcut.

## Architecture

SQLite/WAL remains durable logger truth; Redis only schedules work. Existing durable inbox, effect
ledger, delivery uncertainty, cancellation precedence, queue recovery, and zero-retention workspace
cleanup remain authoritative. Every logger side effect is secondary to the accepted job outcome.

## Persistence

Backups include logger destinations/outbox/effect records and preserve WAL consistency. Recovery
reconciles stale leases and uncertain sends without claiming impossible exactly-once Telegram
delivery. Indefinite audit retention is preserved during rollback.

## Configuration

Validate strict logger settings, configured/runtime reconciliation, privacy acknowledgement, and
feature gates before activation. Restore the matching pre-change configuration on rollback.

## Security and privacy

Run secret scans and least-privilege reviews across databases, Redis, logs, backups, Telegram
captures, and metrics. Verify the logger never receives future VIP/Instagram credentials or payment
secrets and that ordinary users cannot inspect destinations.

## Failure semantics

Migration, backup, permission, configuration, or dispatcher failure blocks activation but does not
stop ordinary downloads. Per-destination outages are isolated. Recovery preserves `UNCERTAIN`,
cancellation, job idempotency, and cleanup authority.

## Telegram behavior

Validate private channel permissions and Bot API copy behavior in a controlled environment. Confirm
no automatic admin DMs after error/Cookie Health migration and no user-visible blocking during logger
outage.

## Backward compatibility and migration

Old databases/configurations start with empty logger state. Additive initialization is rerunnable.
Rollback leaves new tables/state dormant and restores the prior strict configuration; it never
deletes audit history.

## Tests

- Full destination, error, Cookie Health, submission, exclusion, reliability, and admin UX matrix.
- Backup/restore with WAL/SHM, restart/recovery, Redis loss, duplicate/uncertain sends, and channel
  removal.
- Migration rerun, old configuration, clean installation, feature-gate rollback, and permission
  changes.
- End-to-end privacy notice, secret scans, bounded metrics, and zero-admin-DM proof.

## Operational considerations

Publish runbooks for destination setup/removal, Bot API permission loss, outbox growth, uncertain
sends, backup restoration, privacy incidents, and manual retention/purge review. Record rollout
owner, activation time, and rollback criteria.

## Acceptance gates

- All prior reliability and quality gates pass with logger disabled and enabled in test fixtures.
- No destination, outbox, or privacy failure changes user download outcomes.
- Production activation is staged only after backup, migration, privacy, security, and recovery
  evidence is reviewed.

## Definition of done

The complete migration, compatibility, backup/restore, staged rollout, observability, failure,
rollback, and production-readiness checklist is approved for implementation.
