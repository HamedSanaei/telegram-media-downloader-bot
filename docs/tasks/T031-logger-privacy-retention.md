# T031 - Privacy, retention, access, and secret-exclusion controls

**Status:** complete

## Goal

Make private audit-channel collection explicit, operator-controlled, indefinitely retained by
default, and provably free of credential or secret material.

## Why

Copying original user media and URLs into a private Telegram channel is a material privacy change.
It requires transparent notice, access controls, retention ownership, and future-feature guardrails.

## Dependencies

- T026 through T030.
- Existing zero-retention workspace cleanup and proposed ADR-038.
- Planned VIP/Instagram/payment boundaries from T014-T025.

## Scope

- Gate activation on the exact Persian notice: «برای اجرای سرویس و پشتیبانی/امنیت، لینک‌ها و رسانه‌هایی که برای دانلود می‌فرستید ممکن است در کانال خصوصی عملیاتی لاگر کپی و به‌صورت نامحدود نگهداری شوند؛ با ادامهٔ استفاده موافقت می‌کنید.»
- Document indefinite retention of copied audit content and safe audit metadata in the first
  implementation; do not introduce automatic Telegram deletion.
- Define a future operator-only manual purge as bounded, idempotent, independently retried, and
  never coupled to user-facing message deletion.
- Require private channels, minimal human membership, bot post-only permissions, operator-controlled
  membership, and no ordinary-user exposure of destination IDs.
- Maintain a permanent exclusion list for cookies, passwords, 2FA/checkpoint codes, authorization
  headers, bot tokens, credentials, filesystem paths, raw exceptions, Instagram sessions, signed
  login tokens, card/payment secrets, and gateway credentials.
- Define secret-free structured logs/metrics and review future VIP/account/payment flows before any
  logger event is allowed.

## Non-goals

- No legal-policy claim beyond the explicit product notice.
- No public audit channel, automatic channel membership, or privacy bypass.
- No deletion of existing Telegram messages or media workspaces in this planning change.

## Architecture

The privacy notice is a product/configuration gate before T030 emits copies. Channel access is an
operator responsibility. Logger content, durable metadata, structured logs, metrics, and future
credential/payment records have separate redaction policies.

## Persistence

Audit content and safe metadata are retained indefinitely as selected. Existing media files retain
the project’s zero-retention cleanup. Credential/payment/audit state remains outside media cleanup.
Any later purge must preserve idempotency and not erase job/effect authority needed for recovery.

## Configuration

Document the enabled-with-configured-destination activation rule and the explicit privacy-notice
acknowledgement. Unknown logger settings must fail under the repository’s strict configuration model.

## Security and privacy

Numeric Telegram user IDs are included deliberately in private audit metadata. They are excluded
from metrics and structured logs. Secret scanning must cover SQLite, Redis payloads, logs, metrics,
Telegram test captures, backups, and failure alerts.

## Failure semantics

If notice, access, redaction, or secret validation fails, logger mirroring fails closed while normal
user processing continues. Manual purge failures are retried independently and reported only through
sanitized operational signals.

## Telegram behavior

The notice must be shown before activation and remain understandable in Persian. Logger messages are
private operational copies, not forwards to the user and not edits to the original message.

## Backward compatibility and migration

Existing users and databases remain usable with logger disabled/unconfigured. Enabling the feature
is additive and staged; rollback restores the prior configuration without deleting audit state.

## Tests

- Notice gating and indefinite-retention behavior.
- Private-channel access and bot post-only assumptions.
- `/start`, `/menu`, callbacks, cookie uploads, Instagram credentials, login tokens, 2FA, and
  payment-secret exclusion.
- Secret-free logs/metrics/database/Redis/backup scans and numeric user-ID policy assertions.
- Manual purge idempotency and failure isolation (future implementation contract).

## Operational considerations

Maintain an access-review checklist, channel membership inventory, retention statement, incident
response procedure, and credential/payment redaction review before each future feature activation.

## Acceptance gates

- The exact notice and indefinite-retention decision are documented in product and architecture docs.
- No forbidden secret can cross the audit, logging, metrics, or Telegram boundaries.
- Existing zero-retention media cleanup remains unchanged.

## Definition of done

Privacy notice, retention, access control, data classification, secret exclusions, future-feature
review, tests, and operational ownership are explicit and implementation-ready.

## Implementation record

- **v1.4.0-rc.2 change:** accepted-submission mirroring requires logger enablement, mirror
  enablement, explicit operator privacy attestation, and at least one usable private destination —
  and nothing else. The per-user acknowledgement gate was removed from the acceptance path; the
  disclosure is informational only (`/privacy`) and never blocks, delays, or rejects a download.
- The exact Persian disclosure text is `LOGGER_PRIVACY_DISCLOSURE_FA`. Legacy
  `logger_privacy_acknowledgements` rows and the `has_privacy_acknowledgement`/
  `acknowledge_privacy` API are deprecated and retained for backward compatibility only; no
  destructive migration is performed and the table is never consulted at runtime.
- Notice/control interactions never create audit events. Sanitizer and audit faults close only the
  mirror path and cannot change ordinary download acceptance.
- Audit identifiers are bounded safe values. The sanitizer rejects raw tracebacks, sensitive paths,
  cookie rows, and non-string payloads, and redacts authorization, bot-token, account/session,
  signed-login, proxy, card/payment, and gateway-secret material before durable persistence.
- Audit copies and safe metadata remain indefinitely retained. There is no automatic Telegram or
  audit-state purge; media-workspace zero-retention cleanup is unchanged. A future operator purge
  remains a separately designed, bounded, idempotent operation.
- Destination probing continues to require a private channel, bot membership, and posting access.
  Operators own minimal channel membership/access review; destination IDs never reach ordinary
  users or metric labels.
