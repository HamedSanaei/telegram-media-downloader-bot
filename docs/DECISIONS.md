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
