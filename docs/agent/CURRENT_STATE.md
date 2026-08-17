# Current implementation state

This file contains starting facts only. Historical fixes and verification evidence remain in
`docs/STATUS.md` and `docs/HANDOFF_REPORT.md`.

- Package version: `1.3.3`; Python baseline: 3.14 or newer; dependencies are locked by `uv.lock`.
- Runtime topology: separate aiogram bot and ARQ worker, Redis for transient queue/cache state,
  SQLite/WAL for durable state, and optional dedicated Local Telegram Bot API service.
- Media engines: yt-dlp behind the sole Python adapter; gallery-dl 1.32.8 behind an isolated strict
  subprocess adapter; `RoutedMediaEngine` owns normalized routing.
- Instagram supports posts, Reels, mixed carousels, exact Story items, and profile-avatar actions.
  Image-bearing plans retain gallery ownership; mixed video children use the strict yt-dlp path.
- Public video choices are actual native AV1/H.264 MP4 or VP9 WebM plans. `best_original` is
  native-only and incompatible inline video may be sent as a document.
- All runtime consumers reopen one canonical Netscape cookies file for subsequent jobs. Divergent
  legacy gallery cookie aliases fail configuration validation.
- Cancellation is durable-first. Delivery uncertainty is quarantined. Every terminal job outcome
  triggers idempotent, symlink-safe download/temp cleanup.
- The updater performs candidate/static preflight, offline post-install verification while writers
  are stopped, conditional post-start online checks, exact service-state restoration, and full
  rollback on transaction failure.
- Bot and worker use a bounded Local API startup readiness wait; a permanently unavailable endpoint
  still fails non-zero.
- Tasks T001 through T013 are implemented. Before changing an established feature, load its relevant
  task file and current detailed status section rather than treating this summary as history.

