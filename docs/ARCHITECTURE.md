# Architecture

## System view

```text
Telegram user
    |
    v
aiogram bot process
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
- `DownloadRequest` for semantic requests;
- `DownloadResult` for final files;
- project exceptions for failure categories.

Raw yt-dlp info dictionaries, extractor objects, format IDs, hooks, and exceptions remain inside the
adapter package.

## Processes

### Bot

- long polling initially;
- validates basic URL and user policy;
- creates an immutable queue payload;
- does not download media;
- remains responsive while workers are busy.

### Worker

- owns engine calls and local job directories;
- executes blocking yt-dlp calls via a thread boundary;
- uploads the result in the initial implementation;
- cleans temporary files;
- can be horizontally scaled after idempotency work.

## File isolation

Each job uses:

```text
/data/temp/<job-id>/
/data/downloads/<job-id>/
```

No user-provided title becomes a directory name. Output paths are resolved and checked beneath the
configured root.

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
