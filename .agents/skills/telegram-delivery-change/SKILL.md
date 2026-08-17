---
name: telegram-delivery-change
description: Scope Telegram handlers, callbacks, administrator UX, delivery, upload, batching, Local Bot API, or user-visible progress changes. Use when changing aiogram presentation or Telegram delivery behavior.
---

# Telegram delivery change

Start with `graphify query "Trace TELEGRAM_BEHAVIOR from handler/callback to application port,
worker delivery, receipts, and tests"`. Inspect `telegram/`, the delivery/application ports, worker
receipt handling, and relevant handler/UI/delivery integration tests. Load T005/T006/T008 and
ADR-006, ADR-008, ADR-013, ADR-024, or ADR-025 as applicable.

Handlers must not block or call media engines directly. Reauthorize admin actions, keep callbacks
opaque/owner-bound/within Telegram limits, preserve source ordering and exact-byte document paths,
and treat ambiguous upload as `delivery_uncertain` rather than an automatic duplicate. Check
cancellation, batching, progress, cleanup, and Local API behavior together when the path crosses them.

