# Roadmap

## Milestone 0 - Foundation

- T001 repository and quality baseline — complete
- T002 strict configuration and startup composition — complete
- T003 engine boundary and initial yt-dlp adapter — complete
- T004 bot-to-worker default download path — complete as a starter

## Milestone 1 - User experience and correctness — complete

- T005 two-step inspect and semantic format selection
- T006 progress, cancellation, deduplication, and resilient retries
- T007 source policy, URL security, user rate limits, and admin controls
- T008 Telegram delivery strategy and large-file behavior

## Milestone 2 - Operations and scale — complete

- T009 persistent job history, cleanup reconciliation, and restart recovery
- T010 observability, health endpoints/commands, metrics, and operational alerts
- T011 controlled yt-dlp upgrade automation, canary validation, and rollback documentation
- T012 external plugin package scaffold for custom extractors

## Milestone 3 - Optional adapters — not part of v1

- Spotify metadata resolver with truthful alternate-source labeling;
- object storage or local Telegram Bot API;
- webhook mode;
- multi-worker scheduling and per-source concurrency pools.

## Milestone 4 - Accounts, VIP subscriptions, billing, and authenticated Instagram access — future

- T014 VIP entitlement and subscription domain — complete
- T015 provider-neutral billing and payment foundation — complete
- T016 secure companion web and callback boundary — complete
- T017 encrypted per-user Instagram credential vault — complete; depends on T016
- T018 Instagram account connection and recovery UX — complete; depends on T016/T017
- T019 credential resolution and adapter integration — complete; depends on T017
- T020 VIP public Instagram user-first fallback — complete; depends on T014/T018/T019
- T021 VIP private Instagram media — complete; depends on T020
- T022 per-user Instagram session recovery and isolation — complete; depends on T018/T020/T021
- T023 VIP purchasing and account Telegram UX — complete; depends on T014/T015/T016/T018
- T024 first real payment gateway adapter — **blocked: payment provider not selected**; depends on
  T015/T016/T023
- T025 VIP security, migration, operations, and end-to-end rollout — complete; depends on T014-T023;
  production billing activation also depends on T024

Dependency order:

```text
T014 -> T015 -----------------------> T023 -> T024 (BLOCKED)
  |       |                             ^
  |       +-----------> T016 -----------+
  |                       |
  +---------------------> T017
                          | \
                          v  v
                        T018 T019
                          \  /
                           v
                         T020 -> T021 -> T022 -> T025
```

The milestone is additive and feature-gated. Account connection may be made available to Free
users before purchasing exists. Production purchasing stays disabled until T024 is unblocked and
verified. This VIP means a paid bot entitlement; it does not restore the removed Telegram Premium,
Telethon/MTProto staging-channel, Premium upload queue, or copy-delivery architecture.

## Milestone 5 - Operational Logger and private audit channels — complete (T026-T032)

- T026 Operator Logger domain and event-routing foundation — complete
- T027 Durable logger destinations, configuration, and outbox — complete; depends on T026
- T028 Administrator logger-channel management UX — complete; depends on T026/T027
- T029 Operational error and Cookie Health notification migration — complete; depends on T026-T028
- T030 Durable accepted-submission audit mirror — complete; depends on T026/T027
- T031 Privacy, retention, access, and secret-exclusion controls — complete; depends on T026-T030
- T032 End-to-end logger rollout, migration, and operations — complete; depends on T026-T031

Dependency graph:

```text
T026 Logger domain/events
   └──> T027 destinations/config/outbox
          ├──> T028 admin logger UX
          │      └──> T029 error/Cookie Health migration
          └──> T030 accepted-submission mirror
                 └──> T031 privacy/retention/security
                        └──> T032 E2E rollout/operations
```

T026/T027 are implemented with additive SQLite/WAL state and defaults fully off: a typed,
centrally sanitized audit event domain and a durable per-destination outbox with leases, bounded
retry, `UNCERTAIN` quarantine, and config/runtime destination reconciliation. T028 adds the admin
logger-channel management UX (add/list/test/enable/disable/remove with a typed verifier and
reauthorized callbacks). T029 routes terminal operational failures and Cookie Health
transitions/reminders as typed logger events with no admin-DM fallback. T030 now emits replay-safe
accepted-submission events after durable job creation, resolves bounded album source identities,
and supplies native `copyMessage`/`copyMessages` delivery. T031 adds the exact Persian disclosure,
explicit operator attestation, indefinite retention, and the permanent secret-exclusion boundary.
Since v1.4.0-rc.2 the privacy disclosure is informational only (`/privacy`): no per-user
acknowledgement is required, requested, or consulted in the acceptance path, and legacy
acknowledgement rows are retained for backward compatibility only. T032 wires bounded worker
draining, aggregate health and metrics, destination lifecycle isolation, consistent backups, and
staged rollout/incident/rollback runbooks. Alerts and submission mirroring are separately enabled;
mirroring requires the operator privacy attestation and a usable private destination. Audit
content has indefinite retention and no automatic Telegram deletion. ADR-036 through ADR-038 are
accepted.

## Milestone 6 - CI performance and developer velocity

- T033 Fast-feedback CI and conditional heavy validation — complete; CI infrastructure remains
  orthogonal to the now-complete T015-T023 and T025-T032 runtime work

Implemented: a deterministic, unit-testable changed-path classifier, a fast
`quality` lane for ordinary source/docs changes, conditional heavy lanes
(dependency/package/plugin-sdk/docker-runtime/updater-integration/installer-linux/installer-windows),
stable `change-detection` / `final-ci-gate` checks, fail-conservative fallback for any unclassifiable
or unknown change, safe same-ref development concurrency, and preserved tag-only publication
safety. T015-T023 and T025-T032 runtime work is complete; T024 remains blocked pending provider
selection.
