---
name: media-engine-change
description: Scope yt-dlp, gallery-dl, media routing, format selection, Instagram, or adapter contract changes. Use for inspection/download regressions, source support, parser changes, native formats, mixed media, and upstream dependency updates.
---

# Media engine change

Query Graphify for the affected adapter, `RoutedMediaEngine`, application callers, and tests; for
example `graphify query "Trace FEATURE through media inspection, routing, download, and tests"`.
Then inspect:

- `src/telegram_media_bot/infrastructure/ytdlp/` or `infrastructure/gallerydl/`;
- `infrastructure/media_engine_router.py` and project-owned domain/port models;
- matching unit fixtures, integration tests, and opt-in contracts;
- T003/T013 plus ADR-002, ADR-019 through ADR-022, or ADR-027 as relevant.

Keep `yt_dlp` imports inside its adapter, raw vendor data inside adapter boundaries, gallery-dl in
its subprocess isolation, and signed/pseudo URLs out of durable state. Preserve strict failure
translation and existing Twitter HLS, Instagram ordering, cancellation, and cleanup contracts.

