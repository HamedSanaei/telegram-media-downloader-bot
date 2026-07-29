# Architecture

## System view

```text
Telegram user
    |
    v
aiogram bot process
    |
    +---- project Telegram runtime ---- public or migrated Local Bot API
    |
    | enqueue project-owned DownloadJob payload
    v
Redis / ARQ
    |
    v
worker process
    |
    v
DownloadService (application)
    |
    v
DownloadEngine port
    |
    v
YtDlpEngine adapter
    |
    v
yt-dlp + ffmpeg
```

SQLite/WAL under `/data/state` is the durable control plane shared by the bot and worker. Redis is
the transient queue/rate-limit/membership-cache plane and is not the source of truth for completed
delivery state or user usage.

## Dependency direction

```text
telegram ---------> application ---------> domain
workers ----------> application ---------> domain
infrastructure ---> application ports ----> domain
bootstrap --------> all concrete adapters (composition only)
```

The domain layer imports no framework. The application layer does not import aiogram, ARQ, Redis,
or yt-dlp. Infrastructure implements application ports.

## Stable internal contract

The engine port exposes normalized project models:

- `MediaInfo` for inspection;
- `MediaFormatOption` for a real selected-stream candidate, normalized codec/geometry/size fields,
  and adapter-supplied selected-format identity;
- `NativeOptionView` for a deduplicated, public, non-transcoding Telegram choice;
- `OutputContainer`/`ContainerPolicy` for native-only or guaranteed MP4/WebM/MP3 output;
- `DownloadRequest` for semantic requests;
- `DownloadResult`/`DownloadArtifact` for one or an ordered set of final files;
- `DeliveryProgressEvent` for packaging, byte transfer, and opaque finalization;
- project exceptions for failure categories.

Raw yt-dlp info dictionaries, extractor objects, hooks, and exceptions remain inside the adapter
package. Selected format IDs are normalized into an immutable project tuple for deduplication,
integrity validation, and structured logging; they never become callback data or user-facing raw
choices. Telegram callbacks contain only a short opaque option digest.

## Processes

### Bot

- long polling initially;
- validates public DNS results and static/durable user policy;
- enforces membership in every configured channel through a cached Telegram gateway;
- upserts the Bot API user profile and request counters through a persistence port;
- creates a durable job record before its immutable queue payload;
- reads owner-bound, expiring selections for callbacks;
- does not download media;
- remains responsive while workers are busy.

### Worker

- owns engine calls and local job directories;
- executes blocking yt-dlp calls via a thread boundary;
- publishes normalized inspection UI and uploads through a `DeliveryGateway` port;
- persists transitions, attempts, cancellation, and delivery receipts;
- maps download and delivery progress through a bounded, throttled presentation/logging channel;
- cleans temporary files;
- is deployed as one worker container until a leased multi-host store is introduced.

## File isolation

Each job uses:

```text
/data/temp/<job-id>/
/data/downloads/<job-id>/
```

No user-provided title becomes a directory name. Output paths are resolved and checked beneath the
configured root.

Interrupted `running` jobs are requeued on startup. Jobs interrupted during `delivering` become
`delivery_uncertain`; automatic retry is blocked because Telegram has no upload idempotency key.
This trades a possible manual resend for prevention of an uncontrolled duplicate.

Cancellation is a durable-first state transition. The bot atomically marks an owned cancellable job
`cancelled` with `cancel_requested=1`, then requests official ARQ abort and removes finalized
enqueue/retry/in-progress/result state. ARQ workers enable abort support. Startup reconciliation
always converts legacy cancelled queued/running/retrying rows to `cancelled` and never enqueues them;
the operation is idempotent. A simultaneous user cancellation and worker shutdown resolves to user
cancellation, while an ordinary shutdown leaves a healthy pre-delivery job recoverable.

## Update isolation

Potential upstream compatibility changes are constrained to:

```text
infrastructure/ytdlp/engine.py
infrastructure/ytdlp/mapper.py
infrastructure/ytdlp/options.py
infrastructure/ytdlp/error_mapper.py
```

A genuine change to the project-owned engine port requires an ADR and coordinated tests.

## Extension points

- Additional engines implement the same port.
- Source-specific custom extraction belongs in an external `yt-dlp` plugin package.
- Storage can be replaced behind a future storage port.
- Queue implementation can be replaced without changing Telegram handlers.

## Large-file routing

- `<= telegram.max_upload_size_mb`: direct Local Bot API delivery.
- `> telegram.max_upload_size_mb` and `<= multipart.max_total_size_mb`: stored multi-volume ZIP
  documents, each bounded by `multipart.part_size_mb` and sent through Local Bot API.

`best_original` is native-only and never transcoded. Public MP4 selection is a zero-transcode
container contract supporting AV1/AAC and H.264/AAC: codec families are planned independently
before quality/bitrate ranking, and the configured deterministic fallback either chooses the
highest lower resolution in that family or reports no compatible format. Public WebM selects native
VP9 + Opus. Every public video option is checked once
while building the catalog and again immediately before durable job creation; neither button starts
a codec conversion. Public video jobs retain the codec-family-filtered `GUARANTEED` contract: a
source-format change between inspection and download fails safely, while only
`EXPLICIT_TRANSCODE` may encode. The internal explicit-MP4 policy may still convert AV1/VP9 for non-public
administrative/development flows after a conservative timeout estimate accepts the work.

Container mux compatibility, Telegram inline-video streamability, and document delivery are
separate decisions. AV1/AAC MP4 is a valid native artifact and is delivered as a document when it
does not satisfy the H.264/AAC inline-video profile. `best_original` normalizes
every accidental guaranteed-container request to native-only before it reaches the adapter.
Transcoding first uses a quality-oriented CRF pass; the configured maximum is only a ceiling, and
bitrate limiting is a fallback when that pass exceeds the ceiling or would multiply a small
source's size.

Heavy transcoding is separately bounded from general worker concurrency: FFmpeg receives a real
encoder thread limit, a process-local semaphore bounds simultaneous encodes, a wall-clock timeout
terminates the process group, and an operator may disable forced conversion. Progress is parsed from
FFmpeg's machine-readable channel without exposing source paths. Docker can optionally add a worker
CPU quota through `.env`.

Fixed-resolution candidates are exact-height contracts except for the documented MP4 codec-family
fallback. The application planner labels only actual selected height/FPS/dynamic range/codecs and
deduplicates plans by selected streams plus those output properties. Thus several requested modes
that resolve to one 1080p stream produce one 1080p choice. Exact `filesize` wins over
`filesize_approx`; selected video and audio components are summed, bitrate/duration is only an
estimate, and incomplete metadata remains explicitly unknown.

Navigation callbacks are versioned (`c2`, `o2`, `n2`), owner-bound, expiring, and below Telegram's
64-byte limit. Back transitions form `quality -> output type -> send a new link`, edit the same
message, and read the existing SQLite selection. Legacy `container:`/`fmt:` callbacks never create a
job and instead redirect to the current Native menu.

Delivery reads through a tracked `InputFile`. Byte progress ends when the HTTP body has streamed to
Local Bot API. Since Bot API exposes no subsequent byte callback, final response waits emit only
elapsed-time heartbeats. Each successful multipart receipt is persisted before the next volume.

## Runtime control plane

The worker exposes internal-only `/health`, `/ready`, and Prometheus `/metrics` endpoints. Readiness
covers Redis, SQLite, writable storage, Telegram, ffmpeg, and the engine. Compose does not publish
the port to the host by default; the worker container health check consumes it internally.

Linux release updates run from an isolated script copy and validate the complete staged Bash,
Compose, configuration, and executable-mode payload before stopping writers. Top-level application
entries use rollback snapshots, while local configuration/state remain outside replacement. A
same-UID filesystem write and SQLite WAL probe must pass before candidate startup. Container
running/health state is then mandatory; any post-stop failure restores the prior source, image,
usable permissions, command link, and previous service set.

## Telegram endpoint control plane

`infrastructure/telegram/local_api.py` owns managed server lifecycle, migration state, endpoint
leases, safe status, and public/local transitions. `telegram/bot_factory.py` is the shared Bot/Worker
composition point. Both processes resolve the same endpoint from YAML plus durable migration state.
Cross-process leases reject mixed endpoints and keep a managed server alive until the final local
client exits. Migration writes its intent before each non-idempotent `logOut`; an uncertain result
is quarantined and never repeated automatically.

The Docker topology can assign Local API lifecycle ownership to a dedicated `local-api` service.
That service reads credentials from mounted YAML and injects them only into the official child
process environment. Bot and Worker connect through `http://local-api:8081`; no credential is
placed in Compose environment or command arguments.

## Source-specific policy without handler coupling

Source detection remains inside the yt-dlp adapter. The worker consumes normalized `source` policy:
Instagram collections are the approved automatic multi-artifact flow, image entries are discarded,
and ordered video artifacts are delivered separately. With `force_mp4`, the adapter selects the
best native MP4 video and M4A audio and only merges/remuxes them; without it, the source-selected
container is preserved. Cookies remain an optional read-only operator file. Telegram handlers
contain no extractor/domain-name dispatch chain.
