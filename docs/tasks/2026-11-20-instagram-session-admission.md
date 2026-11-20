# Issue 4: credential-wide Instagram admission

Implement distributed admission for the shared operator session and isolated owner/generation
sessions. Redis permits coordinate concurrent workers; additive SQLite/WAL tables preserve
rolling quotas, active quarantine and provider circuits through restart/Redis loss. Account-wide
429/Retry-After and manual challenge/reauthentication pauses prohibit subsequent extraction and
provider-error credential fallback. Local pre-extraction public fallback retains an independent
operator gate. Private authorization and read-only cookie handling remain in force.

Inspection and transfer operations are charged separately. Existing same-operation reuse is
preserved without caching signed URLs or trusting UI selections. Private operator-cookie snapshots
pin credential identity during atomic upload and are cleaned under the existing job lifecycle.
Defaults, sanitized status/review CLI, health/metrics/Logger visibility and incident response are
documented in [the operations guide](../INSTAGRAM_SESSION_SAFETY.md). No production account, server
or credential was used for validation. This task does not publish a release or deploy production.

## Validation

Offline coverage includes two Telegram users sharing one operator budget/circuit, two spawned
worker processes with real isolated Redis, restart persistence after transient-key loss, private
fail-closed behavior, public fallback, generation rotation, cancellation, atomic cookie replacement,
rolling budgets, long cooldown floors, migration and inspection/request amplification. The quality
CI job supplies a disposable Redis so its process test executes there as well.

- Linux `bash scripts/ci_fast_quality.sh`: 1,928 passed, 5 platform/opt-in skips,
  16 contract deselections, 83.49% coverage. Real Redis process coverage executes, not skips.
- Windows complete non-contract suite: 1,919 passed, 12 platform/opt-in skips, 82.94% coverage;
  subsequent focused worker/CLI tests cover final snapshot and unavailable-identity handling.
- Explicit `RUN_CONTRACT_TESTS=1 uv run pytest -m contract`: 3 public yt-dlp regressions passed,
  13 fixture/opt-in skips. No Instagram account was contacted for these checks.
- Lock check, frozen sync, Ruff, strict mypy (339 files), architecture/context/text/manifest and
  secret checks pass. Live dependency audit reports no known vulnerabilities without exclusions.
- Wheel/sdist and clean install smoke, frozen Docker build, packaged admission/CLI runtime smoke
  and Compose configuration validation pass. No updater transaction or production deployment runs.

## Graphify usage

- Version: `graphify 0.9.26`.
- Freshness: `graphify check-update .` exited 0 with no stale indication. No update was required;
  no fallback navigation was used.
- Exact bounded queries:
  - `graphify query "Trace Instagram credential resolution in worker inspection download retries fallback through DownloadService RoutedMediaEngine Redis and session errors and tests" --budget 1200 --graph graphify-out/graph.json`
  - `graphify query "Trace CI quality pytest integration Redis fixtures and conservative required lane classification" --budget 1200 --graph graphify-out/graph.json`
- Located DownloadService, RoutedMediaEngine, worker credential attempts, credential/vault models,
  gallery/yt-dlp entry points and CI quality/integration ownership. Verified actual behavior in
  source, tests, ADR-007/008/017/023/034/047/050 and the architecture/specification. Truncated graph
  discovery was not treated as behavioral evidence.
