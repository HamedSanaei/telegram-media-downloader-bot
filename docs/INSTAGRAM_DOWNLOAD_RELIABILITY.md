# Instagram download reliability investigation

## Scope and production evidence

Investigated local `1.4.0-rc.14` and production job `2AMIcSr54b9YMwMI_SNLLJ7M` for
`https://www.instagram.com/stories/anitasarhadi/4011482396666383837/`.
The initial local working tree was clean. No commit, push, publication, deployment, production
cookie change, service restart, or unrelated service modification was performed.

SSH diagnostics on `Nethederland` were read-only: bounded Compose logs, service state and installed
package/file-access metadata. Production reported application `1.4.0rc14`, gallery-dl `1.32.8`, a
running worker, and an existing readable canonical cookie. Cookie contents were neither printed nor
exported. No additional Instagram request or production media download was made: the known failure
was not replayed against an ephemeral Story or the shared session.

The job-specific structured logs establish:

| UTC, 2026-10-20 | Observation |
|---|---|
| 10:48:55.278 | Download attempt 1 starts |
| 10:49:07.054 | gallery-dl inspection succeeds: one Instagram video, 11.702 seconds |
| 10:49:23.853 | Redundant gallery-dl inspection succeeds: one video, 16.789 seconds |
| 10:49:40.774 | Download extraction fails with `GalleryDlExtractionError`, marked retryable |
| 10:49:56.268 | Attempt 2 starts |
| 10:50:04.155 | gallery-dl exits 0 with zero events; `GalleryDlEmptyResponseError` |
| 10:50:05.863 | Terminal gallery-extraction failure; workspace cleanup follows |

## Confirmed causes and remaining uncertainty

1. **Redundant provider requests:** `DownloadService.download` inspected, then
   `RoutedMediaEngine.download` inspected again before the actual download extraction. Direct
   gallery downloads also had an inspection entry point. The logs confirm duplicated extraction
   in this job, increasing request count and the interval in which media/session state could change.
2. **Unsafe shared-cookie write mechanisms:** gallery-dl commands did not disable its default
   `extractor.cookies-update`; direct yt-dlp contexts also save their cookie jar on close. These
   mechanisms can overwrite shared credentials or race with another job/administrator replacement.
   A local real-runtime smoke reproduced upstream yt-dlp modifying a synthetic source file and
   demonstrated the protected adapter preserving it across four concurrent contexts. This proves
   the mechanism, not that production cookie bytes were corrupted during this historical job.
3. **Retry classification and amplification:** generic gallery extraction and empty-response errors
   inherited retryability from `DownloadFailedError`. The worker used a fixed delay, while vendor
   request/download retries could multiply traffic inside each worker attempt. Neither a generic
   extractor failure nor an empty successful process establishes a transient condition.
4. **Outdated Instagram extractor:** installed `1.32.8` predates relevant upstream fixes. Stable,
   non-yanked `1.32.17` supports Python >=3.8 and is compatible with this project's Python >=3.14.
   Its release notes fix GraphQL query/document-ID lookup and ensure nonzero exit status on GraphQL
   errors; `1.32.14` fixes Story/Highlight extraction and `1.32.16` fixes Highlight/user lookups.
5. **Leased user-cookie deletion during output reset:** the worker materialized `.credentials-*`
   beneath the same directory that gallery/yt-dlp clear before transfer. A valid leased cookie could
   therefore survive inspection but be deleted immediately before download. The worker now gives
   adapters `job-root/media` while credential leases live in a sibling under `job-root`. Terminal
   cleanup still owns the complete job root. This source-confirmed lifecycle defect is not proof
   that the historical job used a personal session.

**Not established:** the historical provider HTTP response, a specific GraphQL response, rate
limiting, expired/deleted Story, invalid session, or actual cookie corruption. Existing production
logs lack definitive evidence for those conclusions. A readable cookie is not proof of valid
upstream authentication; empty output is not proof of invalid authentication. The fix addresses
confirmed architectural defects and known extractor incompatibilities without claiming live recovery
of that particular Story.

Upstream references:

- [PyPI stable package metadata](https://pypi.org/project/gallery-dl/1.32.17/)
- [Pinned upstream changelog](https://github.com/mikf/gallery-dl/blob/v1.32.17/CHANGELOG.md)
- [Cookie-update configuration](https://gdl-org.github.io/docs/configuration.html#extractor-cookies-update)

## Implementation

### Dependency and parser boundary

`pyproject.toml` and `uv.lock` pin gallery-dl `1.32.17`. Adapter health, fixture/version checks,
packaged third-party notices and image CI/publication smoke checks use the same version. The
application version remains unchanged; this is an unreleased local fix, not a new release.
Vendor JSON Lines remain adapter-local. Normalized assets contain stable identities, not signed
CDN URLs or raw dictionaries.

### Inspection reuse and authorization

`MediaInspection` is transient and bound to the resolved credential handle, exact canonical URL,
explicit cookie override and collection bound. `DownloadService` validates once, carries that
inspection in `DownloadRequest`, and the router/gallery consume only matching metadata. Changing
URL, credential kind, generation, materialized lease path, cookie override or collection bound
invalidates reuse. Collection limits apply during the first extraction.

Queued jobs intentionally do **not** reuse durable selection/UI metadata. Those records expose
owner/freshness information, but download jobs do not carry a source-selection link or proof of the
actual extraction credential/operator generation after fallback. Freshness alone cannot establish
safe reuse. Each queued attempt rechecks its durable owner/request/private authorization, resolves
current credentials and inspects once inside that operation. This is a conservative rejection of
unprovable reuse, not a new persistent cache or schema migration. Stale/foreign UI selections cannot
bypass fresh validation; revoked credentials fail before extraction. Actual download still resolves
current provider media URLs and fails closed on missing/mismatched output.

### Read-only credentials

All gallery commands, including unrestricted tray/probe builders, disable cookie updates. Extractor
and downloader retry loops are disabled; nested yt-dlp receives in-memory cookies, no cookie-file
persistence and no independent retry loops. The direct yt-dlp runtime overrides cookie saving while
preserving its per-instance in-memory jar. Concurrent media jobs are readers, not competing shared
credential writers. The administrator atomic cookie manager remains the intentional writer.

User sessions retain encrypted owner-bound storage, exact generation checks, exclusive leases,
restricted job-local materialization and cleanup on success, error and cancellation. Credentials
are siblings of the media output directory, so resetting media output cannot unlink an active
credential lease. Terminal cleanup removes the complete job root. No new secret storage, log payload,
fallback credential, private-content bypass or auth policy was introduced.

### Error diagnostics and retry policy

Structured diagnostics retain adapter/version/provider, operation stage, process exit code,
fixed diagnostic category, observed HTTP status, retryability, optional observable Retry-After and
elapsed time. Raw stderr/response bodies, URLs, cookie paths/values and session identifiers are not
logged. Unknown evidence stays unknown.

- Explicit session rejection, login/challenge or HTTP 401 retain authentication handling.
- Bare HTTP 403 is access denial, not proof of a dead personal session; it does not invalidate that
  session or switch credentials.
- HTTP 429/rate-limit evidence is retryable; 5xx/network/timeouts are transient.
- Explicit unavailable/deleted/not-found media is terminal; malformed schema/extractor errors are
  terminal incompatibility; unqualified GraphQL errors and ambiguous empty output are terminal
  extraction failures, not fabricated auth or availability conclusions.
- Existing public personal-session fallback remains bounded and typed-auth-only; private jobs never
  fall back to shared credentials.

The worker owns gallery retries. For attempt `n`, the exponential ceiling is
`min(900, queue.retry_delay_seconds * 2**(n-1))` seconds, with equal jitter in the upper half of that
interval. An observable provider cooldown is a minimum delay. A cooldown beyond 900 seconds ends
with the original provider cause rather than issuing an early request. `queue.max_tries` still
bounds attempts; cancellation, delivery uncertainty and durable receipt behavior are unchanged.
Upstream may not print Retry-After; the adapter never invents an unseen header.

## Risks and intentional behavior changes

- Ambiguous extraction failures now terminate instead of blindly retrying. This reduces traffic but
  may require a later explicit user retry after an unclassified transient provider problem.
- Bare 403 no longer expires a personal session. Explicit login/session evidence still does.
- Provider-updated cookies remain in memory and are not written back. Operators/users must refresh
  source sessions through the existing intentional management/login flows when necessary.
- One fresh inspection per queued attempt remains necessary until durable credential provenance is
  designed; this change does not claim zero provider extraction requests during actual downloads.
- Fixture verification cannot guarantee present-day Instagram availability or private-account access.
- No configuration or database migration is required; existing queued jobs and secrets remain local.

## Production rollout: instructions only

Do not run these commands until deployment is separately authorized. This local fix has no published
release tag. First assign an approved new release version, run the release gates, review/publish its
immutable image and checksummed source archive under explicit authorization. Do not deploy it under
the existing `1.4.0-rc.14` tag and do not run an ad-hoc `pip install` in the production container.

On the production host, using the actual approved published tag:

```bash
ssh Nethederland
cd /opt/telegram-media-downloader-bot
read -r -p 'Approved published fix tag: ' FIX_RELEASE_TAG
test -n "$FIX_RELEASE_TAG"
docker compose -f docker-compose.companion.yml stop companion
tmb backup create
TMB_RELEASE_TAG="$FIX_RELEASE_TAG" tmb update
docker compose -f docker-compose.companion.yml up -d --force-recreate companion
tmb status
tmb doctor --offline
docker compose exec -T worker python -m gallery_dl --config-ignore --version
curl --fail http://127.0.0.1:8090/ready
```

The transactional updater performs preflight/image pull before downtime, stops only its project
writers, backs up SQLite/WAL/config/cookies privately, preserves Redis and restores the prior service
set on failure. Stop the separate companion writer before management and recreate it with the
matching image afterward; preserve its prior stopped/running state if it was not enabled. See
[the companion runbook](INSTAGRAM_CONNECTION.md#backup-update-restore-and-rollback).

Expect gallery-dl `1.32.17`. With an authorized owner, request one currently accessible Story in the
bot, then one small public post/carousel if needed. Observe one metadata inspection per download
attempt, no source-cookie writes, correct ordered delivery and sanitized terminal diagnostics.
Do not repeatedly replay the historical Story, issue credential probes or bulk-download a tray.
Inspect private logs locally without pasting credential-bearing payloads. Preserve the original
backup and old immutable image until the canary succeeds.

### Rollback

No schema migration is involved. Stop the companion before the normal transactional rollback:

```bash
cd /opt/telegram-media-downloader-bot
docker compose -f docker-compose.companion.yml stop companion
TMB_RELEASE_TAG=v1.4.0-rc.14 tmb update
docker compose -f docker-compose.companion.yml up -d --force-recreate companion
tmb status
tmb doctor --offline
```

Restore the exact prior companion service state and image selection. Keep config/vault keys/cookies
from the managed backup; do not rotate credentials or restore unrelated services. Do not replay
`delivery_uncertain` jobs. Rolling back also restores the known old extractor/redundant-request/
cookie-write risks, so it is an operational recovery option, not a reliability fix.

## Graphify usage

- Version: `graphify 0.9.26`.
- Freshness: `graphify check-update .` exited 0 with no output or stale indication.
- Exact bounded query:
  `graphify query "Trace Instagram inspection credentials GalleryDlEngine RoutedMediaEngine DownloadService retries and tests" --budget 1200 --graph graphify-out/graph.json`.
- Discovered `GalleryDlEngine`, `RoutedMediaEngine`, `DownloadService`, `MediaInfo`, `DownloadRequest`,
  worker jobs and credential orchestration; verified behavior in source/tests and authoritative docs.
- No graph update was indicated or performed; no fallback navigation was used.

## Verification

Executed on Python 3.14.5 unless noted:

| Check | Observed result |
|---|---|
| Full non-contract suite with coverage | 1,863 passed; 12 platform/opt-in skips; 16 contract deselections; 83.18% coverage |
| Focused gallery/router/service/cookie/worker regressions | 301 passed; one Windows symlink-permission skip |
| Real gallery upstream/loopback integration | 2 passed, including twelve concurrent subprocesses |
| Story/Highlight worker authorization and collection paths | 18 passed |
| Explicitly enabled external contracts | 3 passed; 13 skipped for missing operator-maintained fixture URLs/config |
| `uv lock --check`, frozen dev sync and `uv pip check` | Passed; only gallery-dl upgraded in the frozen lock |
| Ruff lint/format and strict mypy | Passed; 341 formatted files, 326 typechecked source files |
| Architecture, agent-context and UTF-8 integrity scripts | Passed |
| Installed-version/parser fixture checker | Passed: 14 sanitized fixtures against gallery-dl 1.32.17 |
| Package build and clean wheel installation/resource smoke | Passed; temporary build/install artifacts removed |
| Local Docker candidate build | Passed, `tmb-instagram-fix:local`; not published |
| Built-image CLI, gallery adapter and native-format smokes | Passed with networking disabled |
| Built-image real Story pipeline | Passed: one inspection, real gallery CLI, 2,507-byte valid H.264/AAC MP4, actual ffprobe, unchanged cookie source, loopback-only HTTP |
| Real direct yt-dlp cookie smoke | Upstream source overwrite reproduced; four concurrent protected contexts preserve source |
| Secret hook, including explicitly named new files | Passed using isolated writable caches; default local hook cache had a permission error |

The first full suite found four collection test doubles missing the engine's new optional
`max_assets` argument. Their contract was corrected, without weakening assertions; the dedicated
Story/Highlight suite then passed. The first local upstream harness also exposed that current
Instagram defaults use web/GraphQL user lookup rather than the old REST-only fixture. Its server
now serves the actual default HTML/script/GraphQL protocol, with transport redirected to loopback;
no extractor behavior is mocked or production fallback added.

Live Instagram authentication/private availability was not claimed: production diagnostics were
read-only, and no production credentials were exported into local tests. The opt-in gallery tests
without supplied fixture configuration were skipped, not reported as live successes. Windows-only
permission/platform skips do not establish POSIX-only behavior; the built runtime smoke separately
exercised the Linux application/download/probe path.

## Modified-file inventory

Runtime and dependency changes:

- `pyproject.toml`
- `uv.lock`
- `src/telegram_media_bot/application/ports/download_engine.py`
- `src/telegram_media_bot/application/services/download_service.py`
- `src/telegram_media_bot/domain/models.py`
- `src/telegram_media_bot/domain/errors.py`
- `src/telegram_media_bot/infrastructure/media_engine_router.py`
- `src/telegram_media_bot/infrastructure/gallerydl/adapter.py`
- `src/telegram_media_bot/infrastructure/gallerydl/command_builder.py`
- `src/telegram_media_bot/infrastructure/gallerydl/errors.py`
- `src/telegram_media_bot/infrastructure/gallerydl/parser.py`
- `src/telegram_media_bot/infrastructure/gallerydl/runner.py`
- `src/telegram_media_bot/infrastructure/ytdlp/engine.py`
- `src/telegram_media_bot/workers/jobs.py`

Verification and packaging:

- `.github/workflows/ci.yml`
- `.github/workflows/publish-container.yml`
- `scripts/check_gallerydl_fixtures.py`
- `scripts/check_package_assets.py`
- `tests/integration/test_gallerydl_cookie_integrity.py` (new)
- `tests/fixtures/gallerydl/local_transport.py` (new, loopback-only upstream transport)
- `tests/integration/test_runtime_cookie_consumers.py`
- `tests/unit/application/test_download_service.py`
- `tests/unit/infrastructure/gallerydl/test_adapter.py`
- `tests/unit/infrastructure/gallerydl/test_regressions.py` (new)
- `tests/unit/infrastructure/test_media_engine_router.py` (new)
- `tests/unit/infrastructure/ytdlp/test_read_only_cookies.py` (new)
- `tests/unit/workers/test_gallery_retry.py` (new)
- `tests/unit/workers/test_jobs.py`
- `tests/unit/workers/test_story_highlight_jobs.py`
- `tests/unit/test_cli.py`
- `tests/unit/workers/test_instagram_empty_response.py` (removed incidental wording assertions;
  meaningful empty-response/error/retry regressions remain in the adapter/worker tests)

Documentation and generated source-integrity metadata:

- `CHANGELOG.md`
- `docs/PROJECT_SPEC.md`
- `docs/ARCHITECTURE.md`
- `docs/DECISIONS.md` (ADR-050)
- `docs/agent/DECISION_INDEX.md`
- `docs/CODE_MAP.md`
- `docs/STATUS.md`
- `docs/THIRD_PARTY_NOTICES.md`
- `docs/INSTAGRAM_DOWNLOAD_RELIABILITY.md` (new)
- `FILE_MANIFEST.txt` (regenerated after final source/documentation changes)
