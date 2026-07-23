# Roadmap

## Milestone 0 - Foundation

- T001 repository and quality baseline — complete
- T002 strict configuration and startup composition — complete
- T003 engine boundary and initial yt-dlp adapter — complete
- T004 bot-to-worker default download path — complete as a starter

## Milestone 1 - User experience and correctness

- T005 two-step inspect and semantic format selection
- T006 progress, cancellation, deduplication, and resilient retries
- T007 source policy, URL security, user rate limits, and admin controls
- T008 Telegram delivery strategy and large-file behavior

## Milestone 2 - Operations and scale

- T009 persistent job history, cleanup reconciliation, and restart recovery
- T010 observability, health endpoints/commands, metrics, and operational alerts
- T011 controlled yt-dlp upgrade automation, canary validation, and rollback documentation
- T012 external plugin package scaffold for custom extractors

## Milestone 3 - Optional adapters

- Spotify metadata resolver with truthful alternate-source labeling;
- object storage or local Telegram Bot API;
- webhook mode;
- multi-worker scheduling and per-source concurrency pools.
