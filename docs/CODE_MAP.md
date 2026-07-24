# Code map

| Path | Responsibility |
|---|---|
| `src/telegram_media_bot/domain/` | Stable models, enums, identifiers, and exceptions |
| `src/telegram_media_bot/application/ports/` | Interfaces required by use cases |
| `src/telegram_media_bot/application/services/` | Orchestrates inspection, policy limits, and selected downloads |
| `src/telegram_media_bot/application/services/job_service.py` | Durable job creation and active-job idempotency |
| `src/telegram_media_bot/application/services/progress.py` | Framework-free progress throttling policy |
| `src/telegram_media_bot/application/services/access_policy.py` | Static/dynamic user access and rate policy |
| `src/telegram_media_bot/infrastructure/ytdlp/` | The only direct yt-dlp integration |
| `src/telegram_media_bot/infrastructure/queue/` | ARQ queue client implementation |
| `src/telegram_media_bot/infrastructure/persistence/` | SQLite/WAL job, selection, block, and recovery store |
| `src/telegram_media_bot/infrastructure/security/` | Public URL/DNS validation and Redis rate limiting |
| `src/telegram_media_bot/infrastructure/observability/` | Health HTTP server and Prometheus metrics registry |
| `src/telegram_media_bot/infrastructure/telegram/local_api.py` | Local Bot API lifecycle, durable migration, endpoint leases, and safe status |
| `src/telegram_media_bot/telegram/` | Handlers, semantic UI, correlation middleware, and bounded-time delivery adapter |
| `src/telegram_media_bot/telegram/bot_factory.py` | Shared Bot/Worker Telegram endpoint and client construction |
| `src/telegram_media_bot/workers/` | ARQ worker settings and job functions |
| `src/telegram_media_bot/bootstrap/` | Config, logging, and composition roots |
| `tests/unit/` | Fast deterministic tests |
| `tests/integration/` | Local integration tests with fakes/Redis where available |
| `tests/integration/test_local_api_large_upload.py` | Explicit opt-in real Local API upload over 200 MB |
| `tests/contracts/` | Opt-in external yt-dlp smoke tests |
| `docs/tasks/` | Ordered implementation tasks for Codex |
| `plugins/example_extractor/` | Independent external yt-dlp extractor plugin SDK/template |
| `scripts/upgrade_ytdlp.py` | Reviewed engine upgrade, verification, and report workflow |
| `scripts/compare_canary.py` | Baseline/canary failure-rate promotion gate |
| `scripts/generate_file_manifest.py` | Deterministic SHA-256 source-manifest generation |

## Upstream compatibility hot spots

- `infrastructure/ytdlp/engine.py`: `YoutubeDL` lifecycle and calls;
- `infrastructure/ytdlp/options.py`: semantic mode mapping and bounded complete-stream selection;
- `infrastructure/ytdlp/transcoder.py`: cancellable size-bounded H.264/AAC video transcoding;
- `infrastructure/ytdlp/mapper.py`: upstream metadata to `MediaInfo`;
- `infrastructure/ytdlp/error_mapper.py`: upstream errors to project exceptions.

## Durable state ownership

- `domain/models.py`: stable job, progress, selection, health, and delivery records;
- `application/ports/job_repository.py`: persistence contract;
- `infrastructure/persistence/sqlite_repository.py`: schema and transition implementation;
- `workers/jobs.py`: transitions, cancellation, retry, delivery, and cleanup;
- `telegram/handlers.py`: owner validation, admin controls, and safe enqueue ordering.
