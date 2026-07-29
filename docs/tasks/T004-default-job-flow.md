# T004 - Default queue flow

**Status:** complete (terminal zero-retention added 2026-07-29)

Implement URL message handling, ARQ enqueueing, worker composition, default-mode download, Telegram
document upload, and cleanup. Preserve responsiveness and error sanitization.

Single-video YouTube URLs are canonicalized before SQLite persistence and Redis enqueue, and again
at worker execution so retries, recovery, and legacy raw payloads cannot expand a Mix.
Every terminal path invokes the shared exact-workspace cleanup component without masking the
original result.
