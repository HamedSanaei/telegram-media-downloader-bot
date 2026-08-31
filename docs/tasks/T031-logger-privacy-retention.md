# T031 - Privacy, retention, access, and secret-exclusion controls

**Status:** planned

## Goal

Gate logger activation on explicit Persian consent and document indefinite audit retention, private
membership, minimal human access, numeric user-ID exposure, and strict secret exclusion.

## Why

Source mirroring is operationally useful but materially changes user privacy expectations.

## Dependencies

T026-T030, existing zero-retention media cleanup, and future VIP/Instagram/payment boundaries.

## Scope

Show before activation: «برای اجرای سرویس و پشتیبانی/امنیت، لینک‌ها و رسانه‌هایی که برای دانلود
می‌فرستید ممکن است در کانال خصوصی عملیاتی لاگر کپی و به‌صورت نامحدود نگهداری شوند؛ با ادامهٔ
استفاده موافقت می‌کنید.» Require private channels, bot post-only permissions, bounded metrics, and
secret-free logs. No automatic Telegram deletion is introduced.

## Non-goals, architecture, persistence, and security

Do not expose cookies, passwords, 2FA, tokens, credentials, payment secrets, or Instagram session
material. Future manual purge must be bounded and idempotent; audit records remain outside media
zero-retention cleanup.

## Tests, operations, acceptance gates, and Definition of Done

Test notice gating, access control, retention, purge idempotency, exclusion scans, and bounded
labels. Done requires documented operator access and a reviewed privacy/incident runbook.
