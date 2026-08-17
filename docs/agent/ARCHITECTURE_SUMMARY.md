# Architecture navigation summary

This is a routing aid, not a replacement for `docs/ARCHITECTURE.md`.

```text
Telegram / aiogram bot
        |
        | durable job + project-owned queue payload
        v
SQLite/WAL <----> Redis / ARQ ----> worker
                                  |
                                  v
                       application services/ports
                                  |
                    +-------------+-------------+
                    v                           v
             GalleryDlEngine               YtDlpEngine
                    |                           |
             gallery-dl process          yt-dlp + ffmpeg
                    +-------------+-------------+
                                  v
                         Telegram delivery port
                                  |
                         cloud or Local Bot API
```

## Boundaries

- Dependency direction is presentation/infrastructure -> application ports -> domain. Bootstrap is
  the composition root.
- Bot handlers validate and enqueue; blocking inspection/download runs in the worker.
- Only `infrastructure/ytdlp/` imports `yt_dlp`; raw upstream mappings never cross that adapter.
- Gallery-dl stays behind its argv/subprocess adapter. Durable state contains stable media identity,
  never signed CDN or `ytdl:` URLs.
- The router gives image-containing supported social posts to gallery-dl and preserves the strict
  yt-dlp video path where the normalized plan requires it.
- SQLite/WAL is durable truth for jobs, transitions, selections, receipts, cancellation, and usage.
  Redis/ARQ is transient queue/rate-limit/cache state.
- Telegram delivery is a project port. Local Bot API lifecycle, endpoint migration, leases, and
  readiness stay inside the Telegram infrastructure boundary.
- Job workspaces are isolated below the configured download/temp roots and are cleaned on every
  terminal outcome without following symlinks.

For architecture, persistence, queue, cancellation, concurrency, security, cleanup, configuration,
upgrade/rollback, backward-compatibility, or release changes, read the relevant sections of
`docs/ARCHITECTURE.md`, the ADR selected by `DECISION_INDEX.md`, and the applicable task history
before editing. Graphify locates relationships; it does not authorize a boundary change.

