# T030 - Durable accepted-submission audit mirror

**Status:** planned

## Goal

Mirror every successfully accepted download submission to enabled private logger destinations via a
durable asynchronous outbox, preserving Telegram-native media content, captions, order, and album
identity without delaying normal job acceptance.

## Why and dependencies

Operators need a complete operational trace while user processing remains independent of logger
availability. Depends on T026-T027, `durable_polling.py`, `EffectLedgerService`, and
`TelegramDeliveryGateway`.

## Scope and non-goals

Cover URL text, captions, photos, videos, documents, audio, animations, supported media, and albums.
Persist original text separately from canonical URL/provider classification; enqueue after acceptance
and use `copyMessage`/`copyMessages`. Exclude commands, callbacks, cookie uploads, credentials,
payment navigation, and rejected work; acceptance never waits for logger delivery.

## Architecture, persistence, security, and failure semantics

Rows are `PENDING`, `COMPLETED`, or `UNCERTAIN`, keyed by source update/media-group identity and
destination. SQLite is truth; Redis only dispatches. Copies contain no secrets, retry independently
with bounded backoff, and do not claim Telegram exactly-once delivery.

## Compatibility, tests, operations, acceptance gates, and Definition of Done

Preserve inbox/effect-ledger semantics and existing zero-retention media cleanup. Test every media
kind, captions/order/albums, duplicate and uncertain sends, restart, Redis loss, destination
isolation, and exclusion matrix. Done requires replay-safe mirroring with no user-path coupling or
secret leakage.
