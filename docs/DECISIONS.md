# Architecture decision record

## ADR-001: Python 3.14 baseline with no artificial upper bound

**Status:** accepted

Use Python 3.14, the latest stable CPython generation selected for this project. The project requires
`>=3.14` and intentionally has no artificial upper bound, so a newer installed stable Python can be
adopted after the quality gates pass. Docker defaults to Python 3.14 through the configurable
`PYTHON_VERSION` build argument. Preview and beta interpreters are not production defaults.

## ADR-002: Embed yt-dlp through one adapter

**Status:** accepted

Use the Python embedding API, but permit imports only in `infrastructure/ytdlp`. Upstream metadata is
sanitized and mapped immediately to project models.

## ADR-003: Bot and worker are separate processes

**Status:** accepted

Use aiogram for polling and ARQ/Redis for asynchronous jobs. Downloads must not block the Telegram
polling process.

## ADR-004: YAML is the local operator configuration

**Status:** accepted

Secrets and runtime settings are stored in ignored `config.yaml`. Pydantic validates the file with
unknown keys forbidden. Environment variables may only select the config path.

## ADR-005: uv lockfile controls updates

**Status:** accepted

Do not self-update yt-dlp at startup. Update the uv lock entry, run adapter and project tests, then
rebuild. Roll back by reverting the update commit.

## ADR-006: Delivery port with worker-owned upload

**Status:** accepted; supersedes the starter wording

The worker owns delivery but depends on a project `DeliveryGateway` port. The Telegram adapter
selects audio/video/document and supports an operator-selected local Bot API endpoint without
coupling application or download-engine contracts to aiogram.

## ADR-007: SQLite/WAL is the durable local job store

**Status:** accepted

Use one SQLite database below `storage.state_directory` for jobs, selections, cancellation, dynamic
blocks, and delivery receipts. WAL plus short transactions supports the bot and one worker container
concurrently. Redis remains the ARQ/rate-limit backend. Multi-host worker replicas require a future
leased database adapter before enablement.

## ADR-008: Quarantine uncertain Telegram deliveries

**Status:** accepted

Persist `delivering` before calling Telegram and persist returned file IDs immediately after. If a
process exits in that gap or Telegram returns an ambiguous transport failure, transition to
`delivery_uncertain`, include it in idempotency matching, and require operator review. Never retry it
automatically.

## ADR-009: Deno is the pinned yt-dlp JavaScript runtime

**Status:** accepted

Pin Deno 2.9.3 from the official binary image. yt-dlp recommends Deno and the locked
`yt-dlp[default]` dependency supplies `yt-dlp-ejs`. Runtime upgrades follow the reviewed
lock/image/canary process and `doctor` reports the executable version.

## ADR-010: Bounded playlists are delivered as one ZIP document

**Status:** accepted

The stable engine contract returns one final file. When playlist policy is enabled and multiple
files are produced, the adapter verifies aggregate size, creates a ZIP below the job directory,
deletes the individual files, and returns it as `MediaKind.PLAYLIST`.

## ADR-011: Preserve semantic video resolution with bounded transcoding

**Status:** accepted

For video modes, select the best complete SDR source at or below the semantic target and bound that
transfer with `media.max_source_size_mb`. If the merged result exceeds the final media
ceiling, transcode it to H.264/AAC at the selected resolution and a calculated bounded bitrate.
This avoids deceptively collapsing `1080p`, `720p`, and `480p` to one low native stream while
remaining compatible with the official Telegram Bot API upload limit.

## ADR-012: Durable explicit Local Bot API migration

**Status:** accepted

Support the official server in managed and external modes behind one project-owned Telegram runtime.
Secrets and all lifecycle settings remain in ignored YAML. Public/local migration is an explicit
operator command with confirmation and a durable write-before-call state machine, so `logOut` is
never repeated after an uncertain response. Normal startup selects the durable active endpoint and
never migrates. Cross-process endpoint leases prevent simultaneous cloud/local clients and provide
reference-counted managed shutdown. The application ceiling is 1900 MB, below Telegram's documented
2000 MB local maximum. Bare-metal mode accepts an operator-supplied binary; the Docker topology
builds the same official server from pinned upstream source.

## ADR-013: Multipart delivery for every oversized result

**Status:** accepted

Every result above the configured direct Bot API ceiling is packaged with 7-Zip into stored
multi-volume ZIP files accompanied by SHA-256 metadata. This preserves source bytes, keeps each
upload below the Local Bot API ceiling, and removes the MTProto user session, private staging
channel, Premium queue, copy step, and their recovery states. `best_original` remains
non-transcoding. Delivery receipts remain per-volume so successfully returned Telegram message
identifiers are durable.

## ADR-014: Container contracts, Instagram automation, and durable user usage

**Status:** accepted

Ordinary videos use a two-stage semantic Container then quality selection. MP4 and WebM are
project-owned contracts: native files are preferred and an incompatible candidate is converted to
H.264/AAC or VP9/Opus. `best_original` remains native-only. Instagram video posts, Reels, Stories,
Highlights, and multi-video collections are normalized by the same yt-dlp adapter, skip
presentation choices, ignore image entries, and deliver ordered best-MP4 artifacts separately.

Required-channel membership is a fail-closed application policy backed by Telegram and positive/
negative Redis cache. User profiles and usage totals are permanent SQLite/WAL records; a unique
event per job makes accounting idempotent. The optional secret proxy is scoped solely to yt-dlp.

## ADR-015: Docker-first installation and official Local API service

**Status:** accepted

Production installation is Docker-first on Linux and Windows. The official Local API executable is
compiled from pinned upstream source and can be owned by a dedicated Compose service. Bot and Worker
share the YAML-derived endpoint; API credentials enter only the official child environment.
Cross-platform `tmb` commands provide lifecycle, logs, doctor, configuration, checksummed release
updates with state-aware recovery, backup, and explicit uninstall.

## ADR-016: Original-quality downloads are native-only and size ceilings are not bitrate targets

**Status:** accepted

`best_original` is an invariant rather than a presentation hint: any accidental
`ContainerPolicy.GUARANTEED` request is normalized to `NATIVE_ONLY` in the project-owned domain
contract. Instagram automation uses that invariant in both durable job creation and queue payloads.
When `media.instagram.force_mp4` is enabled, yt-dlp selects the best native MP4 video plus M4A audio
and may merge/remux only; when disabled, no output container is imposed.

Native mux compatibility, Telegram H.264/AAC inline-video streamability, and unrestricted document
delivery are modeled as distinct decisions. A VP9 stream in MP4 is therefore retained for document
delivery. Genuine codec conversion uses a quality-oriented CRF pass first. The file-size setting is
a ceiling; bitrate calculation is deferred to a fallback only when the quality pass exceeds that
ceiling or an explicit anti-inflation bound.

## ADR-017: Durable-first cancellation and bounded codec conversion

**Status:** accepted

User cancellation is committed atomically in SQLite before transient ARQ coordination. The ARQ
worker enables official job abort, while the queue adapter finalizes known enqueue, retry,
in-progress, abort, and result state only after a job is no longer running. Startup reconciliation
is authoritative: every queued/running/retrying job with `cancel_requested=1` becomes terminal
`cancelled` and is never re-enqueued. Healthy abandoned work retains the existing recovery policy,
and interrupted delivery remains quarantined.

Codec conversion remains available for guaranteed non-original modes but is an explicitly bounded
resource. FFmpeg receives an encoder thread count, one worker-local gate controls concurrent
encodes, a timeout and cancellation terminate the process group, and operators may disable heavy
conversion. Defaults are present in the strict model so older configuration files remain valid.
Compose exposes an optional worker CPU quota without imposing one on every installation.

## ADR-018: Transactional Linux release updates and runtime permission probes

**Status:** accepted

Linux updates execute from a temporary copy so replacing `scripts/tmb.sh` cannot truncate the
script Bash is reading. Release archives carry the command through an executable symlink target to
bootstrap safely from the published v1.0.2 updater. Linux release archives omit tracked `data/`
placeholders so the old updater's final `cp -a` cannot reset freshly migrated state ownership.
Complete staged Bash scripts, Compose, and the existing configuration are validated before
application writers stop. Prepared-image configuration validation receives the same project data
path contract as runtime, but both the container root and persistent `/data` bind are read-only.
The configured runtime UID/GID is retained and cookie readability remains mandatory. Non-mutating
directory accessibility replaces write probes only in this preflight mode; real runtime-user
writes and SQLite WAL are still required after service stop and before candidate startup. Because
v1.2.1 runs this step from its already-installed script, v1.2.2 also publishes a separately
checksummed updater bootstrap. It is executed once without replacing configuration or persistent
state, then the transactional update installs the fixed command for subsequent releases.

The same bootstrapping rule applies to the v1.3.0 backup-order defect: updater code is itself part of
the installed release, so v1.3.0 cannot acquire the corrected stop-before-backup transaction through
its own ordinary update execution. The v1.3.1 standalone updater asset is therefore the required
one-time path from v1.3.0.

The v1.3.1 runner likewise cannot acquire v1.3.2's corrected verification selection before it runs
its own post-install doctor. The checksummed v1.3.2 standalone updater is therefore the required
one-time Linux path from v1.3.1; it changes updater control flow only, not persistent configuration
or state.

After all network/preflight work, the updater records the exact four-service state, stops only the
running bot/worker/Local API filesystem writers, and leaves Redis online. It then creates a private,
atomic archive of configuration, cookies, SQLite/WAL/SHM, and durable Local API state. Downloads and
temp remain in place but outside the archive; the exact volatile Local API log path is excluded.
Top-level application entries are replaced with a rollback snapshot while `.env`, `config.yaml`,
data, backups, cookies, downloads, Redis, and Local API state remain outside replacement. Before
candidate writers start, the updater resolves the Compose runtime UID/GID, normalizes private paths,
performs a real runtime-user write, requires SQLite WAL, and runs a fail-closed offline doctor over
package/dependency/cookie/static runtime state. Offline mode is explicit and cannot perform Local
API, Telegram, required-channel, or worker-processing reachability. Candidate services must then
reach running/healthy state; selected online checks apply only to originally running Local API and
bot services, followed by an exact original-running-set assertion. The default operator doctor
retains its full live behavior. Any
failure after the stop phase restores the prior application/image/permissions/command when changed
and always restores the original service state. Every production service uses the `unless-stopped`
restart policy so the stack survives Docker daemon restarts and host reboots; the update
transaction still stops writers explicitly (an intentional stop the policy never undoes) and
treats a post-start crash/restart state as a rollback trigger.

## ADR-019: Ordinary MP4 is a native codec contract

**Status:** accepted

The ordinary MP4 interaction means fast native H.264/AVC video plus AAC audio, not merely an MP4
file extension. The yt-dlp adapter filters codecs before resolution, FPS, bitrate, and upstream
quality ordering. Its default deterministic fallback selects the highest lower compatible
resolution and exposes that actual height; operators may instead fail when the exact height is
absent. WebM remains native VP9 + Opus and `best_original` remains unconstrained native-only.

Heavy AV1/VP9-to-H.264 conversion is represented by `ContainerPolicy.EXPLICIT_TRANSCODE`, exposed
only when the new default-off operator switch is enabled. A conservative estimate must fit inside
the transcode stage timeout before FFmpeg can acquire the encode gate or spawn. Existing callback
payloads continue to resolve to the ordinary native policy.

## ADR-020: Public video choices are actual native plans

**Status:** superseded by ADR-021

The Telegram UI exposes only native H.264/AAC MP4 and native VP9/Opus WebM video. Explicit
transcoding remains an internal bounded capability and is never a public button, even when its
operator switch is enabled. Public video jobs use the codec-filtered, reject-on-mismatch
`GUARANTEED` contract; old generic/converted callbacks are non-operational redirects.

The adapter maps selected stream IDs, codecs, geometry, FPS, dynamic range, component sizes, and
quality score into immutable project models. The application service validates native codec
contracts, removes every transcode-required plan, deduplicates by actual selected-stream identity,
and chooses an exact-mode representative over a fallback. Telegram receives only a short opaque
digest, never raw format IDs.

Back navigation is a deterministic presentation transition over the existing owner-bound selection:
quality returns to output type, output type returns to the send-link prompt, and neither transition
calls the engine or creates/enqueues a job.

## ADR-021: Native MP4 is a zero-transcode AV1/H.264 contract

**Status:** accepted; supersedes the codec restriction in ADR-020

Native describes processing, not playback compatibility. Public MP4 plans may contain AV1/AAC or
H.264/AAC when both streams can be merged/remuxed with stream copy and neither stream requires
encoding. Codec families are planned, labeled, deduplicated, persisted, queued, and revalidated
independently so a user's AV1 choice cannot silently become H.264 after enqueue or restart.

Telegram inline-video streamability remains a separate H.264/AAC decision. A native AV1 MP4 that is
not inline-streamable is delivered as a document without invoking an encoder. The Best Original
summary is derived from a visible native plan, so every advertised resolution/container/codec/size
has a corresponding selectable opaque option.

## ADR-022: A YouTube video ID takes precedence over playlist context

**Status:** accepted

For supported YouTube `watch`, `youtu.be`, `shorts`, and `live` URL shapes, a validated video ID is
authoritative single-video intent even when `list`, Mix, index, or sharing parameters are present.
The application canonicalizes such URLs before persistence and enqueue, while worker and adapter
boundaries repeat the normalization for legacy and recovered jobs. Explicit `/playlist?list=...`
URLs continue to use the existing bounded-playlist policy.

Canonicalization and yt-dlp `noplaylist=true` are independent defenses. Both inspection and
download use the canonical URL, preventing an inspection/download mismatch and avoiding needless
Mix expansion or Deno CPU work. Audit logs retain only allowlisted URL fields so credential-like
query parameters are neither stored in the event nor exposed.

## ADR-023: Terminal media is zero-retention and update cleanup is project-scoped

**Status:** accepted

Every terminal job outcome owns an idempotent cleanup of its exact download and temporary
directories. Confirmed delivery receipts are the deletion boundary: multipart volumes and
multi-artifact files are removed immediately after their receipt is durable, while final worker
cleanup removes the source, sidecars, partial downloads, manifests, and packaging residue. Deletion
does not follow symlinks, cannot address a storage root or sibling job, and never masks the primary
job result. Startup and maintenance repeat cleanup for terminal jobs and age-gated unknown
workspaces, but preserve active and retryable jobs.

The release updater may reclaim Docker resources only after health, exact version, doctor, and
service-status verification. Its allowlist is the exact project GHCR repository plus superseded
stopped containers from the current Compose project. Image IDs referenced by any container, the
running image, other repositories, volumes, and BuildKit cache are protected. A cleanup failure
does not roll back an otherwise healthy release; it is visible to the operator and safely
repeatable through `tmb cleanup [--dry-run]`.

## ADR-024: Administrator UX is role-aware presentation over shared use cases

**Status:** accepted

Administrator capability discovery uses a persistent Telegram reply keyboard shown from `/start`,
`/menu`, or the backward-compatible `/panel`. Visibility is not authorization: every management
message and callback checks the sender against the currently loaded `telegram.admin_ids`. Forged
button text fails closed, and FSM state is cleared when role authorization fails.

The administrator download prompt receives the same URL-submission callable as the ordinary URL
router. It does not introduce an admin download service, job kind, worker, queue payload, delivery
path, format contract, or workspace. The ordinary access policy—including the existing
administrator membership bypass—remains authoritative.

Usage reporting reads project-owned activity through an application port. Public KPI aggregation
filters configured administrator IDs in memory but retains durable jobs/events for audit and
idempotency. Tehran-local weekly/monthly windows and the complete report's 14-day detail are
rendered under a per-administrator single-flight guard. PNG bytes exist only in memory and reports
never contain administrator IDs, URLs, filenames, SQL, or internal exceptions.
# ADR-025: Bundle chart typography and render usage dashboards with Pillow

- **Status:** accepted
- **Date:** 2026-07-30

The v1.0.9 raw RGB encoder could draw only primitives, so its apparent legend contained colored
swatches but no title, labels, axes text, dates, or KPI descriptions. Usage charts now use Pillow
and load the project-bundled Noto Sans Regular bytes through `importlib.resources`. The SIL OFL 1.1
license is shipped beside the font in wheel, sdist, release archives, and Docker images.

Missing, empty, undecodable, or glyph-incomplete resources raise `UsageChartFontError`; silently
falling back to Pillow's bitmap font is forbidden. English ASCII chart text deliberately avoids an
implicit Arabic shaping/BiDi dependency, while Telegram captions remain Persian. Rendering stays
in memory and preserves zero-retention. CI validates meaningful text regions and publishes weekly
and monthly fixture charts produced as UID 10001 without network, DISPLAY, or a writable image.
## ADR-026: Infer only Twitter's explicit HLS audio rendition contract

- **Status:** accepted
- **Date:** 2026-08-01

Twitter/X currently reports its AAC HLS audio rendition with `acodec=null`, while retaining an
audio-only resolution, `video_ext=none`, MP4 audio extension, `m3u8` protocol, and a stable
`hls-audio-*-Audio` identifier. The adapter may infer AAC only when all of those signals agree.
This keeps H.264/AAC MP4 eligible for stream-copy remux without broadly treating unknown MP4 audio
as compatible. The exact inspected stream IDs are persisted and reused instead of replanning at
download time. Empty native plans use a distinct safe error category from unavailable source media.

## ADR-027: Isolate gallery-dl behind an expiring-URL-free subprocess contract

**Status:** accepted

Gallery-dl 1.32.8 runs as a separately installed external program through an argv-only asyncio
subprocess with ignored user configuration, bounded stdout/stderr/runtime/concurrency, and
process-group cancellation. No application code imports `gallery_dl`, and no vendor dictionary,
stderr, exit code, command, extractor label, or signed CDN URL crosses the adapter boundary. This
keeps the GPL-2.0 runtime dependency replaceable and preserves the application's layering without
making a legal conclusion about combinations.

Routing depends on normalized inspection: one or more images makes gallery-dl own the complete
ordered social post; an explicit no-image result falls back to yt-dlp. Unsupported bulk social URLs
fail closed. Stable asset IDs derive from provider, post identity, order, kind, and extension—not a
temporary URL—and downloads re-inspect from the canonical post URL. Original images are signature
validated and delivered as photo/media group when compatible, otherwise as documents; ZIP and
oversize delivery reuse the existing archive/multipart boundaries.

## ADR-028: Administrators merge cookies through one atomic canonical-file adapter

**Status:** accepted

Cookie administration is exposed only in the bot's private role-aware panel and is injected through
an application port. The infrastructure adapter manages the already configured
`yt_dlp.cookies_file`; no new cookie path, database schema, worker message, or container mount is
introduced. yt-dlp and all gallery-dl providers use one resolved effective path. Existing
`gallery_dl.cookies` keys remain readable only as compatibility aliases: when non-null, every alias
and `yt_dlp.cookies_file` must resolve to the same file or configuration fails before startup. A
single legacy gallery alias is promoted to the effective path for yt-dlp and all gallery providers.

Uploads are bounded in memory and parsed as strict seven-field Netscape records. Filename and MIME
metadata are ignored. Supported service ownership is derived from normalized domain suffixes, and
the merge key is case-folded domain plus exact path and name. The last uploaded duplicate wins;
matching existing records are replaced, new keys are appended in first-seen upload order, duplicate
records for detected services collapse deterministically, and every unrelated raw line remains
unchanged.

Before modification, the adapter creates a private same-filesystem hard-link backup. It then writes
and fsyncs a same-directory temporary file, preserves the canonical file's numeric owner/group/mode,
and atomically replaces the path. Current readers retain their open inode and subsequent jobs open
the new file. Error reporting contains only stable Persian messages and exception class names—never
cookie values, names, domains, contents, upload filenames, or filesystem paths.

## ADR-029: Add progressive agent navigation without changing engineering authority

**Status:** accepted

Keep root `AGENTS.md` as the always-loaded global engineering contract, but stop requiring every
ordinary task to preload all major project documents and historical task files. Route tasks through
compact indexes and repository-local subsystem Skills, then use bounded Graphify queries to locate
the smallest likely source/test working set. Architecture-wide or high-risk changes still require
the applicable complete specification, architecture, ADR, status, and task context.

Graphify is optional local developer tooling backed by `.graphifyignore` and ignored
checkout-specific output. It is not a production dependency, source of behavioral truth, hosted CI
service, or substitute for source/tests/ADRs. Source defines behavior, tests define expected
behavior, and detailed documentation retains rationale. A dependency-free AST utility and CI
guardrail provide deterministic fallback/navigation verification without requiring Graphify.

## ADR-030: Cookie health is passive and local

**Status:** accepted

Repeated authenticated health probes can trigger provider anti-automation and account-security
systems. Cookie Health therefore has no provider client, probe URL, scheduled watcher, startup
network validation, or administrator live-check action. Automatic/admin/upload refreshes inspect
only the canonical Netscape file: readability, format, provider records/counts, expiry/session
timestamps, and filesystem permissions. Session-only records are `UNVERIFIED`, not `MISSING`, and
local `HEALTHY` describes structural/expiry state rather than authenticated provider acceptance.

An authentication failure returned by a real user-requested extraction is valid passive evidence
and may persist `AUTH_FAILED`; Cookie Health must not issue a second request to confirm it. A fresh
provider upload clears that provider's prior runtime failure and immediately persists a local
snapshot. Historical SQLite active fields remain readable to avoid a migration, but old network
probe success is discarded at the next static refresh. Historical probe configuration keys are
accepted and ignored so strict configuration compatibility is preserved while generated examples
and schema expose no live-probe controls.

## ADR-031: Withdrawn releases fail closed in standalone bootstrap policy

**Status:** accepted

Release v1.3.7 is a known-broken production artifact and must remain in immutable Git history while
being unavailable as an install, update, rollback, or publication target. A repository-owned
`release-policy.json` denylist is authoritative for build/publication tools. Linux and Windows
installers/updaters embed the same small list because one-line installers and checksummed updater
assets must work without another runtime file or revocation endpoint; deterministic tests require
all four snapshots to match the canonical policy.

Both the requested tag and the independently extracted package version are checked. Requested-tag
rejection happens before download. Candidate-version rejection happens after checksum/extraction
but before application/configuration/persistent-state mutation, image pull, backup, or service
stop, preventing aliases from bypassing the withdrawal. The installed version is deliberately not
blocked: an affected v1.3.7 deployment must retain its forward path to v1.3.8 or later. The updater
does not silently substitute a release, and healthy older rollback targets remain available. A
remote revocation service is deferred because the single embedded withdrawal does not justify a
new availability and trust dependency in disaster-recovery tooling.

## ADR-032: Durable VIP entitlement is a typed, ledger-based bot capability

**Status:** accepted for the T014 entitlement foundation (plans, grants, subscription projection,
UTC calendar arithmetic, reversal recomputation, and job snapshots) and for the T015 provider-neutral
billing/payment-confirmation decisions implemented below. Future real-gateway integration (T024),
the secure companion web/callback boundary (T016), and VIP Telegram UX (T023) remain **proposed**.

The product calls the feature VIP, but domain/application code uses typed subscription,
entitlement, capability, plan, and grant models. Telegram's `UserProfile.is_premium` is unrelated
and cannot authorize bot VIP behavior; it is never read by the entitlement service.

Subscription plans accept any positive number of calendar months and snapshot integer minor-unit
price, currency, enabled state, and capabilities. Month arithmetic is UTC with end-of-month
clamping. Non-reversed grants are evaluated in payment-confirmation order:

```text
start = max(grant.confirmed_at, preceding_expiry)
expiry = add_calendar_months(start, grant.duration_months)
```

Renewal therefore preserves paid time. A refund/reversal marks the payment's immutable grant
reversed and recomputes the chain from the remaining grants; if no valid paid time remains, access
ends immediately. Economic rows are retained indefinitely and never enter media cleanup.

A protected request authorized and durably accepted while its capability is active stores a safe
`EntitlementSnapshot` on its job and may finish after subscription expiry. Automatically created
child jobs inherit that snapshot. A later user callback or new URL is a new protected request and
must reauthorize. Credential revocation (T017+) remains a separate authority and can still stop an
accepted credential-dependent job.

**Implemented (T015): payment confirmation does not grant a second extension.**

A provider adapter cryptographically verifies its callback before returning a project-owned result.
One SQLite `BEGIN IMMEDIATE` transaction validates order/amount/currency/status, claims the unique
`(provider, provider_transaction_reference)` via the `provider_transaction_claims` table, confirms
the order, updates attempts, and inserts exactly one grant in the same transaction. Duplicate,
concurrent, replayed, redirect-only, or uncertain confirmation cannot grant a second extension.
Refund/reversal is also one atomic transaction that marks the payment's grant reversed and
recomputes the chain from remaining grants, ending access immediately when no valid paid time
remains. Provider selection remains a composition concern rather than a business-code branch.

## ADR-033: Per-user Instagram sessions use an encrypted owner-bound vault

**Status:** accepted

Instagram passwords and 2FA/checkpoint codes are transient web-flow input and are never durable.
The resulting session is stored in a dedicated owner-bound vault using AES-256-GCM, a random 96-bit
nonce, versioned envelope, active key ID/version, and associated data binding provider, credential
ID, Telegram owner, and monotonically increasing generation. Ignored least-privilege YAML holds one
active encryption key and retained decrypt-only keys for rotation; keys never enter SQLite, logs,
images, source, or metrics.

The application sees only `CONNECTED`, `EXPIRED`, `CHALLENGE_REQUIRED`, `REVOKED`, or
`DISCONNECTED`, safe timestamps, stable failure category, and generation. Disconnect/revoke erases
ciphertext immediately. Sanitized credential events expire after 90 days. One atomic expiring lease
per owner credential/generation prevents uncontrolled concurrent use.

Decryption occurs only after owner/generation/state/lease checks. A context-managed materializer
writes a Netscape file inside the exact job workspace with restrictive permissions, passes it as an
explicit ephemeral engine credential, and removes it on success, failure, cancellation, timeout,
and cleanup. Jobs/Redis may store safe references and policy only—never cookies, ciphertext,
passwords, codes, headers, usernames, or persistent plaintext paths.

**Implemented (T017) without routing media:** additive WAL credential/event/lease tables, one
active row per owner, AES-256-GCM envelopes with random 96-bit nonces bound to
provider/credential/owner/generation, a single atomic expiring lease, an admin-keyed rotation
path (decrypt-only previous keys) with re-encryption under the active key, and a context-managed
restricted Netscape materializer that cleans up on every exit path. Disconnect/revoke erases
ciphertext immediately while keeping sanitized 90-day events. No plaintext, key, nonce, password,
or 2FA value is durable or logged.

## ADR-034: Public operator credentials never authorize private Instagram media

**Status:** proposed

The existing canonical cookie file remains the operator credential, but authenticated-user routing
requires its Instagram account to be dedicated and verified as following zero accounts. An
explicit operator action records `OPERATOR_PUBLIC` attestation bound to the generation/keyed
verifier of the canonical file's Instagram records. Changing those records invalidates attestation.
If zero-follow status or identity cannot be verified, operator-backed Instagram routing fails
closed. No scheduled provider probe is introduced.

Application policy—not Telegram handlers or adapters—selects one of:

- `OPERATOR_PUBLIC`: current Free/public and VIP-without-session behavior;
- `USER_FIRST_PUBLIC_FALLBACK`: healthy connected VIP user first, then exactly one operator switch
  after a typed credential/session failure;
- `USER_ONLY`: restricted/private media through that user's session only.

Normalized inspection carries `PUBLIC`, `USER_RESTRICTED`, or `UNKNOWN`. An operator-context result
is deliverable only when it explicitly classifies as public. Once content is known restricted, the
operator context is structurally prohibited for inspection, mixed-media child resolution, download,
retry, and recovery. Unknown scope fails closed without confirming existence.

Public switching is limited to expired/invalid/login-required/credential-rejected user-session
failures and is persisted before the switch. Filesystem, FFmpeg, schema, post-processing, size,
delivery, cancellation, local-runtime, and generic failures never switch credentials. Private posts,
Reels, Stories, Highlights, mixed media, and Close Friends content require active capability,
connected owner credential, and visibility that account already possesses. The bot never bypasses
Instagram privacy controls.

This paid-bot VIP design does not restore ADR-013's removed Telegram Premium user session,
Telethon/MTProto uploader, staging channel, Premium queue, or `copyMessage` delivery.

## ADR-035: One least-privilege web companion separates account links and payment callbacks

**Status:** accepted

Use a separate, optional `aiohttp.web` companion process for browser-based Instagram connection and
machine payment callbacks. Reuse the existing dependency, but give the process only its database
path, web security material, handoff verification public key, vault-key access, and future provider
credentials. It must not receive or read the Telegram bot token.

The bot signs Ed25519 handoff claims containing purpose, Telegram owner, nonce, issued time, and a
five-minute expiry. The link places the token in the URL fragment; the browser POST-exchanges it,
and SQLite consumes a nonce hash exactly once. Browser sessions are Secure/HttpOnly/SameSite, every
mutation has CSRF protection, and responses enforce restrictive CSP/no-referrer policy. Interactive
password/2FA state is memory-only and expires within ten minutes.

Instagram and payment routes use separate request models, middleware, rate limits, and application
ports. Payment adapters verify signatures, freshness, and replay before billing sees a normalized
result; a browser redirect may display status but never activates an entitlement. HTTPS, trusted-
proxy rules, body/time limits, non-permissive CORS, secret-redacted access logs, and bounded labels
are mandatory. The companion remains disabled until its threat model, deployment, and security
tests are complete.

**Implemented (T016) as the disabled optional boundary:** bot-signed Ed25519 handoff claims
(purpose, owner, nonce, issued time, five-minute expiry) delivered in the URL fragment and
POST-exchanged into a Secure/HttpOnly/SameSite browser session with synchronizer CSRF. Each
SHA-256 nonce hash is consumed exactly once in additive SQLite/WAL. Instagram browser routes and
machine payment-callback routes have independent request handling/middleware; with no provider
adapter registered the payment callback route fails closed (`404`, never a confirmation). A
reduced `CompanionSettings` maps no Telegram token and no signing key, so the process cannot read
the bot token. Restrictive CSP/no-referrer/x-frame headers, trusted-proxy rules, and body/
time/rate limits are enforced; all tests are network-free and deterministic. No Instagram login or
payment gateway is implemented.

## ADR-036: Typed audit events use a durable, isolated logger outbox

**Status:** proposed

The Operator Logger is distinct from stdout and structured application logs. Application services
emit project-owned `AuditEvent` values with one of `ERROR`, `COOKIE_HEALTH`, `USER_SUBMISSION`, or
`SYSTEM` categories, a severity, UTC timestamp, correlation/request/update ID, optional job ID,
content/provider classification, sanitized message, source-message references, and an explicitly
approved numeric Telegram user ID. Business services do not branch on destination names or
administrator IDs.

After a request is durably accepted, logger work is written to SQLite/WAL before normal processing
continues. A per-destination dispatcher records `PENDING`, `COMPLETED`, and `UNCERTAIN` (or an
equivalent quarantine state), uses bounded retries and leases, and isolates channel failures. A
Telegram timeout or ambiguous response is never treated as proof that a duplicate send is safe.
No logger destination falls back to all `telegram.admin_ids`; without a usable destination the
system emits only structured logs and bounded health/metric signals. Logger failure cannot change a
job outcome, cancellation, cleanup, or delivery-uncertainty state.

## ADR-037: Logger destinations reconcile configuration and runtime ownership

**Status:** proposed

The future strict `telegram.logger.enabled/channels` configuration supplies numeric `-100...`
channel IDs, while authorized administrators may add durable runtime destinations. The effective
set is a deduplicated union keyed by `chat_id`. Config-managed rows remain configuration-owned and
cannot be falsely removed through the Telegram UI; they disappear only after configuration reload or
restart. Runtime rows remain durable until an authorized removal.

Each destination has independent `ACTIVE`, `UNREACHABLE`, `FORBIDDEN`, and `DISABLED` health. Bot
membership and posting permission are validated before activation. A removed or forbidden channel
updates only that row and does not stop delivery to healthy channels or the user-facing workflow.
The admin panel is the only runtime management surface and reauthorizes every action using the
current `telegram.admin_ids`.

## ADR-038: Original submissions are private audit copies with explicit indefinite retention

**Status:** proposed

`USER_SUBMISSION_RECEIVED` is emitted only after a real download submission is durably accepted.
URL text, photo, video, document, audio, animation, captions, supported attachments, and albums are
copied with Telegram-native `copyMessage`/`copyMessages` where possible. Album order, all items,
captions, and one logical submission identity are preserved. The original message is not edited or
deleted, and control interactions are excluded. The original user-entered URL remains in the
private copy; canonical/provider classification is separate correlation metadata.

Before activation, users see: «برای اجرای سرویس و پشتیبانی/امنیت، لینک‌ها و رسانه‌هایی که برای دانلود می‌فرستید ممکن است در کانال خصوصی عملیاتی لاگر کپی و به‌صورت نامحدود نگهداری شوند؛ با ادامهٔ استفاده موافقت می‌کنید.» Audit copies and safe metadata are retained indefinitely in the first implementation; no automatic
Telegram deletion is introduced. Any later manual purge must be bounded and idempotent.

Private channels have minimal human membership and bot post-only access. Cookies, passwords, 2FA,
authorization headers, bot tokens, credentials, filesystem paths, raw exceptions, Instagram
sessions, signed login tokens, payment secrets, and gateway credentials are prohibited from audit
events, outbox rows, logs, metrics, and Telegram messages.

## ADR-039: CI validation is fast by default, conservative by classification, and full for releases

**Status:** accepted

CI provides three explicit validation tiers: an always-reporting fast lane for ordinary source and
documentation changes, conditional heavy lanes selected by a deterministic changed-path classifier
(`scripts/ci_change_policy.py`), and a full mode for manual, scheduled, and release validation.
The classifier is repository-owned and unit-testable without GitHub Actions; it computes the real PR
base/head or push before/head range, tallies every matching category, and requests all heavy lanes on
any unknown, root-critical, workflow/policy, or unclassifiable change because uncertainty never
permits a safe skip.

Stable `change-detection`, `quality` (the fast lane, keeping branch-protection compatibility with the
former required check), and `final-ci-gate` checks report on every development run. The conditional
heavy lanes are `dependency`, `package`, `plugin-sdk`, `docker-runtime`, `updater-integration`,
`installer-linux`, and `installer-windows`. The final gate evaluates `needs` with an always-run
condition and understands `success`, `failure`, `cancelled`, and `skipped` structurally: an explicitly
irrelevant heavy job may succeed by skipping, while a relevant failure, timeout, or cancellation
fails the gate. Workflow-level `paths:` filters are prohibited because they can leave required checks
permanently pending. Development/PR runs cancel superseded work for the same ref, but publication
uses a separate non-destructive concurrency policy.

Docker image/runtime correctness and the historical privileged updater/rollback matrix are separate
conditional lanes; `updater-integration` builds and loads the same `telegram-media-downloader-bot:ci`
image from the shared GHA cache itself before its matrix, because it is independently triggerable
(`updater` can be true while `docker` is false) and must not depend on a build from another lane.
Installer Linux/Windows, dependency/package, and plugin SDK checks activate from
their owned paths; full/manual/nightly validation forces all lanes. The tag-only publication
workflow remains independently full and fail-closed, retaining tag/package/release-policy checks,
published-image smokes, updater compatibility, reproducible archives, and release ordering.
Passing fast CI alone never proves a release safe. `release-policy.json` changes expand updater and
both installer lanes so a withdrawn-release safety regression cannot pass under fast gating.

Branch protection's stable merge-blocking required check is `final-ci-gate`, because it is the
aggregate safety check that depends on `change-detection`, the `quality` fast lane, and every
conditional heavy lane (`dependency`, `package`, `plugin-sdk`, `docker-runtime`,
`updater-integration`, `installer-linux`, `installer-windows`). `quality` is the explicit
fast-feedback check and may also be required for visibility, but `quality` + `change-detection`
alone is NOT a sufficient merge gate: a relevant heavy lane (for example `docker-runtime` after a
`Dockerfile` change) can fail while `quality` and `change-detection` pass, and only
`final-ci-gate` surfaces that failure. Recommended required checks: `final-ci-gate` (required) and,
optionally, `quality` (required for visibility). `change-detection` does not need to be required
separately because `final-ci-gate` already depends on it and fails whenever classification fails.
No removed job name should remain required.
