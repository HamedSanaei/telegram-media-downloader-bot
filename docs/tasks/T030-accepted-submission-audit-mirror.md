# T030 - Durable accepted-submission audit mirror

**Status:** planned

## Goal

Mirror every successfully accepted download submission to enabled private logger destinations via a
durable asynchronous outbox, preserving Telegram-native media content, captions, order, and album
identity without delaying normal job acceptance.

## Why

Operators need a complete operational trace of accepted work while user processing remains
independent of logger availability.

## Dependencies

T026-T027, `durable_polling.py`, `EffectLedgerService`, and `TelegramDeliveryGateway`.

## Scope

Cover URL text, captions, photos, videos, documents, audio, animations, supported media, and albums.
Persist original user text separately from canonical URL/provider classification; enqueue a durable
mirror after acceptance and use `copyMessage`/`copyMessages` where available.

## Non-goals

Do not mirror commands, callbacks, cookie uploads, credentials, payment navigation, or rejected work;
do not make acceptance wait for Telegram logger delivery.

## Architecture and persistence

Outbox rows are `PENDING`, `COMPLETED`, or `UNCERTAIN`, keyed by source update/media-group identity
and destination. SQLite is truth; Redis only dispatches. No exactly-once delivery claim is made.

## Security, failure semantics, and compatibility

Source copies contain no secrets and use private bot-post-only channels. Destination failures retry
independently with bounded backoff; existing inbox/effect-ledger semantics remain authoritative.

## Tests, operations, acceptance gates, and Definition of Done

Test every supported media kind, captions/order/albums, duplicate and uncertain sends, restart,
Redis loss, destination isolation, and exclusion matrix. Done requires durable replay-safe mirroring
with no user-path coupling and no secret leakage.
