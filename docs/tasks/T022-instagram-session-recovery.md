# T022 - Per-user Instagram session recovery and isolation

**Status:** implemented locally

## Goal

Add bounded, generation-aware recovery for one user's Instagram credential failures without
changing another user or the global operator-cookie health state.

## Why

Current cookie remediation is provider/operator scoped. Applying it to per-user sessions would let
Alice's expiry pause, requeue, or mark Bob's jobs unhealthy.

## Dependencies

- T018 connection/reconnect lifecycle.
- T020 public fallback state.
- T021 private user-only authorization.
- Existing durable recovery from T006/T009 and ADR-017/023/030.

## Scope

- Add typed cases equivalent to `USER_SESSION_EXPIRED`, `USER_SESSION_CHALLENGE_REQUIRED`,
  `USER_PRIVATE_ACCESS_DENIED`, and `OPERATOR_COOKIE_EXPIRED`.
- Persist a user credential state transition only from a real request made with that exact owner
  and generation.
- Keep operator failure/remediation on the existing provider-level Cookie Health path.
- On successful reconnect, offer or perform one bounded recovery of eligible same-owner jobs,
  atomically rebinding them to the new generation according to explicit user action/state.
- Explicit revoke cancels/fails dependent queued work and never auto-resumes after a later connect.
- Preserve entitlement snapshots for accepted work, but require a currently usable owner credential.
- Persist fallback phase/generation so restart and Redis loss cannot alternate credentials forever.
- Add per-user credential leases and bounded recovery batch/queue-pressure behavior.

## Non-goals

- No scheduled Instagram keepalive/probe, global user-session health flag, arbitrary failed-job
  replay, cross-user recovery, or retry of delivery uncertainty.

## Architecture

SQLite remains durable truth for job eligibility, credential state/generation, recovery attempts,
and fallback-used state. Redis/ARQ only coordinates transient execution. Reconnect recovery queries
by owner + prior generation + typed recoverability; it never derives ownership from a username or
URL. Delivery uncertainty and cancellation remain authoritative exclusions.

## Data and persistence changes

Add safe recovery class, expected/new generation, recovery attempt/time, notification state, and
credential phase fields where T020 fields are insufficient. Index by owner, credential generation,
terminal status, recoverability, and update time. Retain with the media job under existing job
retention; credential lifecycle events retain 90 days. No cookie or upstream error text is stored.

## Security requirements

- Recovery candidate SQL and service checks both enforce `job.user_id == credential.owner_user_id`.
- Reconnect cannot revive a job cancelled/revoked by the user or one in `delivery_uncertain`.
- A state update requires an observed credential kind/generation, not URL/provider inference alone.
- Metrics contain state/category/outcome only, never owner, username, URL, credential, or job ID.

## Failure semantics

- Expired/invalid -> `EXPIRED`; challenge/checkpoint -> `CHALLENGE_REQUIRED`; explicit no-access
  remains an authorization result and does not mark the session expired.
- Operator failure updates operator Cookie Health only.
- Lease busy is bounded retry/defer, not credential expiry.
- Generation mismatch fails or enters explicit reconnect recovery; it never silently decrypts old
  or unrelated material.

## Migration and backward compatibility

Legacy provider recovery remains unchanged. Existing jobs without user credential metadata cannot
enter user-session recovery. Empty new columns/tables are additive and idempotent.

## Telegram UX

Notify only the affected user with reconnect/retry actions and sanitized state. Bob receives no
notification when Alice fails. Avoid repeated alerts through durable notification deduplication.

## Acceptance gates

- Alice expiry changes only Alice's credential and eligible jobs.
- Restart/Redis loss/retry preserves exactly-one public credential switch and bounded recovery.
- Revocation, cancellation, and delivery uncertainty are never automatically requeued.
- No active health polling exists.

## Tests

- Alice failure/Bob unaffected and cross-user candidate-query defenses.
- Expired, challenge, access-denied, operator-expired, lease-busy classification.
- Reconnect generation replacement and bounded same-owner requeue.
- Explicit revoke versus reconnect; cancellation and delivery-uncertainty precedence.
- Process/worker restart, Redis loss, queue pressure, retry exhaustion, and notification dedupe.
- Subscription renewal and accepted-job expiry interactions.

## Operational considerations

Expose aggregate session states, recovery outcomes, fallback outcomes, and lease contention. Keep
all inspection/admin views sanitized and low cardinality.

## Risks

Unbounded replay can duplicate work or trigger challenges. Cross-user queries can leak credentials.
Owner/generation indexes, atomic transitions, bounded attempts, and T025 concurrency tests mitigate.

## Definition of done

Owner/generation-scoped recovery eligibility and terminal exclusions are implemented locally. Full
reconnect rebinding, queue orchestration, and notification deduplication remain rollout-gated by
T025; no scheduled probes or cross-user remediation are introduced.
