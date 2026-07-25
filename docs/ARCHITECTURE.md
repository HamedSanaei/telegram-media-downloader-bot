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
- `MediaFormatOption` for a real semantic candidate and its size-confidence metadata;
- `OutputContainer`/`ContainerPolicy` for native-only or guaranteed MP4/WebM/MP3 output;
- `DownloadRequest` for semantic requests;
- `DownloadResult`/`DownloadArtifact` for one or an ordered set of final files;
- `DeliveryProgressEvent` for packaging, byte transfer, and opaque finalization;
- project exceptions for failure categories.

Raw yt-dlp info dictionaries, extractor objects, format IDs, hooks, and exceptions remain inside the
adapter package.

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

`best_original` is native-only and never transcoded. Ordinary MP4 and WebM selections prefer a
native candidate, then guarantee MP4 H.264/AAC or WebM VP9/Opus through cancellable FFmpeg
conversion. Fixed resolution and at-most-60-FPS contracts are preserved.

Fixed-resolution candidates are exact-height contracts: `video_2160` is absent unless a real 2160p
stream can be combined with audio, and download cannot silently fall back. Inspection and download
use the same adapter-owned bounded selector. Component sizes are summed from `filesize`, then
`filesize_approx`, then bitrate and duration; incomplete metadata remains explicitly unknown.

Delivery reads through a tracked `InputFile`. Byte progress ends when the HTTP body has streamed to
Local Bot API. Since Bot API exposes no subsequent byte callback, final response waits emit only
elapsed-time heartbeats. Each successful multipart receipt is persisted before the next volume.

## Runtime control plane

The worker exposes internal-only `/health`, `/ready`, and Prometheus `/metrics` endpoints. Readiness
covers Redis, SQLite, writable storage, Telegram, ffmpeg, and the engine. Compose does not publish
the port to the host by default; the worker container health check consumes it internally.

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
and ordered video artifacts are delivered separately. Cookies remain an optional read-only operator
file. Telegram handlers contain no extractor/domain-name dispatch chain.
