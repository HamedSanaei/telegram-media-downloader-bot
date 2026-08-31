# T028 - Administrator logger-channel management UX

**Status:** complete

## Goal

Add a role-protected Telegram administration flow for discovering, testing, enabling, disabling,
and removing private logger destinations.

## Why

Operators need to manage more than one audit channel without editing secrets in source code, while
untrusted users and forged callbacks must not change destination state.

## Dependencies

- T026 and T027.
- Existing `admin_menu.py`, `admin_handlers.py`, callback authorization, and Bot API gateway.
- Proposed ADR-037.

## Scope

- Add the existing admin menu button `🧾 کانال‌های لاگر` and a dedicated management keyboard/state.
- Require current `telegram.admin_ids` authorization for every message and callback.
- Support numeric `-100...` add, list, test, enable, disable, remove, and health actions.
- Validate channel existence/type, bot membership, and post permission; send a sanitized test
  message before marking a destination active.
- Show config/runtime ownership and health without exposing secrets or to ordinary users.
- Make forged, stale, malformed, or cross-action callbacks fail closed.

## Non-goals

- No public channel discovery, automatic invitation, membership management, or single-channel
  assumption.
- No change to ordinary user menus or download behavior.

## Architecture

The presentation layer calls an application destination-management port. It does not write SQLite,
inspect raw Telegram exceptions, or send operational alerts directly. Callback payloads remain
opaque, short, action-scoped, and reauthorized at execution time.

## Persistence

Runtime-created destinations and their health are durable through T027. Removing a runtime row does
not delete config-managed ownership. Test-message effects use replay-safe semantics and do not
create arbitrary audit events.

## Configuration

Configured channels are displayed as config-managed and cannot be falsely removed through the UI.
They disappear only after configuration is changed and reloaded/restarted. Runtime channels remain
durable until an authorized removal.

## Security and privacy

Only private channels with minimal operator membership are supported/recommended. Bot permissions
must allow posting but need not grant broad administrative rights. Channel IDs are never disclosed
to ordinary users; admin output is sanitized.

## Failure semantics

Invalid IDs, non-channels, missing membership, forbidden posting, and Telegram ambiguity return
actionable Persian admin errors and update only the affected destination health. A failed test does
not remove another destination or change user jobs.

## Telegram behavior

All management messages are private-admin-only. Every callback repeats authorization and validates
the current destination/action. The UX includes list and health refresh, explicit confirmation for
removal, and a test action that reports success without leaking transport details.

## Backward compatibility and migration

Existing admin menus, cookie-health actions, reports, and download prompts remain available. An
administrator with no logger configuration sees a disabled/empty state rather than an error.

## Tests

- Authorized admin, unauthorized user, forged callback, malformed callback, and stale state.
- Add/list/test/enable/disable/remove for runtime destinations.
- Invalid chat ID, non-channel, bot not member, bot forbidden, removed channel, and retry health.
- Config-managed channel cannot be falsely removed; duplicate config/runtime IDs deduplicate.
- Persian errors are sanitized and no ordinary user receives logger destination details.

## Operational considerations

Document the operator checklist: create private channel, add bot with post permission, copy numeric
ID, add/test, confirm health, and monitor destination metrics.

## Acceptance gates

- Every state-changing action is role-authorized and durable.
- A successful test proves bot posting permission before activation.
- Config/runtime ownership and removal behavior are visible and deterministic.

## Implementation summary

- Added `DestinationProbeOutcome`/`DestinationProbeResult` to `domain/audit.py` and a
  `LoggerDestinationVerifier` protocol in `application/ports/audit.py`.
- Added `record_probe_health` to `SqliteAuditRepository` so a probe updates only the probed
  destination's health/failure class.
- Added `application/services/audit_destination_admin.py`: `LoggerDestinationAdminService` with
  strict `-100...` validation (`InvalidLoggerChannelError`), probe-driven health recording,
  enable/disable, list/health snapshot, and removal that refuses to remove config-owned rows
  (`ConfigOwnedLoggerChannelError`).
- Added `infrastructure/telegram/audit_destination_verifier.py`: `TelegramAuditDestinationVerifier`
  maps `get_chat`/`get_chat_member`/`send_message` failures to typed outcomes
  (ACTIVE / NOT_CHANNEL / BOT_NOT_MEMBER / FORBIDDEN / UNREACHABLE / AMBIGUOUS), sends a fixed
  sanitized Persian probe message, and best-effort deletes it. No raw exception text is surfaced.
- Added admin menu button `🧾 کانال‌های لاگر`, a bounded `awaiting_add_chat_id` state, and an
  inline keyboard showing channel ID, ownership (پیکربندی/مدیر), enabled state, and health with
  per-row test/enable/disable/remove actions plus a two-step remove confirmation.
- Every message and callback path reauthorizes against `telegram.admin_ids`; forged callbacks and
  non-admin continuation attempts fail closed with `ACCESS_DENIED_TEXT`.
- Wired `audit_admin` through `handlers.build_router` and `bot_app.run_bot` (audit store already
  initialized there for T027).
- Tests: `tests/unit/telegram/test_logger_admin.py` (23) and
  `tests/unit/infrastructure/telegram/test_audit_destination_verifier.py` (11); keyboard button-set
  test updated for the new menu row. Full non-contract suite 1131 passed, 11 skipped.
