# Project status

Last updated: 2026-08-31

## Release state

Release v1.3.7 is withdrawn as a known-broken production release without rewriting its Git tag or
history. Fresh Linux/Windows installers and both platform updaters now reject direct `v1.3.7` or
`1.3.7` targets before download and independently reject verified archives reporting package
version `1.3.7` before installation, configuration/state mutation, image pull, backup, or service
stop. The target-only denylist preserves the required v1.3.7 -> v1.3.8+ recovery path. A canonical
repository policy, enforced standalone snapshots, release-build/publish guards, platform recovery
fixtures, and operator documentation make later withdrawals explicit without adding a remote
revocation dependency. The existing GitHub Release is marked prerelease as
`v1.3.7 — BROKEN / DO NOT USE`, its warning is first, and its six downloadable assets were removed;
the annotated Git tag and commit remain unchanged. GHCR `v1.3.7`/`1.3.7` still resolve to their old
isolated digest because the authenticated GitHub token lacks the `read:packages` scope required to
verify the exact package-version tag set before a safe deletion. Moving `latest`/`1.3` and immutable
`v1.3.8`/`1.3.8` remain on the v1.3.8 digest.

Patch 1.3.8 preserves the aiogram 3.30.0 ``Default`` serialization crash fix while keeping outbound
Bot defaults out of inbound snapshots. Durable polling now serializes and persists in Telegram
order, stops the batch at the first unresolved serialization gap, and requests that exact update ID
again before any later update can become durable or handler-visible. A terminal serialization
quarantine is documented as a sanitized audit tombstone that deliberately abandons handler
processing after the bounded threshold, not as preservation of the original update payload.

Delivered media now carries the durable canonical job URL as bottom-most Persian source metadata
without replacing any existing caption content. Direct photo/video/audio/document delivery,
document fallback, Instagram albums and individual items, Stories/Highlights batches, and
multipart parts all receive the same persisted `JobRecord.url`. The caption planner preserves
fixed attribution and per-item/part lines, reduces only oversized title text, keeps the URL whole,
and uses a receipt-associated reply only when the 1024-character media-caption limit cannot fit the
complete source line. No source URL is added to logs or metrics.

Tasks T001 through T014 are implemented. Patch 1.3.6 repairs the production yt-dlp inspection
failure on read-only application filesystems without changing
dependency/runtime topology or the passive Cookie Health architecture: `inspect_options()` now
gives every inspection a private scratch workspace under the configured storage temp hierarchy
(`paths.home` and `paths.temp` both set, created before `extract_info()`, removed after the run,
reclaimed by the orphan sweep if a crash leaks it), so yt-dlp `_check_formats` tempfile usage can
never fall back to `/app`. Local filesystem failures (EROFS/EACCES/EPERM/ENOSPC and other local
path/I/O errnos) map to the new typed terminal `LocalRuntimeError` with category
`local_runtime`, safe path-free reasons carrying only exception class and errno, while network-
shaped OSErrors keep their retryable remote-failure classification. The yt-dlp engine attaches
`adapter`, pipeline stage (`inspection`/`extraction`/`download`), and URL-provable source onto
mapped errors, and workers honor that stage hint whenever no specialized classification exists,
so admin failure alerts no longer report anonymous "unknown/internal/Media download failed"
diagnoses for infrastructure failures. A committed network-free
`inspection_workspace_smoke` module reproduces the production conditions (read-only root
filesystem, no usable ambient temp dir) inside the real container and is enforced in CI.
Patch 1.3.5 corrects the production Instagram inspection
and cookie safety regressions without changing dependency/runtime topology: gallery-dl
successful-empty Instagram inspections are conservatively unavailable and make no second
diagnostic request; Pinterest/SoundCloud session cookies are present and UNVERIFIED; canonical
writes are verified and followed only by targeted local health refresh; every automatic provider
probe, Cookie Health cron/watcher, and admin live-check action is removed. Old probe config keys are
accepted and ignored. Same-request real extraction auth failures still persist AUTH_FAILED, and
unchanged Cookie Health Telegram edits remain idempotent. Patch 1.3.4 added rich structured administrator
failure diagnostics (typed `FailureContext` threaded from the failing layer through retries to
the terminal admin notification, with a central secret sanitizer), an admin-only Cookie Health
Center (now passive/static-only with persisted state-transition alert deduplication and runtime
auth-failure alerts), Instagram bulk Stories (single exact item vs all active stories,
batch delivery with per-item isolation and a final summary), first-class Instagram Highlights
(direct `/stories/highlights/ID/` URLs plus a paginated profile highlight browser), and
Instagram cookie-health gating before bulk collection jobs. v1.3.3 had added first-class
Instagram Story support and profile-avatar downloading while keeping the exact-media-id Story
contract; patch 1.3.2 corrected the post-install verification lifecycle.

An additive progressive-context layer is ready: root engineering rules stay mandatory, compact
indexes route each task, Graphify provides bounded local structural queries, and source/tests/
detailed documentation remain authoritative. Six validated subsystem Skill trees live under
`.agents/skills/`. The optional graph has no production or CI dependency; a standard-library AST
fallback and deterministic CI guard cover environments without Graphify. Runtime behavior is
unchanged.

Milestone 4 decomposes future VIP subscriptions, provider-neutral billing, encrypted per-user
Instagram credentials, authenticated public/private routing, secure account linking, and rollout
work into T014-T025. ADR-032's entitlement foundation (T014) and provider-neutral
payment-confirmation decisions (T015) are accepted; ADR-033 through ADR-035 remain proposed. T024
is blocked until a real payment provider is selected, so production purchasing remains blocked.

T015 implements the provider-neutral billing foundation in the working tree: typed payment
domain models (`domain/payments.py`), a gateway/persistence port set
(`application/ports/payments.py`), a deterministic test-only fake gateway, an additive WAL SQLite
payment store (`payment_orders`, `payment_attempts`, and unique
`provider_transaction_claims`), and the `BillingService` (`application/services/billing.py`).
Order creation snapshots commercial facts (plan, duration, capabilities, amount, currency) so a
later price change never alters an existing order. Confirmation and refund each run in one
`BEGIN IMMEDIATE` transaction that also creates/reverses exactly one T014 entitlement grant and
recomputes the subscription from remaining grants. Redirect-only and uncertain/timeout provider
states never activate VIP, duplicate/concurrent/replayed callbacks never grant twice, and no real
gateway, pricing, callback route, or card/credential storage is introduced. T014 entitlement
authority, grant, and recomputation semantics are reused, not duplicated.

Milestone 5 planning decomposes a future Operator Logger and private Telegram audit-channel
subsystem into T026-T032. The plan covers typed events, config/runtime destination reconciliation,
durable asynchronous outbox delivery, admin channel UX, migration of operational error/Cookie Health
alerts, accepted-submission mirroring, privacy notice, indefinite retention, secret exclusion, and
end-to-end rollout. ADR-036 through ADR-038 are proposed, not accepted. This is documentation-only:
no Python behavior, schema, migration, dependency, Compose service, configuration model, package
version, tag, release, deployment, or history changed.

T033 implements the fast-feedback CI tiering in the working tree: a repository-owned deterministic
changed-path classifier (`scripts/ci_change_policy.py`), a fast `quality` lane for ordinary
source/documentation changes, conditional heavy lanes (dependency/package/plugin-sdk/
docker-runtime/updater-integration/installer-linux/installer-windows), and an always-evaluated
`final-ci-gate` that understands success/failure/cancelled/skipped and aggregates all relevant
lanes. `final-ci-gate` is the merge-blocking branch-protection required check (`quality` may also
be required for visibility; `quality` + `change-detection` alone is not a sufficient gate).
Development runs cancel superseded same-ref work; permission/license gates still require human
review. The tag-only publication workflow is unchanged. Post-implementation GitHub run timing is
pending the first push.

Patch 1.2.2 fixed updater preflight validation for runtime cookie paths without mutating persistent
data, patch 1.2.1 fixed strict mixed-carousel video-child discovery, and release 1.2.0 introduced
Instagram Photo/File delivery plus redacted administrator failure alerts. The v1 flow is URL
validation -> queued inspection ->
owner-bound semantic selection -> durable download job -> throttled progress/cancellation -> typed
Telegram delivery -> terminal state and cleanup.

Patch `1.0.10` replaces the text-incapable raw RGB chart encoder with a deterministic Pillow
dashboard and package-bundled licensed Noto Sans resource. Visual-region, package, doctor, and
non-root Docker smoke checks prevent blank chart labels. Existing v1.0.0 through v1.0.9
configuration and durable runtime state remain upgrade-compatible.

The gallery bundle implementation also preserves Twitter/X HLS planning for the upstream audio-only MP4
metadata shape that omits `acodec`. It preserves the exact inspected H.264/AAC format pair through
SQLite/ARQ and downloads it with stream-copy remux; planning failures now retain the normalized
source and use a distinct operator category without logging URLs or raw extractor dictionaries.

The v1.1.0 work adds a pinned gallery-dl subprocess boundary for ordered original images and mixed
posts on Instagram, TikTok, Twitter/X, and Pinterest. Image-bearing posts remain gallery-owned;
video-only posts use yt-dlp. Durable selection stores stable asset identities but no CDN URL, and
delivery supports photo, chunked media groups, documents, deterministic image ZIP, and the existing
multipart path. YouTube thumbnails and SoundCloud artwork remain yt-dlp actions.

The v1.2.0 work persists a typed Photo/Document choice only for Instagram inspections containing
images. Mixed posts download original images with gallery-dl's Instagram video output disabled and
download videos through yt-dlp from the canonical post URL, then reconcile exact counts and merge
safe source ordinals. Photo/video albums and ordered document/video runs are capped at ten items;
document images retain the exact validated gallery-dl bytes, filename extension, and format.
Terminal inspection/download failures and uncertain deliveries now generate a redacted private
alert for every uniquely configured administrator after retries are exhausted. Cancellation,
intermediate retries, URLs, user/chat IDs, filenames, paths, and raw exceptions stay outside the
alert contract, and one unreachable administrator cannot affect other alerts or job cleanup.

Final v1.3.0 Python, architecture, security, package, contract, Compose, and Docker results are
recorded in `docs/HANDOFF_REPORT.md` after the release-quality run.

## Milestone 4 foundation (T014)

Implemented the provider-neutral VIP entitlement/subscription foundation without payments or
Instagram behavior. New typed domain (`SubscriptionPlan`, `Subscription`, `EntitlementGrant`,
`SubscriptionStatus`, `Capability`, `EntitlementSnapshot`), UTC calendar-month arithmetic with
end-of-month clamping, deterministic stacking/reversal recomputation, and a fail-closed
`EntitlementService.authorize()` that never reads `UserProfile.is_premium`. Additive SQLite/WAL
tables (`subscription_plans`, `subscription_plan_capabilities`, `subscriptions`,
`entitlement_grants`) plus a nullable `entitlement_snapshot` JSON field on jobs; the commercial plan
catalog is empty by default. A unique `(user_id, source_type, source_reference)` index gives a
future payment provider exactly-once grant creation. Legacy databases upgrade without rewriting or
deleting rows, free users need no subscription row, and Redis is never economic truth. An accepted
protected job keeps its durable snapshot after expiry while automatically created child jobs
inherit it; later user callbacks reauthorize. No version/tag/release/deployment was created.

## Implemented production controls

- Python 3.14.5, committed `uv.lock`, immutable Docker build, non-root/read-only app containers, and
  pinned Deno 2.9.3 plus ffmpeg.
- Secret scanning and a locked-environment `pip-audit` vulnerability gate in local release checks
  and CI.
- Strict local YAML configuration with schema, path containment, unknown-key rejection, and no
  secret-bearing environment variables.
- Separate aiogram bot and ARQ worker; no download or direct yt-dlp call in polling handlers.
- Project-owned engine, persistence, queue, delivery, URL-validation, and rate-limit contracts.
- SQLite/WAL durable state, active-job deduplication, transition history fields, restart recovery,
  delivery-uncertainty quarantine, dynamic blocks, and scheduled cleanup.
- Public-network URL/DNS enforcement before enqueue and inside the yt-dlp adapter for extracted URLs.
- Semantic format UI, bounded playlist ZIP delivery, media-method fallback, local Bot API support,
  explicit size/duration/playlist limits, and filename/caption sanitization.
- Structured redacted logs, request/job correlation, admin commands, internal health/readiness, and
  bounded-label Prometheus metrics.
- Managed and external Telegram Local Bot API modes with a 1900 MB practical ceiling, config-only
  credentials, explicit durable migration/rollback, shared Bot/Worker endpoint leases, lifecycle
  CLI, and Local API readiness.
- Controlled yt-dlp upgrade reports, per-source opt-in contracts, canary failure-rate gate, and an
  independent external extractor plugin template.
- Fail-closed all-channel membership with Redis positive/negative cache and admin bypass.
- yt-dlp-only HTTP(S)/SOCKS proxy switching with legacy behavior and secret-safe configuration.
- Two-stage zero-transcode AV1/H.264 MP4 and VP9 WebM selection with actual-plan labels, opaque versioned
  callbacks, deduplication, deterministic Back navigation, automatic best-MP4 Instagram
  multi-video delivery, and runtime bot attribution in every caption.
- Permanent SQLite user profiles, daily counters, and job-idempotent delivery byte accounting.
- Docker-first Linux/Windows installers with SHA-256-verified release archives, version-pinned
  images, interactive `tmb` management, official pinned Local Bot API source build, dedicated
  Local API service, and a tag-gated GHCR amd64 release workflow. CI and release builds share a
  scoped GHA BuildKit cache for the expensive Telegram API compilation stage.
- Failed interactive configuration writes remove their secret-bearing temporary file; the exact
  temporary filename is also ignored by Git.
- Linux `tmb update` now completes release/network preflight before downtime, records all four
  project services, stops only running filesystem writers, publishes a private atomic backup, and
  restores the exact original service set after backup or later transaction failures. Redis/ARQ
  remains online, while intentionally stopped services remain stopped.
- Linux updater verification has three explicit phases: read-only candidate static preflight,
  post-install offline static verification with writers stopped, and post-start live checks selected
  from the original `local-api`/`bot` service set. The normal `tmb doctor` remains comprehensive.
- User cancellation is terminal in SQLite before official ARQ abort; cancelled rows are excluded
  from startup recovery, finalized queue keys and job directories are cleaned idempotently, and
  shutdown races do not requeue user-cancelled FFmpeg work.
- FFmpeg conversion defaults to two encoder threads and one concurrent encode, with configurable
  timeout/disable controls, process-group termination, structured progress, and an optional Compose
  worker CPU quota.
- Linux updates repair owner-only permissions for persistent runtime paths and the global `tmb`
  command before restart. The image guarantees `7zz`/`7z`, and CI/release execute multipart smoke
  archives.
- Linux updates execute from an isolated runner, validate the complete staged Bash/Compose/config
  payload before stopping services, atomically replace top-level application entries with rollback
  snapshots, probe real runtime-user writes plus SQLite WAL, verify post-start health, and restore
  the prior application/image/permissions/service set on failure. Every production service
  (`bot`, `worker`, `local-api`, `redis`) uses the `unless-stopped` Compose restart policy, so
  crashed containers and the full stack recover automatically after Docker daemon restarts and
  host reboots; explicit operator stops remain intentional.
- Terminal job workspaces are removed idempotently after success, failure, cancellation, timeout,
  or uncertain delivery; startup and maintenance sweepers preserve active jobs while reclaiming
  stale or terminal directories, with structured cleanup metrics.
- Final job failures and delivery uncertainty are proactively sent to every unique configured
  administrator using opaque IDs and stable categories; `/failed` remains the durable fallback.
- Verified updates and `tmb cleanup [--dry-run]` may reclaim only unreferenced old project-image
  IDs and superseded stopped project containers. Current/referenced images, other repositories,
  volumes, and build caches are protected.
- Administrators see a persistent `/start`/`menu` management keyboard, while ordinary users never
  receive management buttons. Admin URLs use the ordinary inspection/download pipeline, every
  management action is reauthorized, and reports exclude current admin IDs without altering jobs.
- Private-chat cookie management validates bounded Netscape documents, detects supported services
  without trusting filenames, atomically merges only matching service records into the canonical
  cookie file, and exports the complete file only to a currently configured administrator. yt-dlp,
  SoundCloud, and every gallery-dl provider use that one effective path on their next job; divergent
  legacy source aliases fail before startup.
- Progressive agent navigation is available with task-specific indexes/Skills, local Graphify
  queries, and a bounded AST fallback without removing any architecture, testing, security,
  cleanup, cancellation, update, release, documentation, or completion safeguard.

## Verification

The v1.3.3 Python suites passed 528 Linux non-contract tests (one destructive opt-in skip) at 83%
coverage and 520 Windows tests (nine platform/opt-in skips) at 82% coverage. The privileged
Docker matrix passed on the v1.3.3 image: historical v1.0.2, v1.2.1 standalone bootstrap, v1.3.0
all-running with an active Local API log, backup/offline-doctor/online-doctor rollbacks,
local-api/bot stopped, mixed services, the v1.3.1 standalone bootstrap, and the new delayed Local
API readiness regression (bot and local-api RestartCount both 0; permanently unavailable endpoint
fails after the bounded wait). The live authenticated Instagram Story (video, 6,355,416 bytes)
downloaded through gallery-dl without a false `too_large`, and the plain-profile avatar action
downloaded the original JPEG. Exact command results, artifact hashes, platform skips, and runtime
smokes are recorded in `docs/HANDOFF_REPORT.md`.

## Recent fixes

- 2026-08-31: Added user-visible durable source links to every successful media-delivery path while
  preserving all existing caption, attribution, Story/Highlight, collection ordinal, and multipart
  text. Album items remain individually traceable, tracking parameters stay stripped by the existing
  canonicalizer, and caption-limit fallback preserves receipt-first `delivery_uncertain` behavior.

- 2026-08-17: Added project-scoped Graphify exclusions and query/freshness guidance, compact agent
  routing/current-state/ADR indexes, six validated subsystem navigation Skills, and dependency-free
  `agent_context.py` symbol/import/reference/test discovery with deterministic tests. Root
  `AGENTS.md` now avoids unrelated document preloading but retains every global engineering rule;
  source, tests, and detailed docs remain authoritative, and CI requires the local context guard.

- 2026-08-16: Prepared patch 1.3.3. The bot no longer crash-restarts when the compose `local-api`
  service is still starting: bot and worker now wait with bounded exponential backoff
  (`local_api_startup_wait` / `local_api_startup_ready` / `local_api_startup_timeout` structured
  events) and still fail non-zero if the endpoint never becomes reachable. Instagram gained
  first-class Story support (exact-media-id canonicalization, gallery-dl as primary engine,
  image photo/file delivery, video `video_original` native MP4 delivery), profile-avatar
  downloading (plain profile URLs become the `/USERNAME/avatar/` action with document/file
  delivery), and an explicit URL routing contract. The production false `too_large` for the
  silent video-only story was fixed: gallery-dl inspections now accept video-only collections,
  and the bounded format selector classifies "no complete selection exists" as format-unavailable
  instead of oversized. Job failures now retain provider source attribution after an adapter has
  processed the request.
- 2026-08-15: Fixed the v1.3.2 privileged-updater fixture so the v1.3.0/v1.3.1 production config it
  generates (local_bot_api enabled in external mode) owns its required Local Bot API persistent
  directory `/data/state` as the configured runtime user before the updater runs. Candidate
  configuration preflight validates that existing directory read-only, so a valid production
  installation must already satisfy the contract; the fixture now asserts it before invoking the
  updater, failing with a precise fixture message instead of inside preflight. The remaining legacy
  root-owned paths still exercise the updater's post-install permission repair.
- 2026-08-15: Prepared patch 1.3.2 after production v1.3.1 proved its nominal offline doctor still
  required `local_api_reachable` and `required_channels` while those services were intentionally
  stopped. The updater now runs fail-closed static checks offline, conditionally verifies restored
  Local API/Telegram endpoints online, preserves mixed service states, rolls back at either failure
  boundary, and exits cleanly on preflight SIGINT. The v1.3.1 atomic backup fix remains unchanged;
  v1.3.1 requires the checksummed standalone v1.3.2 updater once.
- 2026-08-15: Prepared patch 1.3.1 for the production Local Bot API log race in the v1.3.0 Linux
  updater. Candidate assets/images now finish before downtime; running bot/worker/Local API writers
  stop before tar; Redis remains online; and the archive atomically includes config, `.env`,
  cookies, SQLite/WAL/SHM, and durable Local API state while excluding only the exact volatile log.
  Backup and offline-doctor failures restore the prior transaction and exact service state with
  bounded secret-redacted diagnostics. The release keeps the checksummed standalone updater asset
  as the required one-time v1.3.0 bootstrap.
- 2026-08-14: Prepared v1.3.0 with secure administrator cookie management over the existing canonical
  `yt_dlp.cookies_file`: domain-based multi-service detection, deterministic domain/path/name
  deduplication, exact unrelated-line preservation, restricted atomic backups/replacement,
  private-chat full export, bounded in-memory Telegram uploads, secret-free failure logging, and
  one fail-fast effective cookie path shared by every real yt-dlp/gallery-dl consumer.

- 2026-08-14: Corrected the v1.2.2 privileged-updater fixture to use the configured Compose runtime
  UID/GID for its final write/SQLite probe. Real WSL2/Docker diagnostics proved both historical
  paths migrate legacy `root:root` `0500`/`0400` state to runtime-owned `0700`/`0600`; the former
  hard-coded `10001:10001` probe alone was inaccurate. The test now asserts every durable/runtime
  path plus the resolved service user and read-only-config/writable-data bind contract. Each run
  uses a unique Compose project and removes its own temporary volume. Elevated image-pin rewrites
  preserve `.env` ownership/mode, and ShellCheck SC2251 remains resolved without suppression.
- 2026-08-14: Prepared patch 1.2.2 so Linux update preflight pulls the prepared image and mounts
  `config.yaml` plus project `/data` read-only. Runtime-valid default Instagram cookie fallback and
  explicit gallery cookie paths now validate without configuration edits; missing or unreadable
  cookies still fail before service stop. Config/cookie bytes, rollback, backup, and project-scoped
  post-verification image cleanup remain unchanged. A standalone updater plus SHA-256 asset bridges
  the v1.2.1 installed script, which necessarily executes its old preflight before file replacement.
- 2026-08-13: Prepared patch 1.2.1 for production mixed carousel `DZUwLh3jEDk`.
  yt-dlp now classifies raw parent entries without processing photo children, requires all 17
  gallery slots and the exact video ordinal 11, and downloads only child `DZUtxnNDJg7` through the
  normal strict yt-dlp path. Plan mismatch fails before gallery image download; `ignoreerrors`
  remains disabled and signed media URLs remain transient.
- 2026-08-13: Prepared v1.2.0 with an owner-bound Instagram Photo/File confirmation, nullable
  backward-compatible durable/ARQ state, exact-byte image document delivery, deterministic
  ten-item chunking, and mixed-post gallery-image/yt-dlp-video reconciliation. Video-only Reels,
  Twitter HLS remux, other gallery sources, cleanup, cancellation, and signed-URL isolation remain
  unchanged.
- 2026-08-13: Added redacted push alerts to all configured administrators for terminal inspection
  and download failures plus delivery uncertainty, and reverified removal of download/temp job
  workspaces across success, failure, timeout, cancellation, and uncertain delivery.
- 2026-08-12: Prepared v1.1.1 by explicitly enabling gallery-dl JSON Lines output, strictly parsing
  directory/URL message tuples, and recognizing `ytdl:` video events as video-only fallback input.
  Instagram Reels containing no images now reach yt-dlp, while image and mixed-post ownership,
  transient URL validation, subprocess isolation, and durable URL-free asset models are preserved.
- 2026-08-11: Fixed the v1.1.0 Linux quality-job failure by replacing a Windows-only-consumed
  `os.killpg` type suppression with an explicit platform guard. Gallery subprocess cancellation
  keeps the same Windows CTRL_BREAK and POSIX process-group SIGTERM behavior, while strict mypy now
  passes on both Windows and Linux platform models. The locked aiohttp patch was also advanced from
  3.14.2 to 3.14.3 after the release audit identified PYSEC-2026-3545.
- 2026-08-01: Implemented T013 gallery-dl 1.32.8 media bundles, source-isolated cookies, bounded
  subprocess cancellation, image validation, stable selection persistence, and typed album/ZIP
  delivery with offline fixtures and package/Docker license smokes.

- 2026-08-01: Separated the administrator reply keyboard from editable inspection status messages,
  added Telegram edit-to-send fallback for YouTube selections and Instagram auto-download, and
  reconciled deduplicated active jobs with ARQ without creating an orphan status message.
- 2026-07-30: Prepared 1.0.10 with bundled Noto Sans/OFL assets, a complete in-memory Pillow
  reporting dashboard, actionable doctor diagnostics, structural visual regression tests, and
  offline read-only UID-10001 Docker chart artifacts.
- 2026-07-30: Prepared 1.0.9 with a persistent administrator reply keyboard, shared URL entry,
  per-admin single-flight PNG/text usage reports, Tehran-local breakdowns, forged-action defenses,
  and public KPI aggregation that excludes administrators without deleting durable activity.
- 2026-07-29: Prepared 1.0.8 with job-scoped zero-retention, per-part multipart cleanup, cancellable
  isolated 7-Zip processes, observable orphan sweeping, and verified project-only Docker image
  cleanup with a dry-run operator command.
- 2026-07-29: Prepared 1.0.7 with centralized YouTube single-video canonicalization, durable and
  queue-safe canonical URLs, execution-boundary recovery normalization, and `noplaylist` defenses
  that prevent Mix expansion without changing real-playlist or Native format behavior.
- 2026-07-29: Prepared 1.0.6 with selectable AV1/AAC MP4 plans, codec-aware labels and
  deduplication, durable codec-family constraints, selectable Best Original summaries, document
  delivery for non-inline AV1, and packaged zero-encode/stream-copy runtime assertions.
- 2026-07-29: Prepared 1.0.5 with native-only public video choices, actual selected-stream labels
  and sizes, fallback/Best Original deduplication, deterministic Back navigation, dual pre-enqueue
  transcode validation, structured option-catalog logging, and safe legacy callback redirects.
- 2026-07-28: Prepared 1.0.4 with codec-first native H.264/AAC MP4 ranking, deterministic
  lower-resolution/fail policy, native VP9/Opus WebM preservation, opt-in explicit conversion,
  pre-spawn timeout estimation, structured selection reasons, the production metadata fixture, and
  an identical packaged native-selector/remux smoke in both CI and published-image verification.
- 2026-07-27: Prepared hotfix 1.0.3 with updater self-replacement protection, mandatory archive
  syntax/mode validation, runtime SQLite WAL probes, transactional application/image/permission
  rollback, bounded restarts, health verification, and filesystem/privileged upgrade coverage.
- 2026-07-27: Made cancellation durable across SQLite/ARQ and restart-safe; bounded FFmpeg CPU,
  concurrency, timeout, progress, and process cleanup; added automatic permission and `tmb` repair;
  and guaranteed executable multipart 7-Zip tooling in image workflows.
- 2026-07-27: Replaced the fresh-runner Compose image build with Buildx/build-push-action, shared
  the `telegram-media-downloader-bot-amd64` GHA cache between CI and releases, retained Compose
  validation, and added a loaded-image CLI smoke test plus static cache-isolation coverage.
- 2026-07-27: Fixed Instagram `best_original` inflation by making native-only an enforced domain
  invariant, aligning durable/queued auto-download policy, preserving native MP4+M4A or unconstrained
  source containers according to `force_mp4`, routing non-streamable VP9 MP4 through document
  delivery, and changing transcoding to CRF-first with size limiting only as a fallback.
- 2026-07-26: Restricted production container publication and GitHub Releases to matching `v*`
  tag pushes, added stable/prerelease-aware GHCR tags, post-push image smoke testing, and verified
  reproducible source assets. Manual branch dispatches now publish nothing.
- 2026-07-26: Made managed Local Bot API process flags type-safe on non-Windows hosts and fixed
  the Windows CI analyzer to pass each management script as a scalar PSScriptAnalyzer path.
- 2026-07-26: Pinned the official Telegram Bot API parent repository to the verified full commit
  `adfd7f6a8e990272851777eeb3ae0def4216f161`, checks it out before synchronizing submodules, and
  added static plus real Compose-build CI regression gates.
- 2026-07-25: Made successful delivery completion and permanent byte accounting one atomic SQLite
  transaction; persistence uncertainty is quarantined without automatic Telegram retry. Added WAL
  contention coverage and backward-compatible legacy job idempotency keys.
- 2026-07-25: Added required-channel membership, yt-dlp-only proxy control, MP4/WebM container
  selection, Instagram Story/Highlight/multi-video policy, dynamic bot captions, durable user
  usage, and cross-platform Docker installation/management.
- 2026-07-24: Inspection now evaluates each enabled semantic selector against the real formats,
  hides unavailable exact heights, and displays resolution/FPS/HDR plus exact, estimated, or
  unknown video+audio size. Direct and multipart uploads report tracked byte progress in Telegram
  and structured logs, followed by an honest elapsed-time Telegram-processing heartbeat. Per-volume
  receipts are persisted immediately; upload chunks default to 1024 KiB and per-part timeout to
  14400 seconds.
- 2026-07-24: Simplified large-file delivery: every result above the configured direct Local Bot
  API ceiling now becomes stored 1850 MB ZIP volumes through 4096 MB. Removed the Premium account,
  Telethon dependency, MTProto session/CLI, staging channel, uploader queue/process, and `copyMessage`
  route.
- 2026-07-24: Added `1440p`, `2160p`, and non-transcoding `best_original` modes. Delivery now routes
  up to 1900 MB through Local Bot API and every larger result through 4096 MB as stored 1850 MB ZIP
  volumes with SHA-256 manifests.
- 2026-07-24: Local API CLI actions now accept `--config` both before and after the action name, so
  `telegram-media-bot local-api status --config ./config.yaml` and the original ordering are both
  valid.
- 2026-07-24: Added production Local Bot API lifecycle and migration. Bot/Worker now share one
  config-derived endpoint, mixed cloud/local clients are rejected, managed credentials never enter
  process command lines, normal startup never calls `logOut`, and files under the configured local
  upload ceiling are delivered without forced transcoding.
- 2026-07-24: Generic inspection size estimates are now advisory because upstream may report the
  best/default format before semantic selection. The selected download and final post-processed file
  remain strictly bounded by `media.max_file_size_mb`.
- 2026-07-24: Replaced yt-dlp's per-stream `max_filesize` behavior with complete-format selection and
  bounded FFmpeg transcoding. Explicit video modes now preserve both audio and their distinct target
  resolution instead of collapsing every oversized choice to the same lower native stream.
- 2026-07-24: Telegram file uploads now use the dedicated configurable
  `telegram.upload_timeout_seconds` (14400 seconds by default), preventing large uploads from being
  cut off by aiogram's 60-second general session timeout while preserving uncertain-delivery
  quarantine for genuinely ambiguous transport failures.

## Known limitations

- The supported v1 topology is one worker container with bounded internal concurrency. Multi-host
  worker replicas need a leased/shared durable database adapter; SQLite is not presented as that.
- Telegram provides no upload idempotency key. Ambiguous delivery is quarantined for operator review
  instead of automatically retried.
- DNS and extracted URLs are revalidated, but no application can eliminate DNS rebinding between a
  validation lookup and an upstream library's socket connect without controlling that library's
  resolver/transport.
- The Docker image builds the official Local Bot API executable from pinned upstream source. The
  destructive real >200 MB upload test still requires an explicitly configured local bot/chat and
  remains skipped in the default suite.
- Instagram Stories/Highlights that require authentication depend on a current operator-supplied
  restricted canonical cookies file; upstream login challenges can still invalidate it.
- Castbox and Spotify are not implemented; both remain outside the generic v1 engine policy.
- Multi-volume output requires 7-Zip on the server and on the recipient device. Real >2 GB and
  >3.9 GB tests are destructive opt-in tests and remain skipped in the default suite.
