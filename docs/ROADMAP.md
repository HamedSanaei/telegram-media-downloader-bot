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

- T014 VIP entitlement and subscription domain — planned
- T015 provider-neutral billing and payment foundation — planned; depends on T014
- T016 secure companion web and callback boundary — planned
- T017 encrypted per-user Instagram credential vault — planned; depends on T016
- T018 Instagram account connection and recovery UX — planned; depends on T016/T017
- T019 credential resolution and adapter integration — planned; depends on T017
- T020 VIP public Instagram user-first fallback — planned; depends on T014/T018/T019
- T021 VIP private Instagram media — planned; depends on T020
- T022 per-user Instagram session recovery and isolation — planned; depends on T018/T020/T021
- T023 VIP purchasing and account Telegram UX — planned; depends on T014/T015/T016/T018
- T024 first real payment gateway adapter — **blocked: payment provider not selected**; depends on
  T015/T016/T023
- T025 VIP security, migration, operations, and end-to-end rollout — planned; depends on T014-T023;
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
