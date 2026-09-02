# T025 - VIP security, migration, operations, and end-to-end rollout

**Status:** implemented with the T024/T025 milestone (fake-provider purchase E2E, admin VIP
panel, real Instagram acquisition, USER_ONLY private gating, payment Logger, reconciliation, and
migration/additive-schema coverage all green locally). Production billing activation remains
operator-gated: configure `payments.*` credentials/callback URLs and set
`payments.enabled: true`.


## Goal

Validate and stage the complete milestone across migration, backup/restore, key rotation,
concurrency, privacy, payment idempotency, rollback, observability, and production readiness.

## Why

Credential custody and economic state expand the blast radius beyond ordinary media jobs. A green
unit suite alone is insufficient to enable production account linking, VIP routing, or payments.

## Dependencies

- T014 through T023.
- T024 for production purchasing; fake-gateway E2E may run while T024 remains blocked.
- Existing release/update/backup guarantees from T011 and ADR-015/018/023/031.

## Scope

- Audit every proposed table/field for purpose, sensitivity, indexes, uniqueness, retention, and
  backward compatibility.
- Add feature flags defaulting off for web/account connection, VIP credential preference, private
  media, and billing activation.
- Extend private atomic backup/restore to SQLite/WAL/SHM, canonical cookies, companion configuration,
  signing keys, and vault key ring with exact permissions.
- Exercise additive migration, old configuration, clean installation, upgrade, rollback, and
  forward recovery without deleting new tables/audit records.
- Run key-generation/rotation/restore/loss and compromised-session revocation drills.
- Run per-credential lease, worker concurrency, restart, Redis-loss, and fallback-loop tests.
- Verify the complete entitlement, credential, public/private, isolation, recovery, payment, and
  web-security matrices end to end.
- Add safe aggregate metrics/admin visibility and production alert/runbook thresholds.
- Perform privacy/terms/abuse review and document incident response.

## Non-goals

- No repository recreation, history rewrite, forced push, release workaround, privacy bypass,
  arbitrary data purge, or payment activation before T024 passes.

## Architecture

SQLite remains durable control/economic/credential metadata truth; Redis remains transient. Media
zero-retention continues for job files but explicitly excludes financial audit and encrypted
credential state. Rollback restores matching configuration/application files while preserving
forward-readable additive data. Delivery uncertainty and cancellation remain authoritative.

## Data and persistence changes

No new product schema should originate here; this task reviews and tests T014-T022 migrations.
Operational audit records are retained indefinitely; credential events 90 days; expired handoff
hashes/leases purge within 24 hours; media jobs retain the existing policy. Backup manifests list
each sensitive file without logging contents or secret paths in public diagnostics.

## Security requirements

- Independent call-trace proof that private jobs never resolve/call operator credentials.
- Cross-user property/integration tests over repository, resolver, worker, router, and cleanup.
- Secret scanning of database dumps, Redis payloads, logs, metrics, Telegram captures, archives, and
  failure alerts.
- Least-privilege service/config mounts; companion has no bot token.
- No username/URL/user/credential/order/transaction labels in metrics.

## Failure semantics

Every feature flag fails closed. Backup/key/config/preflight failure occurs before writer downtime
or feature activation. Failed migration/restore/rotation preserves the prior usable state. Payment
uncertainty grants nothing. Private ambiguity reveals nothing and never falls back.

## Migration and backward compatibility

Test a server with only current `users`, `jobs`, usage, cookie health, inbound updates, and effect
ledger. Free users receive no credential/subscription row. Old jobs remain readable. Rollback keeps
new additive tables dormant and restores configuration that older strict models can parse.

## Telegram UX

Run owner/role/accessibility review for `/vip`, gating, connection, payment, admin revoke/disable,
and incident messages. Verify Persian messages are actionable and secret-free.

## Acceptance gates

- All repository quality, security, migration, backup/restore, concurrency, and E2E matrices pass.
- Operator Instagram account has current zero-follow attestation.
- Private call traces show zero operator credential use.
- Key backup/restore and rotation succeed with exact permissions.
- Production billing remains disabled until T024 is unblocked and verified.

## Tests

- Clean install; every historical supported config/database fixture; additive migration rerun.
- Backup/restore with WAL/SHM, key ring, web settings, and canonical cookies; rollback at each stage.
- Full matrix from Milestone 4 tasks using deterministic fake upstream/gateway fixtures.
- Load/lease contention, worker restart, Redis loss, session replacement/revoke, accepted VIP expiry.
- Duplicate/concurrent/refund payment E2E and callback security.
- Secret/high-cardinality scans and private operator-resolution call traces.

## Operational considerations

Staged rollout:

1. Backup and apply additive schema with every feature off.
2. Enable Free-user account connection.
3. Enable VIP/user-first public routing after public-only operator attestation.
4. Enable private access after isolation/no-fallback gates.
5. Enable purchasing only after T024 and provider production checks.

Document rollback, key loss, vault compromise, provider outage, chargeback, clock drift, Instagram
challenge/rate limit, upstream change, and operator-cookie incident runbooks.

## Risks

The release risk register must map Instagram challenges/upstream/IP suspicion to T018/T022;
credential compromise/key rotation/cross-user leakage to T017/T019/T025; operator private leakage to
T019-T021/T025; and callback forgery/duplication/chargebacks/clock errors to T015/T016/T024/T025.

## Definition of done

The fail-closed rollout readiness guard is implemented locally and keeps billing blocked when no
provider is selected. Full backup/restore drills, fake-gateway E2E, and operator-approved activation
remain release gates; no production billing is enabled.
