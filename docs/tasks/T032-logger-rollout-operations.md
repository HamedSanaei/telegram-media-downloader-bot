# T032 - End-to-end logger rollout, migration, and operations

**Status:** planned

## Goal

Stage the logger additively with backup/restore, restart and Redis-loss recovery, permission-change
handling, monitoring, runbooks, rollback, and production acceptance.

## Why

Operational delivery and retained audit data must not compromise existing durable job, cookie-health,
delivery-uncertainty, release, or zero-retention guarantees.

## Dependencies

T026-T031 and existing SQLite/WAL, inbox, effect ledger, worker recovery, VIP planning, and release
safeguards.

## Scope and rollout

Back up SQLite/WAL/SHM and config, apply idempotent additive schema with support disabled, enable
channels and privacy notice, exercise restart/permission/outage matrices, then stage activation.
Preserve old tables and audit state on rollback; never create fake commits or delete/recreate data.

## Non-goals and failure semantics

No broad administrator fallback, destructive purge, history rewrite, forced push, release, or deploy.
Logger outage is isolated from user jobs and produces structured health signals.

## Tests, operations, acceptance gates, and Definition of Done

Run migration/backup/restore, multi-channel isolation, outbox replay, monitoring, secret scans, and
rollback drills. Done requires an operator-approved runbook and green full validation with logger
support still feature-gated.
