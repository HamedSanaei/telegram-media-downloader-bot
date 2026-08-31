# Code map

| Path | Responsibility |
|---|---|
| `src/telegram_media_bot/domain/` | Stable models, enums, identifiers, exceptions, and `best_original` native-only normalization |
| `src/telegram_media_bot/domain/failures.py` | Typed structured `FailureContext` (adapter/extractor/source/fallback/HTTP status/retry history/stage/etc.) that survives to the terminal admin notification; bounded `FailureStage`; size-bounded Persian rendering that omits absent fields |
| `src/telegram_media_bot/domain/cookie_health.py` | Cookie Health Center states, network-free static snapshots, backward-compatible persisted passive runtime-auth evidence, and definitive blocking states |
| `src/telegram_media_bot/domain/cookies.py` | The single cookie-provider registry (YouTube/Instagram/TikTok/X/Pinterest/SoundCloud domains and labels), upload size contract, and merge summary |
| `src/telegram_media_bot/application/ports/` | Interfaces required by use cases |
| `src/telegram_media_bot/application/ports/cookie_management.py` | Framework-free canonical cookie merge/export contract |
| `src/telegram_media_bot/application/services/` | Orchestrates inspection, policy limits, and selected downloads |
| `src/telegram_media_bot/application/services/diagnostic_sanitizer.py` | Central secret sanitizer: URL reduction to scheme+hostname+safe path, safe-parameter allowlist, redaction of cookies/tokens/headers/proxy passwords/CDN query secrets, bounded exception-message cleanup |
| `src/telegram_media_bot/application/services/cookie_health_service.py` | Passive Cookie Health orchestration: provider-scoped or complete local static refresh, persisted transition/reminder deduplication, and same-request runtime auth-failure updates; it has no provider client |
| `src/telegram_media_bot/application/services/native_options.py` | Builds the public native-only option catalog, enforces codec/transcode invariants, chooses truthful representatives, deduplicates actual plans, and creates opaque option IDs |
| `src/telegram_media_bot/application/services/job_service.py` | Durable job creation and active-job idempotency |
| `src/telegram_media_bot/application/services/durable_update_inbox.py` | Durable inbound-update state transitions, bounded handler retry, and explicit non-replayable serialization quarantine semantics |
| `src/telegram_media_bot/application/ports/inbound_update_repository.py` | Framework-free replayable-update and terminal-tombstone persistence contract |
| `src/telegram_media_bot/application/services/instagram_delivery.py` | Selects the complete Instagram image/mixed bundle behind the Photo/File confirmation |
| `src/telegram_media_bot/application/services/url_canonicalization.py` | Parses YouTube URL intent, removes Mix context from single videos, gives equivalent X/Twitter status-share URLs one query-free identity, and canonicalizes Instagram share/story/profile URLs (tracking stripped, plain profiles rewritten to the `/USERNAME/avatar/` avatar target) |
| `src/telegram_media_bot/application/services/usage_analytics.py` | Builds Tehran-local usage reports and excludes configured administrators from public KPI aggregation |
| `src/telegram_media_bot/application/ports/usage_analytics.py` | Framework-free usage activity and PNG renderer contracts |
| `src/telegram_media_bot/application/services/progress.py` | Framework-free download/delivery progress throttling |
| `src/telegram_media_bot/application/services/access_policy.py` | Static/dynamic access, required-channel membership, and rate policy |
| `src/telegram_media_bot/application/ports/membership.py` | Framework-free required-channel membership contract |
| `src/telegram_media_bot/domain/subscriptions.py` | T014 VIP/subscription domain: plans, `Capability`, contract and validation, immutable `EntitlementGrant`, monotonic grant windows with UTC calendar-month arithmetic, `EntitlementSnapshot`, and JSON-safe snapshot serialization |
| `src/telegram_media_bot/application/services/entitlements.py` | `EntitlementService` (fail-closed `authorize`, grant activation, reversal recomputation, deterministic derived status); Telegram `is_premium` is never consulted |
| `src/telegram_media_bot/application/ports/subscriptions.py` | Provider-neutral plan-catalog and subscription/grant persistence contracts |
| `src/telegram_media_bot/infrastructure/persistence/sqlite_subscriptions.py` | WAL-backed plan/subscription/grant tables, exactly-once (user, source) unique grant index, additive idempotent schema |
| `src/telegram_media_bot/application/ports/user_repository.py` | Durable profile and usage-accounting contract |
| `src/telegram_media_bot/infrastructure/ytdlp/` | The only direct yt-dlp integration, including strict raw-entry Instagram mixed-carousel video resolution, zero-transcode AV1/H.264 MP4 and VP9 WebM selection, narrow Twitter HLS audio-metadata inference, native/inline compatibility probing, bounded explicit transcoding, private per-inspection scratch workspaces under the configured storage temp root (read-only-app-filesystem safe), and a network-free read-only-container inspection smoke |
| `src/telegram_media_bot/infrastructure/gallerydl/` | Isolated gallery-dl 1.32.8 argv/subprocess, explicit JSON Lines event contract, bounded output/cancellation, strict non-empty vendor tuple parsing/error mapping, successful-empty unavailable classification, stable asset normalization, and safe original-image download with Instagram videos disabled when required. The typed result model carries IMAGE/VIDEO/mixed collections (Stories, Reels, video posts, avatar) without conflating "no images" with "no media" |
| `src/telegram_media_bot/infrastructure/media_engine_router.py` | Inspection-result routing and fail-closed mixed Instagram merge: validate/download canonical yt-dlp video children before gallery-dl images, then merge exact source ordinals |
| `src/telegram_media_bot/infrastructure/image_validation.py` | Pillow signature/format/dimension/decompression-bomb validation without altering originals |
| `src/telegram_media_bot/infrastructure/ytdlp/native_selection_smoke.py` | Packaged, network-free runtime-image assertion for AV1/H.264 MP4 and VP9 WebM selection, stream-copy arguments, and Best Original policy |
| `src/telegram_media_bot/infrastructure/queue/` | ARQ enqueue plus official abort and transient-key finalization |
| `src/telegram_media_bot/infrastructure/persistence/` | SQLite/WAL jobs, durable-first cancellation, users, daily usage, delivery, block, and recovery store |
| `src/telegram_media_bot/infrastructure/persistence/sqlite_inbound_updates.py` | WAL-backed inbound Telegram payload journal, ordered replay queries, terminal serialization tombstones, and retention |
| `src/telegram_media_bot/infrastructure/persistence/sqlite_usage_analytics.py` | Read-only mapping of durable SQLite jobs/events into project-owned usage activity |
| `src/telegram_media_bot/infrastructure/analytics/` | Pillow usage dashboards, bundled-font doctor, and deterministic Docker smoke fixtures |
| `src/telegram_media_bot/assets/fonts/` | Package-bundled Noto Sans runtime font and SIL OFL 1.1 license |
| `src/telegram_media_bot/infrastructure/security/telegram_membership.py` | Telegram membership gateway with positive/negative Redis cache |
| `src/telegram_media_bot/infrastructure/security/` | Public URL/DNS validation, Redis rate limiting, and membership cache |
| `src/telegram_media_bot/infrastructure/cookies/` | Strict Netscape parsing, supported-service detection, deterministic scoped merge, restricted backup, atomic canonical-file replacement, and network-free static health checks; no provider probe adapter exists |
| `src/telegram_media_bot/infrastructure/persistence/sqlite_cookie_health.py` | Durable Cookie Health state (status/static/active/last-notified/reminder) surviving restarts |
| `src/telegram_media_bot/infrastructure/observability/` | Health HTTP server and Prometheus metrics registry |
| `src/telegram_media_bot/infrastructure/telegram/local_api.py` | Local Bot API lifecycle, durable migration, endpoint leases, and safe status |
| `src/telegram_media_bot/infrastructure/archive/` | Safe 7-Zip multi-volume packaging, deterministic ordered image ZIPs, and SHA-256 manifests |
| `src/telegram_media_bot/infrastructure/storage/` | Exact job-workspace cleanup, symlink-safe deletion, and startup/maintenance sweeping |
| `src/telegram_media_bot/telegram/` | Versioned Back/Native/Instagram/Story/Highlight delivery callbacks, real-plan rendering, middleware, tracked exact-byte document delivery, ordered ten-item media-group planning, and centralized bottom-most durable source-link caption/fallback placement |
| `src/telegram_media_bot/telegram/delivery.py` | Telegram method routing, albums/individual artifacts/batches/multipart delivery, receipt-first uncertainty handling, and 1024-safe caption composition that preserves fixed text and complete canonical source URLs |
| `src/telegram_media_bot/telegram/durable_polling.py` | Inbound-safe aiogram serialization without outbound Bot defaults, durable-prefix offset advancement, hard serialization-gap ordering barrier, and sequential dispatch/replay |
| `src/telegram_media_bot/telegram/admin_menu.py` | Central administrator button constants (reports, cookie management, Cookie Health), FSM state, and reply/inline keyboard builders; `admin_handlers.py` verifies cookie writes, refreshes provider health immediately, and makes unchanged status edits idempotent |
| `src/telegram_media_bot/telegram/admin_handlers.py` | Role-checked menu/download/report/cookie routing, private-chat secret export, bounded in-memory document intake, and per-admin report single-flight coordination |
| `src/telegram_media_bot/telegram/handlers.py` | Shared URL submission, editable job-status ownership, active-job queue reconciliation, callbacks, and cancellation routing |
| `src/telegram_media_bot/telegram/bot_factory.py` | Shared Bot/Worker Telegram endpoint and client construction plus the bounded, cancellable Local Bot API startup readiness wait (`local_api_startup_wait`/`ready`/`timeout`) |
| `src/telegram_media_bot/workers/` | ARQ worker settings and job functions, including explicit durable `JobRecord.url` delivery metadata, edit-or-send inspection publication, redacted terminal-failure alerts, passive same-request cookie-auth feedback, retries, receipts, and workspace cleanup; no Cookie Health cron is registered |
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
| `release-policy.json`, `scripts/check_release_policy.py` | Canonical withdrawn-release denylist and fail-closed build/publication check; v-prefixed and package versions normalize to the same target |
| `install.sh`, `install.ps1` | Interactive Docker-first one-line installers, embedded standalone withdrawal-policy snapshots, verified candidate-version rejection before installation/configuration/image/service actions, and global management command setup |
| `scripts/tmb.sh`, `scripts/tmb.ps1` | Cross-platform lifecycle/menu/update/backup/cleanup command with requested/candidate withdrawal guards before state changes; Linux adds prepared-image/read-only-data static preflight, pre-downtime image pulls, exact writer/service-state tracking, private atomic offline backups, isolated transactional replacement, offline post-install plus conditional post-start online verification, runtime probes, SIGINT recovery, rollback, `tmb` repair, and guarded project-image reclamation |
| `scripts/build_release_archives.sh` | Policy-gated reproducible tar/ZIP assets, checksummed standalone updater bootstrap, v1.0.2-safe executable Linux updater packaging, and an explicit deterministic epoch for current-tree integration fixtures |
| `scripts/tests/` | Mocked withdrawal/recovery plus opt-in privileged release-upgrade tests for direct/candidate version denial, allowed forward recovery, phased offline/online verification, exact mixed service restoration, active Local API logs, archive inclusion/exclusion, redacted failures, SIGINT, runtime identity, filesystem/SQLite permissions, Compose bind contracts, and the delayed Local API startup readiness regression (`test_local_api_readiness.sh`) |
| `scripts/ci_change_policy.py` | Deterministic, GitHub-Actions-independent CI change classifier: event-aware PR/push changed-range handling, category union (docs/python/deps/package/plugin/docker/updater/linux/windows/policy/workflow), and fail-conservative fallback that requests every heavy lane rather than skipping |
| `scripts/ci_fast_quality.sh`, `scripts/ci_docs_quality.sh` | Fast-quality lane runners: full non-contract integrity/lint/type/secret suite for ordinary changes; a docs-only minimum integrity set for conclusively documentation-only changes |
| `.github/workflows/ci.yml` | Tiered fast-feedback CI (T033): stable `change-detection`, always-reporting `quality`, conditional heavy lanes (dependency/package/plugin-sdk/docker-runtime/updater-integration/installer-linux/installer-windows), and an always-evaluated `final-ci-gate` that understands success/failure/cancelled/skipped; safe same-ref development concurrency; no workflow-level `paths:` filters. `final-ci-gate` is the aggregate merge-safety check required by branch protection (`quality` optional for visibility; `quality` + `change-detection` alone is not a sufficient gate) |
| `.github/workflows/publish-container.yml` | Tag-only GHCR amd64 publication using the CI cache, runtime doctor/native-selector/remux/CLI/multipart smoke tests, and reproducible release assets |

## Upstream compatibility hot spots

- `infrastructure/ytdlp/engine.py`: `YoutubeDL` lifecycle and calls;
- `infrastructure/ytdlp/options.py`: semantic mode mapping, single-video `noplaylist` defense, codec-family native selection, deterministic lower-resolution policy, and bounded complete-stream selection;
- `infrastructure/ytdlp/transcoder.py`: separate native-container and inline-streamability probes,
  quality-first MP4 H.264/AAC and WebM VP9/Opus conversion, and size-limited fallback;
- `infrastructure/ytdlp/mapper.py`: upstream metadata to `MediaInfo`;
- `infrastructure/ytdlp/error_mapper.py`: upstream errors to project exceptions; local filesystem
  failures (EROFS/EACCES/ENOSPC and related errnos) map to terminal `LocalRuntimeError`
  (`local_runtime`), never to remote download failures.

## Durable state ownership

- `domain/models.py`: stable job, normalized selected-stream/native-option view, explicit image delivery mode, source ordinals, progress, selection, health, and delivery records;
- `application/ports/job_repository.py`: persistence contract;
- `infrastructure/persistence/sqlite_repository.py`: schema and transition implementation;
- `workers/jobs.py`: transitions, retry, delivery progress/logging, per-item receipts, redacted
  terminal-failure administrator alerts, and cleanup;
- `telegram/handlers.py`: owner validation, admin controls, and safe enqueue ordering.

## Milestone 4 ownership (T014/T015/T016/T017/T018 implemented; the rest still planned)

T014's entitlement foundation is implemented (`domain/subscriptions.py`,
`application/services/entitlements.py`, `application/ports/subscriptions.py`,
`infrastructure/persistence/sqlite_subscriptions.py`, and the nullable `entitlement_snapshot` on
`JobRecord`). T015's provider-neutral billing foundation is also implemented
(`domain/payments.py`, `application/ports/payments.py`, `application/services/billing.py`,
`infrastructure/persistence/sqlite_payments.py`, and the additive `payment_orders` /
`payment_attempts` / unique `provider_transaction_claims` tables). T016's optional disabled web
companion is implemented (`domain/web_companion.py`, `application/ports/companion.py`,
`application/services/handoff.py`, `infrastructure/security/handoff.py`,
`infrastructure/persistence/sqlite_handoff.py`, `infrastructure/web_companion/app.py`, and
`bootstrap/companion.py`). The remaining areas (Instagram credentials/Vault, account linking, VIP
Telegram UX, credential-dependent routing, and the first real payment gateway) remain planned for
T017-T025 and do not exist yet.

| Area | Ownership |
|---|---|
| `domain/payments.py` | Provider-neutral orders, attempts, statuses, provider identifiers, transaction references, and immutable amount/plan snapshots (implemented) |
| `domain/instagram_credentials.py` | Credential state, monotonic generation, versioned envelope, sanitized events/leases, associated-data binding, and typed lifecycle semantics (implemented) |
| `application/ports/payments.py` | Payment repository and project-owned payment-gateway requests/results (implemented) |
| `domain/web_companion.py` | Purpose-bound handoff claims, verification outcomes, browser/CSRF tokens, bounded flow state, Instagram-connect and payment views (implemented) |
| `application/ports/companion.py` | Companion handoff signer/verifier/nonce-repository, provider-callback registry, Instagram flow, and payment processor contracts (implemented) |
| `application/services/handoff.py` | Bot link minting and companion exactly-once handoff exchange (implemented) |
| `infrastructure/security/handoff.py` | Ed25519 signer/verifier over `cryptography` (implemented) |
| `infrastructure/web_companion/app.py` | Separate `aiohttp.web` Instagram-browser and payment-callback routes with CSRF/cookies/headers/limits/trusted-proxy isolation (implemented) |
| `bootstrap/companion.py` | Reduced least-privilege `CompanionSettings` (no bot token/signer) and deterministic `build_companion_app` (implemented) |
| `application/ports/instagram_credentials.py` | Owner-bound encrypted-credential repository, key store/cryptor, lease, and ephemeral materialization contracts (implemented) |
| `application/services/billing.py` | Order lifecycle, verified-result confirmation, idempotent atomic refund/reversal, and reconciliation queries without provider-name branching (implemented) |
| `application/services/instagram_connection.py` | Mints handoff links, runs the transient login, stores success in the vault, and exposes sanitized status/disconnect (implemented) |
| `domain/credential_resolution.py` | Typed per-attempt credential kinds/policies, content scope, failure categories, resolved handles, and operator-record verifier (implemented T019) |
| `application/services/credential_resolution.py` | Owner/generation/state-checked user materialization and explicit operator/none context resolution (implemented T019) |
| `application/services/operator_attestation.py` | Explicit verified zero-follow operator attestation and stale-record detection (implemented T019) |
| `application/services/credential_vault.py` | `CredentialVault` connect/re-connect (generation++), expiry/challenge markers, disconnect/revoke erase, and admin key-rotation re-encryption (implemented) |
| `infrastructure/persistence/` additions | Additive SQLite repositories for payments (`sqlite_payments.py`), single-use handoff nonces (`sqlite_handoff.py`), encrypted credentials/events/leases (`sqlite_instagram_credentials.py`), and operator attestations (`sqlite_operator_attestation.py`), all implemented |
| `infrastructure/credentials/` | AES-256-GCM envelope/key-ring adapter, `CredentialCryptor`, and job-scoped restrictive Netscape materializer (implemented) |
| `infrastructure/payment/<provider>/` | First provider adapter selected by composition; intentionally blocked in T024 until a provider is chosen |
| companion web package/process | Separate least-privilege `aiohttp.web` account-link and payment-callback boundary without the Telegram bot token (implemented, disabled by default) |
| `infrastructure/ytdlp/`, `infrastructure/gallerydl/`, and media router changes | Consume explicit ephemeral credential context while remaining unaware of VIP/subscription policy (implemented T019) |
| `telegram/` additions | `/vip`, entitlement-gating, expiry/renewal, audited administrator UX (T023); `/instagram`, `instagram_ux.py`, Persian connection prompts/status/disconnect (implemented) |
| `workers/` and queue/persistence changes | Carry the explicit operator credential seam while preserving legacy jobs; future safe user snapshots/fallback state remain T020-T022 |

## Milestone 5 ownership (T026-T032 implemented)

The following runtime ownership implements the completed logger milestone.

| Area | Ownership |
|---|---|
| `domain/audit.py` | Typed `AuditEvent`, categories, severity, correlation metadata, source-message references, probe outcomes, and redaction-safe payloads (implemented T026/T028/T030) |
| `application/ports/audit.py` | Audit sink, logger destination management, durable outbox, lease, delivery-effect, and destination-verifier contracts (implemented T026-T028) |
| `application/services/audit_service.py`, `audit_sanitizer.py`, `audit_outbox.py` | Event eligibility, centralized fail-closed sanitization, and transport-neutral outbox processing (implemented T026/T027) |
| `application/services/audit_destination_admin.py` | Role-authorized destination management: strict channel-ID validation, probe-driven health, enable/disable, config-protected removal (implemented T028) |
| `infrastructure/persistence/sqlite_audit.py` | Additive SQLite/WAL logger destinations, outbox, health, probe records, leases, and uncertain-delivery records (implemented T027/T028) |
| `infrastructure/telegram/audit_destination_verifier.py` | Typed channel probe: existence, type, bot membership, posting test, sanitized outcome mapping (implemented T028) |
| `application/services/submission_audit.py`, `telegram/handlers.py` | Durable-acceptance-only `USER_SUBMISSION` emission, replay-stable source identity, bounded album aggregation, and logger-failure isolation (implemented T030) |
| `application/services/logger_privacy.py`, `infrastructure/persistence/sqlite_audit.py` | Exact Persian notice, versioned durable per-user acknowledgement, operator-attestation activation gate, and indefinite-retention boundary (implemented T031) |
| `infrastructure/telegram/audit_delivery.py`, `infrastructure/persistence/sqlite_audit.py` | Native single/group copy transport, typed failure outcomes, per-destination outbox, and restart-safe album source merging (implemented T027/T030) |
| `telegram/admin_menu.py` / `telegram/admin_handlers.py` | Logger-channel management flow, numeric channel validation, test, enable/disable/remove, and health UI (implemented T028) |
| `workers/settings.py`, `workers/jobs.py` | Independent alert admission, bounded 30-second/20-item outbox dispatch, aggregate health/metrics, and restart-safe native Telegram delivery (implemented T029/T032) |
| `docs/` | Completed T026-T032, accepted ADR-036-038, privacy/retention policy, staged rollout, incident, backup, and rollback runbooks |

## Milestone 6 ownership (implemented)

T033's tiered CI entitlement foundation is implemented (see `scripts/ci_change_policy.py`,
`scripts/ci_fast_quality.sh`, `scripts/ci_docs_quality.sh`, and the tiered `.github/workflows/ci.yml`
above). The deployment workflow remains unchanged. The following measurement/reporting surface is
left as an explicitly deferred, non-functional follow-up; no secrets or production state are involved.

| Remaining (deferred) | Planned ownership |
|---|---|
| CI measurement/reporting | Bounded workflow/job/step duration, cold/warm cache, cancellation, fallback, and lane-outcome measurements without secrets or production state |
