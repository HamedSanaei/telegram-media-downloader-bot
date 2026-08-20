# Code map

| Path | Responsibility |
|---|---|
| `src/telegram_media_bot/domain/` | Stable models, enums, identifiers, exceptions, and `best_original` native-only normalization |
| `src/telegram_media_bot/domain/failures.py` | Typed structured `FailureContext` (adapter/extractor/source/fallback/HTTP status/retry history/stage/etc.) that survives to the terminal admin notification; bounded `FailureStage`; size-bounded Persian rendering that omits absent fields |
| `src/telegram_media_bot/domain/cookie_health.py` | Cookie Health Center states (HEALTHY/EXPIRING_SOON/EXPIRED/AUTH_FAILED/MISSING/MALFORMED/UNVERIFIED/CHECK_ERROR), static/probe result models, and definitive blocking states |
| `src/telegram_media_bot/domain/cookies.py` | The single cookie-provider registry (YouTube/Instagram/TikTok/X/Pinterest/SoundCloud domains and labels), upload size contract, and merge summary |
| `src/telegram_media_bot/application/ports/` | Interfaces required by use cases |
| `src/telegram_media_bot/application/ports/cookie_management.py` | Framework-free canonical cookie merge/export contract |
| `src/telegram_media_bot/application/services/` | Orchestrates inspection, policy limits, and selected downloads |
| `src/telegram_media_bot/application/services/diagnostic_sanitizer.py` | Central secret sanitizer: URL reduction to scheme+hostname+safe path, safe-parameter allowlist, redaction of cookies/tokens/headers/proxy passwords/CDN query secrets, bounded exception-message cleanup |
| `src/telegram_media_bot/application/services/cookie_health_service.py` | Cookie Health orchestration: provider-scoped or complete static+probe merge, persisted state-transition alerts, reminder/recovery deduplication, runtime auth-failure updates |
| `src/telegram_media_bot/application/services/native_options.py` | Builds the public native-only option catalog, enforces codec/transcode invariants, chooses truthful representatives, deduplicates actual plans, and creates opaque option IDs |
| `src/telegram_media_bot/application/services/job_service.py` | Durable job creation and active-job idempotency |
| `src/telegram_media_bot/application/services/instagram_delivery.py` | Selects the complete Instagram image/mixed bundle behind the Photo/File confirmation |
| `src/telegram_media_bot/application/services/url_canonicalization.py` | Parses YouTube URL intent, removes Mix context from single videos, gives equivalent X/Twitter status-share URLs one query-free identity, and canonicalizes Instagram share/story/profile URLs (tracking stripped, plain profiles rewritten to the `/USERNAME/avatar/` avatar target) |
| `src/telegram_media_bot/application/services/usage_analytics.py` | Builds Tehran-local usage reports and excludes configured administrators from public KPI aggregation |
| `src/telegram_media_bot/application/ports/usage_analytics.py` | Framework-free usage activity and PNG renderer contracts |
| `src/telegram_media_bot/application/services/progress.py` | Framework-free download/delivery progress throttling |
| `src/telegram_media_bot/application/services/access_policy.py` | Static/dynamic access, required-channel membership, and rate policy |
| `src/telegram_media_bot/application/ports/membership.py` | Framework-free required-channel membership contract |
| `src/telegram_media_bot/application/ports/user_repository.py` | Durable profile and usage-accounting contract |
| `src/telegram_media_bot/infrastructure/ytdlp/` | The only direct yt-dlp integration, including strict raw-entry Instagram mixed-carousel video resolution, zero-transcode AV1/H.264 MP4 and VP9 WebM selection, narrow Twitter HLS audio-metadata inference, native/inline compatibility probing, and bounded explicit transcoding |
| `src/telegram_media_bot/infrastructure/gallerydl/` | Isolated gallery-dl 1.32.8 argv/subprocess, explicit JSON Lines event contract, bounded output/cancellation, strict non-empty vendor tuple parsing/error mapping, successful-empty unavailable classification, stable asset normalization, and safe original-image download with Instagram videos disabled when required. The typed result model carries IMAGE/VIDEO/mixed collections (Stories, Reels, video posts, avatar) without conflating "no images" with "no media" |
| `src/telegram_media_bot/infrastructure/media_engine_router.py` | Inspection-result routing and fail-closed mixed Instagram merge: validate/download canonical yt-dlp video children before gallery-dl images, then merge exact source ordinals |
| `src/telegram_media_bot/infrastructure/image_validation.py` | Pillow signature/format/dimension/decompression-bomb validation without altering originals |
| `src/telegram_media_bot/infrastructure/ytdlp/native_selection_smoke.py` | Packaged, network-free runtime-image assertion for AV1/H.264 MP4 and VP9 WebM selection, stream-copy arguments, and Best Original policy |
| `src/telegram_media_bot/infrastructure/queue/` | ARQ enqueue plus official abort and transient-key finalization |
| `src/telegram_media_bot/infrastructure/persistence/` | SQLite/WAL jobs, durable-first cancellation, users, daily usage, delivery, block, and recovery store |
| `src/telegram_media_bot/infrastructure/persistence/sqlite_usage_analytics.py` | Read-only mapping of durable SQLite jobs/events into project-owned usage activity |
| `src/telegram_media_bot/infrastructure/analytics/` | Pillow usage dashboards, bundled-font doctor, and deterministic Docker smoke fixtures |
| `src/telegram_media_bot/assets/fonts/` | Package-bundled Noto Sans runtime font and SIL OFL 1.1 license |
| `src/telegram_media_bot/infrastructure/security/telegram_membership.py` | Telegram membership gateway with positive/negative Redis cache |
| `src/telegram_media_bot/infrastructure/security/` | Public URL/DNS validation, Redis rate limiting, and membership cache |
| `src/telegram_media_bot/infrastructure/cookies/` | Strict Netscape parsing, supported-service detection, deterministic scoped merge, restricted backup, atomic canonical-file replacement, network-free static health checks, and lightweight authenticated probes |
| `src/telegram_media_bot/infrastructure/persistence/sqlite_cookie_health.py` | Durable Cookie Health state (status/static/active/last-notified/reminder) surviving restarts |
| `src/telegram_media_bot/infrastructure/observability/` | Health HTTP server and Prometheus metrics registry |
| `src/telegram_media_bot/infrastructure/telegram/local_api.py` | Local Bot API lifecycle, durable migration, endpoint leases, and safe status |
| `src/telegram_media_bot/infrastructure/archive/` | Safe 7-Zip multi-volume packaging, deterministic ordered image ZIPs, and SHA-256 manifests |
| `src/telegram_media_bot/infrastructure/storage/` | Exact job-workspace cleanup, symlink-safe deletion, and startup/maintenance sweeping |
| `src/telegram_media_bot/telegram/` | Versioned Back/Native/Instagram/Story/Highlight delivery callbacks, real-plan rendering, middleware, tracked exact-byte document delivery, and ordered ten-item media-group planning |
| `src/telegram_media_bot/telegram/admin_menu.py` | Central administrator button constants (reports, cookie management, Cookie Health), FSM state, and reply/inline keyboard builders; `admin_handlers.py` verifies cookie writes, refreshes provider health immediately, and makes unchanged status edits idempotent |
| `src/telegram_media_bot/telegram/admin_handlers.py` | Role-checked menu/download/report/cookie routing, private-chat secret export, bounded in-memory document intake, and per-admin report single-flight coordination |
| `src/telegram_media_bot/telegram/handlers.py` | Shared URL submission, editable job-status ownership, active-job queue reconciliation, callbacks, and cancellation routing |
| `src/telegram_media_bot/telegram/bot_factory.py` | Shared Bot/Worker Telegram endpoint and client construction plus the bounded, cancellable Local Bot API startup readiness wait (`local_api_startup_wait`/`ready`/`timeout`) |
| `src/telegram_media_bot/workers/` | ARQ worker settings and job functions, including edit-or-send inspection publication, redacted terminal-failure alerts to configured administrators (rich `FailureContext`), cookie-health watcher, retries, receipts, and workspace cleanup |
| `src/telegram_media_bot/bootstrap/` | Config, logging, composition roots, and fail-fast effective-cookie path identity |
| `src/telegram_media_bot/cli.py` | Management CLI plus full, fail-closed offline-static, and explicitly service-selected online doctor modes |
| `tests/unit/` | Fast deterministic tests |
| `tests/fixtures/` | Versioned configuration and sanitized upstream-metadata fixtures for network-free regression tests |
| `tests/integration/` | Local integration tests with fakes/Redis where available |
| `tests/integration/test_local_api_large_upload.py` | Explicit opt-in real Local API upload over 200 MB |
| `tests/contracts/` | Opt-in external yt-dlp smoke tests |
| `docs/tasks/` | Ordered implementation tasks for Codex |
| `docs/agent/` | Compact task routing, architecture/current-state summaries, ADR index, and the supported Graphify query/freshness workflow; never a replacement for detailed docs |
| `.agents/skills/` | Validated repository-local Skill trees for navigation, media engines, Telegram delivery, worker jobs, persistence, and release/updater work |
| `.graphifyignore` | Project-scoped Graphify exclusions for secrets, runtime state, caches, generated artifacts, media, logs, and local graph output |
| `plugins/example_extractor/` | Independent external yt-dlp extractor plugin SDK/template |
| `scripts/agent_context.py` | Dependency-free, bounded AST fallback for overview, symbols, imports, reverse imports, references, and likely tests |
| `scripts/check_agent_context.py` | Deterministic CI guard for routing docs/Skills, query-first/source-authority guidance, preserved quality references, exclusions, and no runtime Graphify dependency |
| `scripts/upgrade_ytdlp.py` | Reviewed engine upgrade, verification, and report workflow |
| `scripts/compare_canary.py` | Baseline/canary failure-rate promotion gate |
| `scripts/generate_file_manifest.py` | Deterministic SHA-256 source-manifest generation |
| `scripts/check_package_assets.py` | Wheel/sdist font-license inspection and clean-wheel resource/decode smoke |
| `scripts/check_gallerydl_fixtures.py` | Pinned 1.32.8 normalized-fixture upgrade contract and optional installed-version check |
| `install.sh`, `install.ps1` | Interactive Docker-first one-line installers and global management command setup |
| `scripts/tmb.sh`, `scripts/tmb.ps1` | Cross-platform lifecycle/menu/update/backup/cleanup command; Linux adds prepared-image/read-only-data static preflight, pre-downtime image pulls, exact writer/service-state tracking, private atomic offline backups, isolated transactional replacement, offline post-install plus conditional post-start online verification, runtime probes, SIGINT recovery, rollback, `tmb` repair, and guarded project-image reclamation |
| `scripts/build_release_archives.sh` | Reproducible tar/ZIP assets, checksummed standalone updater bootstrap, v1.0.2-safe executable Linux updater packaging, and an explicit deterministic epoch for current-tree integration fixtures |
| `scripts/tests/` | Mocked recovery plus opt-in privileged release-upgrade tests for phased offline/online verification, exact mixed service restoration, active Local API logs, archive inclusion/exclusion, redacted failures, SIGINT, runtime identity, filesystem/SQLite permissions, Compose bind contracts, and the delayed Local API startup readiness regression (`test_local_api_readiness.sh`) |
| `.github/workflows/ci.yml` | Quality/security and agent-context guardrails, Compose validation, shared-cache Docker build, runtime dependency doctor, native-selector/remux, CLI, and multipart smoke tests |
| `.github/workflows/publish-container.yml` | Tag-only GHCR amd64 publication using the CI cache, runtime doctor/native-selector/remux/CLI/multipart smoke tests, and reproducible release assets |

## Upstream compatibility hot spots

- `infrastructure/ytdlp/engine.py`: `YoutubeDL` lifecycle and calls;
- `infrastructure/ytdlp/options.py`: semantic mode mapping, single-video `noplaylist` defense, codec-family native selection, deterministic lower-resolution policy, and bounded complete-stream selection;
- `infrastructure/ytdlp/transcoder.py`: separate native-container and inline-streamability probes,
  quality-first MP4 H.264/AAC and WebM VP9/Opus conversion, and size-limited fallback;
- `infrastructure/ytdlp/mapper.py`: upstream metadata to `MediaInfo`;
- `infrastructure/ytdlp/error_mapper.py`: upstream errors to project exceptions.

## Durable state ownership

- `domain/models.py`: stable job, normalized selected-stream/native-option view, explicit image delivery mode, source ordinals, progress, selection, health, and delivery records;
- `application/ports/job_repository.py`: persistence contract;
- `infrastructure/persistence/sqlite_repository.py`: schema and transition implementation;
- `workers/jobs.py`: transitions, retry, delivery progress/logging, per-item receipts, redacted
  terminal-failure administrator alerts, and cleanup;
- `telegram/handlers.py`: owner validation, admin controls, and safe enqueue ordering.
