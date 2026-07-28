# Code map

| Path | Responsibility |
|---|---|
| `src/telegram_media_bot/domain/` | Stable models, enums, identifiers, exceptions, and `best_original` native-only normalization |
| `src/telegram_media_bot/application/ports/` | Interfaces required by use cases |
| `src/telegram_media_bot/application/services/` | Orchestrates inspection, policy limits, and selected downloads |
| `src/telegram_media_bot/application/services/job_service.py` | Durable job creation and active-job idempotency |
| `src/telegram_media_bot/application/services/progress.py` | Framework-free download/delivery progress throttling |
| `src/telegram_media_bot/application/services/access_policy.py` | Static/dynamic access, required-channel membership, and rate policy |
| `src/telegram_media_bot/application/ports/membership.py` | Framework-free required-channel membership contract |
| `src/telegram_media_bot/application/ports/user_repository.py` | Durable profile and usage-accounting contract |
| `src/telegram_media_bot/infrastructure/ytdlp/` | The only direct yt-dlp integration, codec-first native MP4/WebM selection, native/inline compatibility probing, and preflight/thread/concurrency/timeout-bounded explicit transcoding |
| `src/telegram_media_bot/infrastructure/ytdlp/native_selection_smoke.py` | Packaged, network-free runtime-image assertion for native MP4/WebM selection, stream-copy arguments, and Best Original policy |
| `src/telegram_media_bot/infrastructure/queue/` | ARQ enqueue plus official abort and transient-key finalization |
| `src/telegram_media_bot/infrastructure/persistence/` | SQLite/WAL jobs, durable-first cancellation, users, daily usage, delivery, block, and recovery store |
| `src/telegram_media_bot/infrastructure/security/telegram_membership.py` | Telegram membership gateway with positive/negative Redis cache |
| `src/telegram_media_bot/infrastructure/security/` | Public URL/DNS validation, Redis rate limiting, and membership cache |
| `src/telegram_media_bot/infrastructure/observability/` | Health HTTP server and Prometheus metrics registry |
| `src/telegram_media_bot/infrastructure/telegram/local_api.py` | Local Bot API lifecycle, durable migration, endpoint leases, and safe status |
| `src/telegram_media_bot/infrastructure/archive/` | Safe 7-Zip multi-volume packaging and SHA-256 manifests |
| `src/telegram_media_bot/telegram/` | Handlers, real-candidate/size UI, middleware, and tracked delivery adapter |
| `src/telegram_media_bot/telegram/bot_factory.py` | Shared Bot/Worker Telegram endpoint and client construction |
| `src/telegram_media_bot/workers/` | ARQ worker settings and job functions |
| `src/telegram_media_bot/bootstrap/` | Config, logging, and composition roots |
| `tests/unit/` | Fast deterministic tests |
| `tests/fixtures/` | Versioned configuration fixtures for backward-compatibility regression tests |
| `tests/integration/` | Local integration tests with fakes/Redis where available |
| `tests/integration/test_local_api_large_upload.py` | Explicit opt-in real Local API upload over 200 MB |
| `tests/contracts/` | Opt-in external yt-dlp smoke tests |
| `docs/tasks/` | Ordered implementation tasks for Codex |
| `plugins/example_extractor/` | Independent external yt-dlp extractor plugin SDK/template |
| `scripts/upgrade_ytdlp.py` | Reviewed engine upgrade, verification, and report workflow |
| `scripts/compare_canary.py` | Baseline/canary failure-rate promotion gate |
| `scripts/generate_file_manifest.py` | Deterministic SHA-256 source-manifest generation |
| `install.sh`, `install.ps1` | Interactive Docker-first one-line installers and global management command setup |
| `scripts/tmb.sh`, `scripts/tmb.ps1` | Cross-platform lifecycle/menu/update/backup command; Linux adds isolated transactional replacement, runtime probes, health verification, rollback, and `tmb` repair |
| `scripts/build_release_archives.sh` | Reproducible tar/ZIP assets and v1.0.2-safe executable Linux updater packaging |
| `scripts/tests/` | Mocked recovery plus opt-in privileged filesystem/SQLite release-upgrade integration tests |
| `.github/workflows/ci.yml` | Quality/security gates, Compose validation, shared-cache Docker build, runtime dependency doctor, native-selector/remux, CLI, and multipart smoke tests |
| `.github/workflows/publish-container.yml` | Tag-only GHCR amd64 publication using the CI cache, runtime doctor/native-selector/remux/CLI/multipart smoke tests, and reproducible release assets |

## Upstream compatibility hot spots

- `infrastructure/ytdlp/engine.py`: `YoutubeDL` lifecycle and calls;
- `infrastructure/ytdlp/options.py`: semantic mode mapping, codec-first native selection, deterministic lower-resolution policy, and bounded complete-stream selection;
- `infrastructure/ytdlp/transcoder.py`: separate native-container and inline-streamability probes,
  quality-first MP4 H.264/AAC and WebM VP9/Opus conversion, and size-limited fallback;
- `infrastructure/ytdlp/mapper.py`: upstream metadata to `MediaInfo`;
- `infrastructure/ytdlp/error_mapper.py`: upstream errors to project exceptions.

## Durable state ownership

- `domain/models.py`: stable job, selected-format/size-confidence, progress, selection, health, and delivery records;
- `application/ports/job_repository.py`: persistence contract;
- `infrastructure/persistence/sqlite_repository.py`: schema and transition implementation;
- `workers/jobs.py`: transitions, retry, delivery progress/logging, per-item receipts, and cleanup;
- `telegram/handlers.py`: owner validation, admin controls, and safe enqueue ordering.
