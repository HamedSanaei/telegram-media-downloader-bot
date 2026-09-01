# Project specification

## Goal

Provide a Telegram bot that receives supported public media URLs, inspects them through a generic
media engine, lets the user choose a semantic output mode, queues the work, downloads safely in a
separate worker, uploads the result, and cleans temporary data.

## Primary non-functional goal

`yt-dlp` must be replaceable and updatable without spreading upstream types, options, exceptions,
or metadata dictionaries throughout the codebase.

## Initial supported-source policy

The operator enables sources in `config.yaml`. The initial example enables YouTube, SoundCloud,
Instagram, Twitter/X, Pinterest, and TikTok. This is a policy list, not a set of dedicated handlers.
Actual extraction support is determined by the installed `yt-dlp` version.

## User flow target

1. User sends one URL.
2. Bot validates access policy, canonicalizes a YouTube URL with a valid video ID to single-video
   intent (removing Mix/playlist context), and enqueues metadata inspection.
3. Bot displays normalized title, duration, source, and only semantic formats that are actually
   selectable, including selected resolution/FPS/HDR and exact, estimated, or unknown size.
4. For ordinary video, the public UI exposes only zero-transcode AV1/AAC or H.264/AAC MP4 and
   native VP9/Opus WebM,
   followed by unique qualities derived from the actual selected streams. Converted/generic
   video policies remain internal and cannot be reached by current or legacy callbacks. Instagram
   video posts, Reels, Stories, and Highlights skip both prompts and use the best original
   streams; `media.instagram.force_mp4` may constrain the native video/audio pair to MP4 + M4A
   without changing codecs.
5. Worker downloads into an isolated job directory.
6. Telegram and worker logs report throttled download/upload progress. Upload percentage covers
   bytes read into Local Bot API; the opaque Telegram phase reports only elapsed-time heartbeats.
7. Result is uploaded using the most suitable Telegram method. Every delivered media item remains
   traceable to the durable canonical job URL: the adapter appends `🔗 لینک اصلی: <URL>` after the
   existing caption/per-item text, or replies with that complete line when Telegram's 1024-character
   media-caption limit cannot accommodate it.
8. Job state is persisted sufficiently for retries and operator inspection.
9. After every terminal outcome, the worker removes that job's media, archive volumes, sidecars,
   `.part` files, and temporary files from both workspace roots. Confirmed multipart parts are
   removed immediately after their durable Telegram receipt.

Configured administrators receive a persistent management keyboard from `/start` or `/menu`.
Its download prompt is only an alternate entry point to the same URL validation, inspection,
selection, queue, worker, delivery, and cleanup flow used by every user. Weekly/monthly PNG and
complete text reports exclude administrator IDs from public KPIs at query time without deleting
their durable jobs or usage events.

The private administrator panel also manages the configured canonical Netscape cookie file. It
accepts bounded Telegram documents, detects supported services from domains rather than filenames,
merges only those service records, and permits an authorized private-chat export of the complete
current file. Every yt-dlp job—including YouTube and SoundCloud—and every gallery-dl provider opens
that same effective path for each new job. Cookie values and file contents never enter logs,
durable jobs, or notifications.

For explicitly enabled large-file delivery, results up to the Local Bot API ceiling are uploaded
directly. Every larger result is emitted as bounded multi-volume ZIP documents through the
configured 4096 MB aggregate ceiling.

The v1 implementation provides the complete two-step inspection, semantic selection, durable job,
progress/cancellation, delivery, and cleanup flow described above.

Every pre-enqueue selection page has deterministic Back navigation. It edits the existing Telegram
message and reuses the owner-bound persisted inspection; Back never repeats yt-dlp inspection,
creates a durable job, or enqueues Redis work.

YouTube `watch`, `youtu.be`, `shorts`, and `live` URLs containing a valid video ID always mean one
video unless the user enters an explicit playlist action. Mix parameters do not turn them into a
playlist. Genuine `/playlist?list=...` URLs retain the configured bounded-playlist policy.

## Required operational behavior

- One-command Docker Compose startup after local config creation.
- Optional fail-closed membership in every configured channel before protected operations.
- An optional HTTP(S)/SOCKS proxy scoped strictly to yt-dlp source traffic.
- Permanent user profiles and idempotent daily/success/failure/byte usage counters in SQLite.
- All secrets in ignored local YAML configuration.
- Separate bot and download worker processes.
- Bounded concurrency, retries, timeouts, size limits, and rate limits.
- Clean shutdown and recovery after restart.
- Zero-retention cleanup for successful, failed, cancelled, timed-out, and delivery-uncertain jobs,
  with a startup/maintenance sweeper for terminal and abandoned workspaces.
- Release updates may remove only unreferenced old images from this project's GHCR repository after
  candidate health/version/doctor verification; they never perform a global Docker prune.
- Structured logs with correlation/job IDs.
- Role-authorized management messages/callbacks, single-flight report rendering, and no
  administrator identity or media metadata in usage reports.
- Controlled dependency updates and rollback through Git/lockfile.
- Unit tests by default; opt-in external contract tests.

## Out of scope unless separately approved

- DRM circumvention;
- arbitrary shell execution or user-controlled yt-dlp options;
- downloading local files or private-network URLs;
- automatic startup-time dependency self-updates;
- modifying the upstream yt-dlp source tree;
- guaranteed support for every upstream extractor;
- disguising alternate-source downloads as direct Spotify downloads.

## Planned VIP / authenticated Instagram access

**Planning status:** future Milestone 4 only. None of the behavior in this section is implemented or
available in production. Current public downloads continue to use the existing canonical operator
cookie path.

The Telegram product term is **VIP**. Internal code will use project-owned `SubscriptionPlan`,
`Subscription`, `Entitlement`, `EntitlementGrant`, `EntitlementSnapshot`, `Capability`,
`PaymentOrder`, and `InstagramCredential` models. The existing Telegram `is_premium` profile field
is not bot VIP authorization. This work does not restore the removed Telegram
Premium/Telethon/MTProto uploader, staging channel, Premium queue, or copy-message delivery design.

### Product and authorization rules

- Every Free or VIP user may connect their own Instagram account through a short-lived signed link
  and HTTPS page. Connection alone does not grant VIP.
- Free public downloading remains supported and does not require account connection.
- Private/restricted Instagram posts, Reels, Stories, Highlights, mixed media, and Close Friends
  content require an active `instagram_private_media` entitlement and a healthy connected account.
- Authenticated extraction may see only content that the voluntarily connected Instagram account
  can already see. The bot does not bypass follower approval, privacy controls, DRM, or account
  security.
- A protected request durably accepted while entitlement is active may finish after expiry. New
  requests and later user callbacks after expiry must reauthorize.

The operator Instagram credential must be a dedicated account verified as following zero accounts.
Its attestation is invalidated when the canonical file's Instagram records change. It may authorize
only results explicitly classified `PUBLIC`; unknown/restricted results fail closed.

### Authoritative credential matrix

| User | VIP | Instagram connected | Content | Credential policy |
|---|---|---|---|---|
| User | No | No | Public | Operator/public |
| User | No | Yes | Public | Operator/public |
| User | No | Yes | Private | Deny + offer VIP |
| User | Yes | No | Public | Operator/public |
| User | Yes | No | Private | Require Instagram connection |
| User | Yes | Yes | Public | User first, operator fallback once |
| User | Yes | Yes | Private | User only, never operator fallback |

For public media, the one operator switch is eligible only after a typed user-session expiry,
invalid/login-required, or credential-rejected failure. Filesystem, FFmpeg, adapter schema,
post-processing, size, Telegram delivery, cancellation, local-runtime, and generic failures never
switch credentials. Credential phase/fallback use is durable so retries and restarts cannot loop.

**Private-content invariant:** once content is known or accepted as user-restricted, every
inspection, gallery/yt-dlp child resolution, download, retry, and recovery attempt is `USER_ONLY`.
The operator credential is never a private authorization fallback. Unknown privacy fails closed and
must not reveal whether content exists.

### Planned subscription and payment behavior

- Plans accept any positive calendar-month duration, integer minor-unit price, currency, enabled
  state, and typed capabilities. No commercial price or payment provider is selected yet.
- Renewals preserve time by starting after the later of payment confirmation or current expiry.
- Verified payment confirmation and one entitlement grant are atomic/idempotent in SQLite. Browser
  redirects never activate VIP, and duplicate provider callbacks never add time twice.
- Refund/reversal excludes that payment's grant and deterministically recomputes remaining grants;
  access ends when no valid paid time remains.
- Financial/audit records are retained separately from media zero-retention and never store card
  numbers, CVV, payment credentials, raw callbacks, or provider secrets.

### Planned credential and connection behavior

- Users never send Instagram passwords or 2FA codes in Telegram. The companion HTTPS flow keeps
  them only in bounded memory, discards them, and persists only an encrypted session.
- Sessions use owner-bound AES-256-GCM envelopes with random nonces, version/key IDs, associated
  data, rotation support, and master keys outside SQLite in ignored least-privilege YAML.
- States are `CONNECTED`, `EXPIRED`, `CHALLENGE_REQUIRED`, `REVOKED`, and `DISCONNECTED`.
- Jobs/Redis/logs/metrics/messages/errors contain no raw cookie. They may carry only safe owner,
  policy, generation, entitlement snapshot, access scope, and bounded fallback state.
- Decrypted Netscape files live only inside the exact job workspace with restrictive permissions
  and are removed after success, failure, cancellation, timeout, and cleanup.
- One user's credential failure/recovery never changes another user or global operator health.

Implementation is decomposed into T014 through T025. T024 remains blocked until the operator
selects and supplies a real payment provider.

## Operator Logger and private audit channels

**Implementation status:** Milestone 5 is complete and defaults off. The subsystem is distinct from
stdout and normal structured application logs: it is a durable, private Telegram operational
destination.

The event categories are `ERROR`, `COOKIE_HEALTH`, `USER_SUBMISSION`, and `SYSTEM`. Typed
events carry UTC time, correlation/request/update ID, job ID when available, content/provider
classification, a sanitized message, source-message references, and an explicitly approved numeric
Telegram user ID. Bounded metrics contain only category, severity, outcome, health state, and outbox
depth; they never contain user/channel IDs, URLs, message text, filenames, or credentials.

### Routing and destinations

- Enabled config-managed and runtime-managed private channels form a deduplicated union by numeric
  `chat_id`; the design does not assume one hardcoded channel.
- Activation requires logger enablement, operator privacy attestation, and at least one valid
  destination. Since v1.4.0-rc.2 there is no per-user privacy-notice acknowledgement gate: the
  disclosure is informational only (`/privacy`) and never blocks acceptance.
- Every enabled destination receives selected events independently. A missing, forbidden, removed,
  or unreachable destination produces structured logs and bounded health metrics only; it never
  falls back to all `telegram.admin_ids`.
- Terminal operational failures currently eligible for administrator alerts route to `ERROR`.
  Ordinary invalid/unsupported URLs, cancellation, normal denial, rate limits, and other user-facing
  responses remain silent unless existing policy explicitly classifies them as operational.
- Cookie Health transitions route only to `COOKIE_HEALTH`. Administrators retain manual Cookie Health
  inspection, but no automatic Cookie Health DM is sent to every administrator.
- The existing admin panel manages add/list/test/enable/disable/remove/health using
  `🧾 کانال‌های لاگر`; all actions remain role-authorized by `telegram.admin_ids`.

### Accepted-submission mirror

After a download submission is durably accepted, the original Telegram message is copied to each
enabled logger destination. URL text, photo, video, document, audio, animation, supported media,
captions, and media groups are included; `/start`, `/menu`, help, callbacks, payment navigation,
and back actions are not. Telegram-native `copyMessage`/`copyMessages` is preferred so media,
captions, and album ordering remain faithful without unnecessary forward attribution. Albums have
one logical submission identity. The original user-entered URL is preserved in the private copy and
canonical/provider classification is recorded separately for correlation.

Copies are scheduled through a durable asynchronous outbox after acceptance. `PENDING`,
`COMPLETED`, and `UNCERTAIN` (or equivalent) states acknowledge Telegram ambiguity without claiming
exactly-once delivery. Logger failure cannot fail, delay, cancel, or change the user’s download.

After a download reaches durable `SUCCEEDED`, the same operator gate also mirrors the actual
Telegram output with a separate `DOWNLOAD_OUTPUT_DELIVERED` event. Only ordered durable delivery
items in `DELIVERED` state with concrete recipient message IDs are eligible; partial collections
include only their confirmed items. Native `copyMessage`/`copyMessages` preserves the representation
the user received without reading or re-uploading local media. A durable pre-delivery intent and
restart reconciliation recover a crash after job completion but before outbox enqueue, while a
deterministic `delivery-output:{job_id}` identity prevents duplicate effects. Delivery-uncertain or
missing-receipt work is never inferred, and logger uncertainty remains quarantined without retry.

### Privacy, retention, and future-feature boundary

Before activation users must see:

> برای اجرای سرویس و پشتیبانی/امنیت، لینک‌ها و رسانه‌هایی که برای دانلود می‌فرستید ممکن است در کانال خصوصی عملیاتی لاگر کپی و به‌صورت نامحدود نگهداری شوند؛ با ادامهٔ استفاده موافقت می‌کنید.

Audit copies and safe metadata are retained indefinitely in the first implementation; no automatic
Telegram deletion is planned. Any later manual purge must be bounded, idempotent, independently
retried, and never coupled to user-facing message deletion. Channels remain private with minimal
human membership and bot post-only permission.

The logger must never receive cookies, passwords, 2FA/checkpoint codes, authorization headers, bot
tokens, credentials, filesystem paths, raw exceptions, Instagram session material, signed login
tokens, card/payment secrets, or gateway credentials. Future VIP/Instagram/payment flows must be
reviewed against this exclusion list before adding events.

Implementation is recorded in T026-T032 (typed sanitized event domain, durable destinations and
outbox, administrator channel management, operational-alert migration with no admin-DM fallback,
durable accepted-submission native-copy intents, versioned privacy/security controls, bounded
worker dispatch, health/metrics, backup, and state-preserving rollout operations),
under accepted ADR-036 through ADR-038. This feature does not change current public download
behavior,
current media zero-retention cleanup, or the removed Telegram Premium/Telethon/MTProto architecture.
