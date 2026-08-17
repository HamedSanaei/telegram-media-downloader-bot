# Agent context index

Use this table only to route a task. Query Graphify, then verify the resulting working set in the
actual source and tests. Load the linked detailed documentation when the change affects its contract.

| Task | Primary source / symbols | Tests | Authority | Graphify starting point |
|---|---|---|---|---|
| URL validation | `infrastructure/security/url_safety.py`, `PublicUrlValidator` | `test_url_safety.py` | Project spec; ADR-022 | `PublicUrlValidator URL validation DNS` |
| URL canonicalization | `application/services/url_canonicalization.py`, `canonicalize_media_url` | `test_url_canonicalization.py`, YouTube flow integration | ADR-022; architecture URL intent | `canonicalize_media_url callers` |
| Inspection flow | `telegram/handlers.py`, `workers/jobs.py`, `DownloadService.inspect` | handler, worker, download-service tests | Architecture processes | `inspection Telegram worker DownloadService` |
| Media selection | `application/services/native_options.py`, `ytdlp/options.py` | native-options, options, container integration | ADR-019 through ADR-021 | `build_native_option_catalog bounded_format_selector` |
| yt-dlp boundary | `infrastructure/ytdlp/` (`YtDlpEngine`, mapper/options/error mapper) | `tests/unit/infrastructure/ytdlp/`, contracts | T003; ADR-002 | `YtDlpEngine project models callers` |
| gallery-dl boundary | `infrastructure/gallerydl/`, `GalleryDlEngine` | gallery adapter/fixtures/contracts | T013; ADR-027 | `GalleryDlEngine parser runner command builder` |
| Engine routing | `infrastructure/media_engine_router.py`, `RoutedMediaEngine` | gallery adapter/router-facing worker tests | Architecture stable contract; ADR-027 | `RoutedMediaEngine inspect download` |
| Instagram posts | canonicalizer, router, gallery adapter | canonicalization/gallery/worker/delivery tests | T013; ADR-027 | `Instagram post gallery ownership` |
| Instagram Reels | router no-image/video behavior; gallery parser | `instagram-reel-ytdl.json`, gallery tests | ADR-027; status 1.1.1 | `Instagram Reel GalleryDlNoImages yt-dlp` |
| Instagram Stories | canonicalizer, gallery adapter/router, delivery UI | Story fixtures, canonicalization, worker, delivery | Current state; T013 | `Instagram Story canonicalize inspection delivery` |
| Instagram profile/avatar | canonicalizer, gallery command/adapter | avatar fixture and canonicalization/gallery tests | Current state; T013 | `Instagram avatar profile URL flow` |
| Telegram commands | `telegram/handlers.py`, `admin_handlers.py`, `bot_app.py` | Telegram handler/admin tests | Architecture bot/admin sections | `Telegram command submit_url handlers` |
| Telegram callbacks | `telegram/handlers.py`, `ui.py` | navigation/UI/handler tests | ADR-020/021/024 | `callback selection navigation job creation` |
| Delivery/upload | `telegram/delivery.py`, delivery port | delivery tests; large upload opt-in | T008; ADR-006/013 | `TelegramDeliveryGateway delivery receipts upload` |
| Queue | application queue port; `infrastructure/queue/arq_queue.py` | ARQ queue and worker tests | ADR-003/007/017 | `ArqJobQueue enqueue abort finalize` |
| Worker jobs | `workers/jobs.py`, `download_job`, `inspect_job` | `tests/unit/workers/test_jobs.py` | Architecture worker; T006/T009 | `worker job transitions retry cleanup` |
| Redis/ARQ | queue adapter, rate limiter, worker settings | queue/rate-limit/recovery tests | ADR-003/007/017 | `Redis ARQ worker dependencies` |
| Cancellation | handlers, queue adapter, worker, SQLite transitions, subprocesses | navigation/ARQ/worker/SQLite/transcoder/gallery tests | ADR-017; architecture cancellation | `cancellation Telegram ARQ SQLite subprocess` |
| SQLite persistence | persistence ports; `SqliteJobRepository` | SQLite integration and analytics tests | T009; ADR-007/008/017/023 | `SqliteJobRepository reverse dependencies` |
| Local Bot API | `infrastructure/telegram/local_api.py`, `telegram/bot_factory.py`, CLI | local API unit/integration tests | ADR-012/015; architecture control plane | `LocalBotApiManager endpoint lease bot factory` |
| Local API readiness | `telegram/bot_factory.py`, `wait_for_local_api_readiness` | `test_bot_factory.py`, privileged readiness shell test | Current state; operations docs | `local_api_startup_wait callers timeout` |
| Configuration/bootstrap | `bootstrap/config.py`, bot/worker composition, CLI | bootstrap/config and CLI tests | ADR-004/028; config example | `Settings composition cookie paths` |
| Installer | `install.sh`, `install.ps1` | updater shell/PowerShell suites | Installation/operations docs; ADR-015 | `installer release archive config` |
| Updater / upgrade | `scripts/tmb.sh`, `tmb.ps1`, upgrade tests | mocked and privileged updater suites | T011; ADR-018 | `perform_update rollback_update verification` |
| Rollback | updater transaction functions and fixtures | failure-stage and service-state tests | ADR-018; operations docs | `rollback_update service state application files` |
| Release | version files, archive builder, publish workflow | architecture/version assertions, archive smokes | CODEX execution; operations | `release version archive publish workflow` |
| CI | `.github/workflows/ci.yml`, quality scripts | scripts' own checks plus full pytest | AGENTS testing gates | `CI architecture manifest Docker gates` |
| Reporting/admin | `telegram/admin_handlers.py`, analytics services/adapters | admin, chart, usage integration tests | ADR-024/025/028 | `admin reporting cookie management authorization` |

Fallback when Graphify is unavailable:

```bash
python scripts/agent_context.py symbol SYMBOL
python scripts/agent_context.py refs SYMBOL
python scripts/agent_context.py tests PATH_OR_SYMBOL
```
