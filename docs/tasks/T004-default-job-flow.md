# T004 - Default queue flow

**Status:** complete (durable YouTube URL canonicalization added 2026-07-29)

Implement URL message handling, ARQ enqueueing, worker composition, default-mode download, Telegram
document upload, and cleanup. Preserve responsiveness and error sanitization.

Single-video YouTube URLs are canonicalized before SQLite persistence and Redis enqueue, and again
at worker execution so retries, recovery, and legacy raw payloads cannot expand a Mix.
