# Changelog

## 1.3.6 - 2026-08-22

### Fixed

- Repair the production yt-dlp inspection failure on the read-only application filesystem.
  `inspect_options()` now creates a private per-run scratch workspace beneath the configured
  storage temp hierarchy and points both yt-dlp `paths.home` and `paths.temp` at it, so format
  probing (`_check_formats` tempfile usage) can never fall back to the process working directory
  (`/app`) on a read-only root filesystem. The workspace is removed when the run finishes and the
  orphan sweep reclaims it after the grace period if a crash leaks it; download jobs keep their
  exact existing per-job path configuration.
- Classify local infrastructure failures honestly instead of masquerading as remote provider
  failures. Filesystem OSErrors (EROFS, EACCES/EPERM, ENOSPC, and related local path/I/O errnos)
  map to a new typed terminal `LocalRuntimeError` with category `local_runtime`, safe path-free
  reasons carrying only the original exception class and errno, while network-shaped OSErrors
  (timeouts, connection reset/refused) keep their retryable remote-failure classification and all
  auth/rate-limit/unavailable mappings are unchanged.
- Preserve real failure context in administrator diagnostics: the yt-dlp engine attaches
  adapter, pipeline stage (`inspection`/`extraction`/`download`), and URL-provable source onto
  mapped errors, and workers honor that stage hint whenever no specialized classification exists.
  A local runtime failure no longer reports "stage: unknown / category: internal / Media download
  failed" to administrators.

### Operations

- Add a committed, network-free `inspection_workspace_smoke` module that reproduces the exact
  production conditions inside the real container (read-only root filesystem, no usable ambient
  temp directory) and proves `/app` stays read-only, storage temp is writable, inspection scratch
  files resolve inside it, and `YtDlpEngine.inspect` succeeds without touching `/app`; enforced in
  CI's Docker job. No Cookie Health behavior changed: refresh remains passive/local with zero
  automatic provider probes.

## 1.3.5 - 2026-08-20

### Fixed

- Classify a successful gallery-dl inspection with no emitted events as unavailable/inaccessible
  content instead of a JSON Lines contract change. Non-zero HTTP 401/403 and 404/410 failures now
  retain authentication/unavailable semantics, while malformed non-empty JSONL remains strictly
  rejected as `GalleryDlOutputChangedError`.
- Keep Instagram carousel `img_index` as explicitly removed presentation state (alongside stripped
  tracking such as `igsi`) while inspecting the canonical full post in source order.
- Treat matching Netscape session-cookie records as present but `UNVERIFIED`, never `MISSING`.
  Pinterest and SoundCloud uploads now use the shared provider registry, receive post-replacement
  canonical-byte/identity/count/permission verification with rollback, and trigger an immediate
  targeted persisted static health refresh before the administrator sees success.
- Make Cookie Health passive/local by design after an operator account received an automated-
  behavior warning. Remove `GalleryDlCookieProbe`, every provider probe URL/timeout/concurrency
  path, the Cookie Health ARQ cron/watcher, and the administrator "check all" action. Worker
  startup, admin refresh, and cookie upload now inspect only the canonical Netscape file and never
  contact a provider. Legacy probe configuration keys remain accepted and ignored for upgrade
  compatibility.
- Preserve `AUTH_FAILED` feedback only from the failure already returned by a real user-requested
  extraction. Successful-empty gallery-dl output remains conservatively unavailable and never
  triggers a second diagnostic request; authentication markers from the same stderr remain
  authoritative. Legacy persisted probe success is discarded by the next local static refresh.
- Treat Telegram's exact `message is not modified` edit error as an idempotent Cookie Health no-op,
  answer each callback once, and continue to propagate unrelated `TelegramBadRequest` failures.

## 1.3.4 - 2026-08-17

### Added

- **Rich administrator failure diagnostics.** A typed structured `FailureContext` is created as
  close as possible to the real failure and survives domain/infrastructure -> application
  service -> worker -> retry -> terminal failure -> admin notification. The final administrator
  message now preserves adapter, extractor, source, fallback chain and reason, HTTP status,
  error category, sanitized reason, attempt/max attempts, previous identical failures
  (`HTTP 403 x1`), media kind, raw/planned format counts, elapsed time, downloaded bytes, and
  app version. Only fields that exist are shown; absent optional fields are never printed as
  "unknown".
- **Central diagnostic sanitizer** (`application/services/diagnostic_sanitizer.py`). URLs are
  reduced to scheme + hostname + safe path with query parameters stripped unless explicitly
  classified safe; exception messages and log/database/admin payloads are redacted for cookie
  values, bearer/API tokens, bot tokens, API ID/hash, proxy passwords, authorization headers,
  signed CDN query secrets, and long opaque tokens. Synthetic secret-bearing exceptions are
  covered by regression tests.
- **Admin Cookie Health Center.** A network-free static validation over the canonical combined
  cookie file (readable, valid Netscape, provider-domain records, expiry timestamps,
  malformed records, file-permission contract) plus real but lightweight authenticated probes
  with bounded concurrency and a short timeout. Providers without a configured
  authentication-required probe endpoint are honestly reported UNVERIFIED instead of being
  marked healthy from anonymous public success. Health state, alert deduplication, and
  reminder markers persist in SQLite so worker/container restarts never reset them.
  Version 1.3.5 removes the active-probe portion of this historical design for account safety.
- **Cookie expiry/alerts.** A configurable local expiry watcher cron, immediate admin alert +
  cookie-health button on real runtime authentication failures, and state-transition alerts
  (HEALTHY -> EXPIRING_SOON -> EXPIRED, HEALTHY -> AUTH_FAILED, recovery notifications) with
  configurable reminder intervals. One provider registry in `domain/cookies.py` is the single
  source of truth (YouTube, Instagram, TikTok, X/Twitter, Pinterest, SoundCloud).
  Version 1.3.5 removes the watcher cron; refresh is now startup/admin/upload local inspection plus
  passive runtime authentication feedback.
- **Instagram bulk Stories.** After a successful exact-story inspection the user chooses
  "download this story" (exact MEDIA_ID preserved) or "download all active stories of this
  account" (internally targets `/stories/USERNAME/`, gallery-dl remains the primary engine).
  Bulk jobs are delivered as per-item batches with per-item failure isolation and a final
  summary (total / succeeded / failed), respect cancellation and per-media size limits, and
  never apply the single-item file limit to the aggregate collection size. Configurable
  per-job batch safeguards limit stories/highlights items.
- **Instagram Highlights.** Direct `/stories/highlights/ID/` URLs canonicalize (tracking
  stripped), inspect, and download every item in the Highlight with mixed image/video support.
  From an Instagram profile flow a "⭐ هایلایتها" action fetches the user's highlight tray,
  paginates it, and lets the user select ONE Highlight to download; stable internal highlight
  IDs live in callback/session state (persisted tray) and large metadata never enters
  callback data.
- **Instagram cookie-health gating.** Bulk Stories/Highlight jobs consult Cookie Health first:
  definitive EXPIRED/AUTH_FAILED/MISSING/MALFORMED states fail early with a useful message;
  UNKNOWN/UNVERIFIED proceeds normally, and a real extraction auth failure updates Instagram
  cookie health immediately.
- **Admin UX.** The admin menu now exposes "🍪 سلامت کوکیها" next to the existing cookie
  management; the center provides "بررسی سلامت همه کوکیها", "تازهسازی وضعیت", upload, and
  export. Every cookie-health callback action is ADMIN ONLY and fails closed for other users.
  Version 1.3.5 removes the live "check all" action; refresh is local-only.
- HTTP status is preserved on gallery-dl and yt-dlp mapped errors when the adapter observes it.

### Fixed

- `scripts/check_gallerydl_fixtures.py` now matches the v1.3.3 normalized video-only contract
  (video-only fixtures carry VIDEO assets instead of raising the removed no-images signal).
- Direct Instagram highlight URLs are classified as `highlight` before the generic story
  pattern so they never masquerade as a story media id.

## Unreleased

## 1.3.8 - 2026-08-31

### Fixed

- Repair a release-blocking durable-polling crash: inbound Telegram updates were persisted with
  raw ``Update.model_dump_json()``, which cannot serialize the aiogram ``Default`` sentinels that
  can appear in nested/default-valued fields (e.g. link-preview options), so every delivery of a
  ``Default``-bearing update crashed the bot into a permanent Docker restart loop.
  ``serialize_update()`` now uses aiogram's own Telegram-object serializer
  (``deserialize_telegram_object_to_python``) without the real Bot's outbound defaults, so updates
  persist as safe, replayable JSON without injecting parse-mode, link-preview, content-protection,
  or caption-placement policy into inbound snapshots.
- Harden poll-loop failure and ordering semantics: an update that cannot be serialized is a hard
  batch barrier, so no later update is persisted, processed, replayed, or acknowledged ahead of it.
  Transient failures retry from the blocked update ID in place. After the bounded threshold, a
  sanitized terminal tombstone durably records the ID and failure while deliberately abandoning
  handler processing, allowing later traffic to resume without misrepresenting the tombstone as the
  original update. Live-handler and serialization failures emit sanitized structured events (no
  payloads or user content).

### Operations

- Bounded per-update serialization retry and terminal quarantine keep a single impossible update
  from stalling the durable inbox; quarantined rows are purged by the existing terminal-failure
  retention. No new configuration required.

## 1.3.7 - 2026-08-30

### Added

- Durable Telegram update inbox: inbound updates are journaled to SQLite before the polling offset
  advances, so an update that arrives while the bot is offline — or that the process received and
  then crashed on — is replayed after restart until it is handled. Duplicate deliveries are
  deduplicated, successfully completed updates are never replayed, and unanswered user requests
  survive bot restarts.
- Recoverable supported-media jobs: terminal failures are classified by a typed, centralized
  recoverability vocabulary. Cookie/auth failures of a supported provider are requeued after an
  administrator successfully replaces that provider's cookie, and explicitly recoverable
  application/runtime failures receive a single automatic retry when a newer app version is
  deployed.
- All-active Instagram Stories delivery-mode selection: before creating the bulk job the user
  picks **normal** media delivery (`📱 دانلود معمولی`) or **file** delivery
  (`📁 دانلود به صورت فایل`); the choice is persisted on the durable job, survives restarts and
  recovery, and the callback is idempotent.
- Durable Telegram side-effect ledger: replay-sensitive handler messages (initial inspection
  status, Story delivery-mode prompt, recovery resume notices) are not duplicated when an inbound
  update replays after a crash — deterministic effect keys, edit/reuse of an already-known
  message, and an explicit `UNCERTAIN` state for Telegram calls whose outcome is unknown.

### Fixed

- `bot`, `worker`, and `local-api` no longer remain offline after a Docker daemon restart or
  server reboot: every production service (`bot`, `worker`, `local-api`, `redis`) now uses the
  `unless-stopped` Compose restart policy. Explicit `tmb stop` / `tmb uninstall` still stop
  services permanently.
- Automatic recovery excludes unsupported sources and never replays `delivery_uncertain` jobs;
  bounded attempts and a max age prevent infinite loops.
- Stale Telegram side-effect reservations are reconciled safely: `PENDING` effects older than the
  configurable 10-minute threshold are boundedly transitioned to `UNCERTAIN` rather than blindly
  resent. Fresh `PENDING`, `COMPLETED`, and existing `UNCERTAIN` effects are untouched.

### Operations

- Bound the durable Telegram inbox: COMPLETED updates are purged after 14 days and
  TERMINAL_FAILURE after 30 days in bounded batches of at most 500 per maintenance pass.
  RECEIVED/PROCESSING updates are never age-purged (they may be unfinished user work) and instead
  surface as `inbound_updates_stuck` when older than one hour. Cleanup is transaction-safe,
  idempotent, and integrated into the existing maintenance job; retention is configurable under
  `operations.inbound_updates`.
- Make recoverable-job requeue gradual instead of a burst: cookie remediation requeues one bounded
  batch (default 20) per pass and the existing maintenance lifecycle keeps draining the backlog
  until it is exhausted — no repeated cookie upload needed. Recovery is provider-isolated,
  oldest-first with a per-user cap for fairness, and bounded by max age (7 days) and max attempts.
  A durable per-provider marker remembers that a fresh cookie is available, and SQLite-first
  transitions keep Redis loss recoverable.
- Recovery queue pressure is outstanding-queue aware: ARQ `queue.max_jobs` is worker concurrency
  (a job stays in the queue sorted set while waiting, running, or deferred/retried — removed only
  at final success/failure), so `queue_depth()` counts outstanding ARQ queue entries. The pressure
  threshold is derived as `queue.max_jobs * queue_backlog_per_worker_slot` (default multiplier 4),
  with an explicit `queue_pressure_threshold` override. Automatic historical recovery only fills
  the spare headroom below the threshold (each batch trimmed to
  `threshold - current_outstanding_depth`) and defers entirely once depth reaches it; fresh user
  traffic keeps its existing admission behavior.
- App-fix startup recovery, cookie remediation, and maintenance backlog draining are bounded the
  same way; requeue reconciliation is durable-state repair (SQLite is source of truth, Redis is
  the execution queue) and always converges. Effect ledger rows are bounded and purged with the
  maintenance lifecycle.
- Stuck inbound-update and effect observability: aggregate metrics and structured logs surface
  unexpectedly old unfinished updates and stale pending effects without update/user identifiers.

### Development tooling

- Add project-scoped Graphify navigation guidance and exclusions, compact agent routing indexes,
  validated subsystem Skill trees under `.agents/skills/`, and a deterministic standard-library
  `scripts/agent_context.py` fallback for symbols, imports, reverse imports, references, and likely
  tests. Graphify remains optional
  developer tooling; source/tests/docs remain authoritative and production/CI have no Graphify
  service or runtime dependency.
- Replace unconditional whole-document-tree preloading with task-directed progressive discovery
  while preserving every architecture, security, cleanup, cancellation, release, documentation,
  lint, type, test, and completion safeguard. CI now checks the routing contract deterministically.

## 1.3.3 - 2026-08-16

### Added

- Bounded, cancellable Local Telegram Bot API startup readiness wait for the bot and worker.
  Service-owned managed and external endpoints are probed immediately, then retried with
  exponential backoff up to the configured `startup_timeout_seconds` deadline. Structured events
  `local_api_startup_wait`, `local_api_startup_ready`, and `local_api_startup_timeout` are emitted;
  a permanently unavailable endpoint still fails non-zero after the bounded deadline.
- First-class Instagram Story support. Story URLs with an exact media id canonicalize to
  `/stories/USERNAME/MEDIA_ID/` (tracking parameters stripped) and gallery-dl is the primary
  engine: image stories use the existing photo/file delivery, video stories expose a new
  `video_original` option and download the original MP4 natively through the Local Bot API.
- Instagram profile-avatar downloading. A plain `instagram.com/USERNAME/` URL classifies as a
  profile-avatar action and canonicalizes to the internal `/USERNAME/avatar/` gallery-dl target,
  so it never silently downloads the account's post history. The avatar is delivered as an
  original image with document/file delivery supported.
- Explicit Instagram URL routing contract: post `/p/`, reel `/reel/` and `/reels/`, story
  `/stories/USER/MEDIA_ID/`, story account `/stories/USER/` (rejected as bulk), profile
  `/USERNAME/` (avatar action), avatar `/USERNAME/avatar/`, and highlights.

### Fixed

- Gallery-dl inspections no longer conflate "no image entries" with "no downloadable media": the
  typed result model now carries IMAGE, VIDEO, and mixed collections, so video-only Stories,
  Reels, and video posts stay gallery-owned instead of falling back to yt-dlp.
- The production false `too_large`: a silent video-only story (no audio stream) previously made
  the bounded format selector raise `MediaTooLargeError("No complete configured format fits the
  size limit")` after the ~6.35 MB story video had already been downloaded. That condition is now
  classified as `NativeFormatUnavailableError`; genuinely oversized complete selections still
  raise `MediaTooLargeError`. Story URLs with a media id also no longer fall back to the yt-dlp
  account-story playlist that downloaded every current story.
- Job failures after an adapter has processed the request now carry the provider source on the
  durable record instead of reporting `source = null`.
- Empty Instagram Story output is classified as unavailable (`MediaUnavailableError`) instead of a
  generic parser-output error, while authentication and expired-cookie failures keep their
  dedicated categories.

## 1.3.2 - 2026-08-15

### Fixed

- Split Linux updater verification into explicit candidate/static, offline post-install, and
  conditional post-start online phases. The offline phases no longer probe the intentionally
  stopped Local Bot API, Telegram bot, required channels, or worker processing.
- Keep offline verification fail-closed for Python/package version, yt-dlp, gallery-dl, canonical
  cookies, ffmpeg/ffprobe, Deno, Local Bot API static configuration/filesystem/migration state,
  chart/font resources, and multipart 7-Zip support.
- Online-verify only services that were running before the update: Local Bot API reachability for a
  restored `local-api`, Telegram/required-channel reachability for a restored `bot`, plus existing
  Compose health and exact service-state checks. Offline or online verification failure performs
  the existing full application/image/permission/service-state rollback.
- Handle operator SIGINT during candidate preflight with exit status 130 and a concise message,
  without exposing a child Python `KeyboardInterrupt` traceback or changing the installed release.

### Operations

- Add privileged v1.3.0-to-v1.3.2 regressions for the production all-running topology, offline and
  online verification failures, Local API/bot intentionally stopped, mixed service states,
  redacted diagnostics, and exact restoration. The v1.3.1 backup ordering, exact-log exclusion,
  atomic publication, Redis availability, and project-scoped cleanup contracts remain intact.

## 1.3.1 - 2026-08-15

### Fixed

- Stop only the bot, worker, and Local Bot API services that were running before taking the Linux
  update backup. This prevents a concurrently written Local Bot API log from making GNU tar abort
  with `file changed as we read it` while Redis remains online in its persistent named volume.
- Make update backups private and atomic, remove incomplete archives on failure, and exclude only
  the audited volatile `data/telegram-bot-api/telegram-bot-api.log` path. Configuration, `.env`,
  cookies, SQLite including WAL/SHM, and other Local Bot API state remain in the archive;
  downloads/temp remain untouched in place under the existing contract.
- Restore the exact original project-service state after writer-stop, backup, installation,
  permission, offline-doctor, startup-health, or state-verification failures. Services intentionally
  stopped before the update remain stopped.

### Operations

- Download, checksum, validate, and pre-pull the candidate before downtime; run version and doctor
  checks before candidate writers start; and expose bounded, secret-redacted diagnostics only for
  the failed stage.
- Add mocked and privileged regressions for moving Local Bot API logs, backup archive policy,
  backup/doctor rollback, all-stopped and mixed service states, and the checksummed standalone
  updater path required to bootstrap an affected v1.3.0 installation.

## 1.3.0 - 2026-08-14

### Added

- Add a private-chat, administrator-only cookie management panel that accepts bounded Netscape
  documents, detects supported services from cookie domains, and exports the complete canonical
  server `cookies.txt` on demand.

### Security

- Merge uploaded records by normalized domain, path, and name while preserving unrelated cookie
  lines exactly. Updates create a restricted atomic backup, retain owner/group/mode, and atomically
  replace the canonical file without logging filenames, cookie names, values, or contents.
- Resolve yt-dlp, SoundCloud, and every enabled gallery-dl provider to that same canonical file.
  Legacy gallery cookie entries are accepted only when they identify the canonical path, preventing
  an admin update from leaving an active runtime consumer on stale cookie state.

### Operations

- Reopen the canonical cookie path for every subsequent inspection/download job, so an atomic
  administrator update reaches yt-dlp and gallery-dl without a container restart.
- Preserve legacy `gallery_dl.cookies.*` configuration keys as aliases. Deployments with divergent
  legacy files must merge them into the combined `yt_dlp.cookies_file` and then use null or
  identical aliases; deployments already configured only with the canonical path need no migration.

## 1.2.2 - 2026-08-14

### Fixed

- Validate an update with the prepared release image before stopping services, mounting the
  existing configuration and persistent `/data` filesystem read-only so runtime-valid cookie
  paths remain visible to gallery-dl checks under the configured runtime UID/GID.
- Add a non-mutating `config-check --read-only-runtime` mode for updater preflight. Cookie
  readability remains fail-closed, while Local Bot API directory validation does not write to
  SQLite, migration, download, cookie, or other persistent runtime state.
- Publish a checksummed standalone updater asset for the one-time v1.2.1 bootstrap, whose installed
  updater necessarily runs its old preflight before it can replace application files.
- Preserve the existing owner and restrictive mode of `.env` when an elevated updater changes the
  pinned image. Privileged fixtures now derive their final write/SQLite probe from the configured
  Compose runtime UID/GID and assert the exact post-migration owner/mode and bind-mount contract.
- Cover default Instagram fallback, explicit gallery-dl cookies, missing and unreadable files,
  disabled gallery-dl, byte-identical configuration/cookies, and previous-release updater layouts.
- Isolate each privileged updater fixture in a unique Compose project and remove only its own
  temporary containers, network, named volume, registry, and filesystem root.

## 1.2.1 - 2026-08-13

### Fixed

- Resolve mixed Instagram carousel videos from yt-dlp's raw, unprocessed parent entries so photo
  children with no video formats cannot abort discovery before a valid video child is reached.
- Require the raw yt-dlp entry count and every detected video source ordinal to match gallery-dl's
  complete media plan before downloading any gallery image; then download only the validated
  public Instagram video-child URLs through the existing yt-dlp adapter.
- Preserve strict failure semantics without enabling yt-dlp `ignoreerrors`, and retain all source
  order, transient-URL isolation, cleanup, Twitter HLS, and exact-byte image behavior from 1.2.0.

## 1.2.0 - 2026-08-13

### Added

- Ask users whether Instagram images should be delivered as Telegram photos or byte-preserving
  documents before creating the download job; the typed choice survives SQLite, ARQ, worker
  recovery, download planning, and delivery.
- Deliver ordered Instagram carousels in deterministic groups of at most ten. Photo mode uses
  photo/video albums; document mode uses ordered document and video runs so no item is converted,
  reordered, or dropped by Telegram media-group type restrictions.
- Split mixed Instagram posts across the existing isolated engines: gallery-dl downloads only the
  original image assets while yt-dlp downloads every video from the canonical post URL. Exact
  source ordinals are reconciled fail-closed before delivery.
- Notify every uniquely configured Telegram administrator when an inspection or download reaches
  a final failure or uncertain-delivery state. Alerts expose only the opaque job ID, job kind,
  normalized source, terminal status, stable error category, and attempt number.

### Operations

- Reverified zero-retention cleanup for successful, failed, cancelled, timed-out, and
  delivery-uncertain workspaces, including both download and temporary job directories. Durable
  job records continue to follow the configured retention period.
- Keep administrator alert failures isolated from job state, user notifications, other
  administrators, and workspace cleanup; intermediate retries and user cancellations do not alert.

### Security

- Keep gallery-dl CDN and `ytdl:` pseudo-URLs transient, preserve signature/decompression and
  workspace checks, and never rewrite validated production image bytes.

## 1.1.1 - 2026-08-12

### Fixed

- Explicitly request gallery-dl JSON Lines for every machine-consumed inspection and strictly parse
  directory and URL message tuples instead of relying on the default pretty-printed outer array.
- Recognize gallery-dl `ytdl:` video events without persisting their pseudo-URLs, allowing
  video-only Instagram Reels to fall back to yt-dlp while image and mixed posts remain gallery-owned.

## 1.1.0 - 2026-08-01

### Added

- Added original image and ordered mixed-media downloads for single Instagram, TikTok, Twitter/X,
  and Pinterest posts through an isolated, exactly pinned gallery-dl 1.32.8 subprocess adapter.
- Added semantic single-image, all-images, all-media, images-only, videos-only, and original-image
  ZIP choices; YouTube thumbnails and SoundCloud artwork remain on the yt-dlp path.
- Added signature-based Pillow image validation, chunked Telegram photo/mixed albums, deterministic
  document fallback, and reuse of the existing multipart archive path for oversized ZIPs.
- Added per-source cookie settings with backward-compatible Instagram cookie fallback, runtime
  health/doctor/config checks, sanitized 1.32.8 fixtures, dependency-upgrade contract tooling,
  package/Docker license smokes, and isolated Renovate updates.

### Security

- Reject bulk social URLs, bound asset count/bytes/runtime/concurrency/output, terminate gallery-dl
  process groups on cancellation, confine outputs to the job workspace, and persist no signed CDN
  URL or raw vendor metadata.

### Fixed

- Preserve the current Twitter/X HLS audio-only metadata inference and exact H.264/AAC inspected
  stream IDs through durable selection and stream-copy download.
- Classify an empty native codec/container plan separately from deleted/private media, retain the
  normalized source on planning failure, and emit URL-free structured diagnostics.

## [1.0.11] - 2026-08-01

### Fixed

- Fixed admin media requests getting stuck after inspection was queued.
- Separated the persistent admin reply keyboard from editable inspection status messages.
- Added a fallback message when Telegram cannot edit an inspection status message.
- Reconciled existing inspection and Instagram download jobs with ARQ when Redis state is missing.

## 1.0.10 - 2026-07-30

- Fixed blank chart titles and labels by replacing the raw-pixel renderer with an in-memory Pillow
  dashboard containing the report title, exact date range, timezone, KPI labels/values, readable
  legend, numeric Y axis, adaptive date labels, and important bar values.
- Bundled deterministic Noto Sans and its SIL OFL 1.1 license inside the wheel, sdist, and
  production image. Rendering never downloads a font or depends on host font packages/fontconfig.
- Added weekly/monthly visual-region tests, clean-wheel resource validation, `tmb doctor` font and
  renderer checks, and non-root/read-only/offline Docker chart smoke artifacts.
- Preserved media zero-retention and all administrator, required-channel, configuration, and
  durable-state behavior unchanged.

## 1.0.9 - 2026-07-30

- Give configured administrators a persistent role-aware management keyboard from `/start`,
  `/menu`, or the backward-compatible `/panel`, with authorization repeated for every management
  message and report callback.
- Route both the admin download prompt and direct admin URLs through the unchanged public
  inspection, selection, queue, worker, delivery, cancellation, and zero-retention pipeline.
- Add single-flight weekly/monthly PNG reports and a complete usage report with Tehran-local daily
  breakdowns, while excluding current administrator IDs from public KPIs without deleting durable
  activity records.

## 1.0.8 - 2026-07-29

- Enforce job-scoped zero-retention after confirmed delivery, failure, cancellation, timeout, and
  uncertain delivery; cleanup is idempotent, symlink-safe, observable, and repeated by startup and
  maintenance sweepers without touching active jobs or shared state.
- Delete each delivered multipart volume immediately after its Telegram receipt is persisted,
  terminate active 7-Zip process groups on cancellation, and retain no media/archive workspace
  after a terminal job.
- Add safe `tmb cleanup [--dry-run]` workspace/container/image cleanup and remove only unused old
  images from `ghcr.io/hamedsanaei/telegram-media-downloader-bot` after successful health, runtime
  version, doctor, and status verification. Referenced/current images, other repositories, volumes,
  and global build caches are never pruned.

## 1.0.7 - 2026-07-29

- Canonicalize YouTube watch, short, live, and `youtu.be` video URLs containing Mix or playlist
  parameters before persistence, queueing, inspection, and download.
- Force single-video inspection and download with yt-dlp `noplaylist` whenever a valid YouTube
  video ID is present, while preserving genuine `/playlist?list=...` behavior.
- Prevent unnecessary YouTube Mix expansion and its associated Deno/CPU cost for links targeting
  one video, including retry, recovery, and legacy persisted-job execution.

## 1.0.6 - 2026-07-29

- Expanded MP4 Native to include real AV1/AAC and H.264/AAC plans; Native is now defined by zero
  video/audio transcoding, with stream-copy merge/remux remaining permitted.
- Added truthful codec-aware option labels and kept AV1 and H.264 plans distinct when they share a
  resolution. Selected codec families remain durable across SQLite, Redis, restart recovery, and
  download-time validation.
- Made the Best Original summary point to an actually selectable Native plan and report that
  plan's resolution, container, codec, and selected-stream size.
- Route non-inline AV1 MP4 through document delivery without re-encoding; runtime-image smoke tests
  assert both native AV1 and H.264 visibility, zero transcoding, no `libx264`, and stream-copy args.

## 1.0.5 - 2026-07-29

- Made every public video choice native-only: Telegram now exposes only H.264/AAC MP4 Native and
  VP9/Opus WebM Native options, while generic MP4/WEBM and every video plan requiring transcoding
  remain hidden.
- Added deterministic Back navigation that reuses the persisted inspection selection without
  creating another inspection, SQLite job, or Redis enqueue.
- Built labels from actual selected resolution, FPS, dynamic range, codecs, and selected-stream
  sizes; exact, approximate, and unknown sizes remain distinguishable.
- Deduplicated requested qualities and Best Original when they resolve to the same real streams,
  so lower-resolution native fallbacks cannot create false 2160p/1440p buttons.
- Versioned callback payloads use short opaque option identities. Legacy generic or converted
  callbacks are rejected safely and return users to the Native menu without starting FFmpeg.

## 1.0.4 - 2026-07-28

- Prepared patch `1.0.4`: ordinary MP4 choices now rank native H.264/AVC + AAC ahead of
  bitrate/quality, may deterministically fall back to a lower compatible resolution, and never
  hide an AV1/VP9-to-H.264 transcode.
- Added an opt-in, disabled-by-default converted-MP4 choice with a conservative preflight timeout
  estimate; native WebM and `best_original` remain codec-preserving.
- Added structured native-selection/fallback logging and deterministic production-like fixtures
  for MP4, WebM, stream-copy, timeout rejection, and legacy callback/config compatibility.

## 1.0.3 - 2026-07-27

- Prevented the updater from replacing its own executing script, added mandatory full-script Bash
  syntax validation, and restored executable modes plus the verified global `tmb` symlink after
  release extraction.
- Made updates transactional: validate staged scripts/Compose/config before stopping services,
  replace application entries through a rollback snapshot, and restore the prior source, image,
  usable permissions, command link, and service set after any post-stop failure.
- Repaired SQLite/data ownership and private modes using the resolved runtime UID/GID, then added a
  real runtime-user write probe and SQLite `PRAGMA journal_mode = WAL` probe before service start.
- Added bounded container restart attempts and post-start Bot/Worker/Local API health verification;
  a crash/restart state now stops the affected service and rolls the update back.
- Added filesystem-level and privileged Docker upgrade regression coverage for lost archive modes,
  real SQLite/WAL access, preserved config/state/media, command repair, and rollback behavior.

## 1.0.2 - 2026-07-27

- Made user cancellation durable and idempotent across SQLite and ARQ: cancelled queued/running/
  retrying jobs are never recovered, official ARQ abort is enabled, stale transient keys are
  finalized, and shutdown races no longer requeue an already user-cancelled FFmpeg job.
- Added conservative FFmpeg controls (two encoder threads, one concurrent transcode, 25-minute
  timeout, an operator disable switch), process-tree termination without orphan FFmpeg processes,
  structured transcode progress, and optional `TMB_WORKER_CPUS`.
- Made `tmb update` normalize database and Local Bot API ownership/restrictive permissions before
  restart, repair the global `tmb` command, and leave services stopped with the prior image restored
  if permission migration fails. Existing configuration, `.env`, SQLite/Redis state, cookies,
  downloads, and Local Bot API state remain preserved during the v1.0.1-to-v1.0.2 upgrade.
- Guaranteed compatible `7zz`/`7z` commands in the runtime image and added a real multipart archive
  smoke test to CI and the release workflow.
- Shared the expensive Telegram Local Bot API BuildKit stage between CI and release builds while
  retaining full runtime-image, Compose, dependency-doctor, and multipart smoke validation.

## 1.0.1 - 2026-07-27

- Prevented Instagram `best_original` VP9/MP4 downloads from being re-encoded toward the upload
  ceiling; original mode is now always native-only in durable jobs, queue payloads, and engine
  requests.
- Made Instagram `force_mp4` select and merge native MP4 video plus M4A audio, while disabling it
  preserves the source-selected container.
- Separated native-container, inline-video, and document compatibility and route non-streamable
  native MP4 files to document delivery without changing their source bytes.
- Changed forced video conversion to quality-first CRF encoding with bitrate-limited fallback only
  for actual ceiling or anti-inflation violations, with structured source/target/size logging.
- Prevented a several-megabyte Instagram input from expanding toward the configured upload ceiling
  solely because that ceiling is large.

## 1.0.0 - 2026-07-23

- Added queued metadata inspection and owner-bound expiring semantic format selection.
- Added progress throttling, cancellation, active-job deduplication, classified retries, and safe
  uncertain-delivery handling.
- Added public-network URL/DNS enforcement, Redis rate limiting, durable blocks, and admin commands.
- Added typed audio/video/document delivery with fallback, sanitization, upload limits, local Bot API
  support, bounded playlist ZIPs, ffmpeg, and pinned Deno.
- Added SQLite/WAL job persistence, restart recovery, scheduled cleanup, structured redacted logs,
  health/readiness, Prometheus metrics, controlled upgrades, canary comparison, and plugin SDK.
- Added fail-fast cross-platform release scripts, secret scanning, and dependency vulnerability
  auditing; upgraded pytest to its fixed 9.x line.
- Kept generic inspection size estimates advisory while enforcing configured limits on the selected
  download and final post-processed file.
- Prevented partial audio-only delivery for oversized video selections by choosing the best complete
  configured video/audio pair below the aggregate size limit.
- Added a dedicated configurable Telegram file-upload timeout instead of the 60-second general
  aiogram session default.
- Preserve distinct requested video resolutions with SDR source selection and bounded H.264/AAC
  transcoding when the native result exceeds Telegram's upload ceiling.
- Added managed/external Telegram Local Bot API lifecycle, config-only credentials, explicit
  idempotent migration/rollback, shared Bot/Worker endpoint leases, safe diagnostics, and a
  practical 1900 MB upload ceiling without forced transcoding below that ceiling.

## 0.1.0 - 2026-07-23

- Added the initial layered project foundation.
- Added strict local YAML configuration.
- Added isolated yt-dlp engine adapter.
- Added aiogram bot, ARQ worker, Redis Compose service, and management scripts.
- Added Codex implementation specifications, security rules, tests, and CI.

## 0.1.1 - 2026-07-23

- Changed the Python baseline from 3.12 to Python 3.14 or newer.
- Added configurable Docker `PYTHON_VERSION` through `.env`.
- Updated CI, uv lock helper images, Ruff, mypy, documentation, and Codex instructions.
