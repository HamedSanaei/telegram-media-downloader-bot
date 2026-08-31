# T033 - Fast-feedback CI and conditional heavy validation

**Status:** implemented

## Goal

Design a three-level GitHub Actions validation policy that gives ordinary application changes fast
feedback, runs expensive infrastructure checks when their owned paths change, and preserves a
complete manual/nightly/release validation mode.

## Why

The current development workflow runs the full Docker image, runtime smoke, privileged historical
updater matrix, and Linux/Windows installer matrix for every push and pull request. That protects
production well, but the feedback cost is disproportionate for ordinary Python or documentation
changes. The optimization must change when checks run, never what they protect.

## Dependencies

- Current `.github/workflows/ci.yml` and `.github/workflows/publish-container.yml`.
- `scripts/tmb.sh`, `scripts/tmb.ps1`, `install.sh`, `install.ps1`, `manage.sh`, `manage.ps1`,
  release archive/policy scripts, and their Linux/Windows fixtures.
- T011 controlled upgrade/canary policy; ADR-015, ADR-018, ADR-023, ADR-029, and ADR-031.
- Existing architecture, agent-context, text-integrity, manifest, quality, security, Docker,
  Local Bot API, updater, installer, package, and release checks.

## Current CI baseline

Development CI is triggered by every push to `main` and every pull request. It always starts:

1. `quality`, which checks out the repository, sets up uv, parses all release Bash scripts,
   installs Python 3.14.5, validates the lock and frozen environment, runs architecture,
   agent-context, text-integrity, manifest, Ruff, format, mypy, detect-secrets, pip check,
   pip-audit, package build/assets install smoke, external extractor SDK tests, and the full
   non-contract pytest coverage suite.
2. `docker`, which validates Compose restart policy, builds the amd64 runtime image with the shared
   BuildKit/GHA cache, runs gallery-dl, yt-dlp, Telegram UI, inspection-workspace, ffmpeg/ffprobe,
   7-Zip, doctor, and chart smokes, then runs the privileged updater filesystem/SQLite matrix and
   Local Bot API readiness test.
3. `installer-quality`, a Linux/Windows matrix. Linux runs ShellCheck, Bash parsing, and update
   recovery; Windows installs and runs PSScriptAnalyzer and runs PowerShell update recovery.

The tag-only publication workflow independently builds and smoke-tests the published image, runs
the privileged updater matrix, verifies tag/package/release policy, builds reproducible archives,
and creates the release. It is not a substitute for development CI and must remain fail-closed.

Measured GitHub Actions history (UTC):

| Run | Workflow result | Quality | Docker | Updater step | Installer Linux/Windows | Wall-clock signal |
|---|---:|---:|---:|---:|---:|---:|
| `33344361437` docs planning push | success | 69s | 646s | 563s | 11s / 31s | 648s |
| `33345803568` VIP source push | success | 76s | 637s | 555s | 11s / 27s | 648s |
| `33331204551` release-prep push | success | 72s | 649s | 559s | 10s / 36s | 660s |

The latest successful tag-publication run `33333448333` took about 533s for `publish`, including a
443s updater step, followed by an 11s `release` job. These are representative warm-cache hosted
runner observations; the implementation must capture cold-cache and warm-cache distributions
before and after the change rather than claim improvement from fewer YAML lines.

## Scope

- Design `FAST CI`, `CONDITIONAL HEAVY CI`, and `FULL VALIDATION` lanes.
- Add deterministic changed-path classification that works for pull requests, main pushes,
  workflow dispatch, scheduled runs, and tags without relying on workflow-level `paths:` filters.
- Keep stable always-reporting change-detection and final-gate checks so branch protection cannot
  hang when an expensive job is irrelevant.
- Split Docker image/runtime correctness conceptually from the historical updater/rollback matrix.
- Make concurrency cancellation safe for development runs and non-destructive for publication.
- Document caching, duplicate-work decisions, baseline measurement, rollout, and troubleshooting.

## Non-goals

- Do not modify `.github/workflows/publish-container.yml` in T033. The development `.github/workflows/ci.yml`
  *is* modified by the T033 implementation task (tiered lanes), per separate authorization.
- Do not remove, weaken, delete, or silently downgrade any existing test, security assertion,
  package check, Docker smoke, installer check, updater scenario, release-policy guard, or release
  verification.
- Do not change application runtime, dependencies, version metadata, Docker images, installers,
  updater behavior, tags, releases, deployments, or branch protection in T033 itself.

## Proposed architecture

```text
always-running change-detection
              |
              +--> fast-quality (stable check)
              |
              +--> conditional dependency/package
              +--> conditional docker-runtime
              +--> conditional installer-linux
              +--> conditional installer-windows
              +--> conditional updater-integration
              +--> conditional plugin-sdk
              |
              +--> final-ci-gate (stable check, always evaluates)

workflow_dispatch / scheduled full mode
              |
              +--> all fast and heavy lanes forced on
```

The implementation must keep one workflow invocation reporting the stable `change-detection`,
`fast-quality`, and `final-ci-gate` checks. Heavy jobs may be skipped only after classification says
they are irrelevant. `final-ci-gate` uses `needs` with `if: always()` and fails when a required
heavy job fails or is cancelled; it treats an explicitly irrelevant skipped job as successful.
Workflow-level `paths:` filters are prohibited because they can prevent required checks from
reporting at all.

If a classifier errors, cannot obtain the correct base/head range, sees an unknown/root-critical
file, or encounters an event shape it does not understand, it requests every heavy lane. Unknown
never means safe-to-skip.

## Persistence and state

T033 introduces no application or CI database schema. The future classifier may persist only
bounded, non-secret run metadata needed for measurement (category, forced-heavy fallback, lane
outcome, duration, cache signal, and event type); it must not persist credentials, cookies, SQLite
production state, job payloads, or validation results as a substitute for executing a check. GitHub
Actions artifacts and caches remain disposable and scoped to a commit, lockfile, Python version, or
BuildKit target as appropriate. A rerun after cache loss must execute the same validation rather than
trust a stale result.

## Configuration

The later implementation must use explicit workflow inputs for `workflow_dispatch` full mode and
keep the existing release workflow's tag and policy inputs authoritative. Classification rules,
stable check names, lane ownership, and cache scopes should be versioned with the workflow and
validated as configuration; malformed YAML, missing event fields, or an unknown input fails
conservative. No application `config.yaml`, configuration model, runtime environment variable, or
production secret is added by T033.

## Change classification

Classification is the union of all matching categories. The implementation must use the event’s
actual comparison range: pull requests compare the PR base SHA to head SHA; pushes compare the
`before` SHA to the pushed head; initial/unavailable ranges fetch enough history or fail
conservative. Manual full mode bypasses classification.

| Category | Repository-owned paths and effects | Required lanes |
|---|---|---|
| Python/application | `src/**`, `tests/**`, ordinary Python tooling and fixtures | Fast quality, including full non-contract pytest for source/test changes |
| Dependency/package | `pyproject.toml`, `uv.lock`, package metadata, package asset/license files, `scripts/check_package_assets.py`, `plugins/example_extractor/pyproject.toml`, plugin lockfile | Fast + dependency/package + Docker runtime; plugin SDK when plugin paths match |
| Docker/runtime image | `Dockerfile`, `.dockerignore`, `docker-compose.yml`, Compose-related files, image build inputs, `src/telegram_media_bot/cli.py`, `bootstrap/**`, Local Bot API, storage/workspace, archive, observability, worker/runtime contract paths | Fast + Docker runtime; updater integration also when filesystem, service, persistence, or image lifecycle semantics are affected |
| Installer/updater | `install.sh`, `install.ps1`, `manage.sh`, `manage.ps1`, `scripts/tmb.sh`, `scripts/tmb.ps1`, `scripts/build_release_archives.sh`, `scripts/check_release_policy.py`, `scripts/tests/test_tmb_update.sh`, `scripts/tests/test_tmb_upgrade_integration.sh`, `scripts/tests/test_local_api_readiness.sh`, `scripts/tests/Test-TmbUpdate.ps1`, backup/restore, permission, Local API lifecycle, and persistent-path code | Fast + matching Linux/Windows installer lane; updater integration for shared/release/update contracts |
| CI/release | `.github/workflows/**`, `.github/actions/**`, `release-policy.json`, release verification/build scripts, Renovate or publication policy | Fast + all relevant heavy lanes, conservatively full when workflow semantics are affected |
| Plugin SDK | `plugins/example_extractor/**` | Fast + dependency/package + plugin SDK; Docker runtime when image inputs change |
| Documentation-only | `docs/**`, `README*`, `AGENTS.md`, planning/task/ADR/index files, other explicitly documented text-only files | Documentation integrity minimum; no Docker/updater/installer/package heavy lane |

The exact implementation allowlist must be reviewed against the Dockerfile `COPY` set and the
updater’s filesystem/service contracts. A shared runtime or release path is classified conservatively
even when the changed file is Python. Multiple categories activate the union of their lanes.

## Fast lane

The always-reporting fast path evaluates the classifier and then runs, for ordinary Python/application
changes:

- checkout and uv setup/cache;
- Python 3.14 installation;
- `uv lock --check` and `uv sync --frozen --group dev`;
- architecture, agent-context, text-integrity, and manifest checks;
- Ruff lint, Ruff format check, mypy, detect-secrets, and pip check;
- the full ordinary non-contract pytest/coverage suite.

For a conclusively documentation-only change, the stable fast job runs the minimum safe set:
architecture/context routing, text integrity, manifest verification, secret detection, and
`git diff --check`; it does not install/build/test the Python runtime. Any mixed or uncertain change
uses the complete fast path. Documentation integrity is never skipped.

The current Bash syntax parsing may be consolidated out of the fast path because installer/updater
lanes and release publication retain it; keeping one cheap parse as defense-in-depth is acceptable
if timing measurement shows it is immaterial. This is not permission to remove the independent
release-workflow parse.

## Conditional heavy lanes

### Dependency and package

Run `pip-audit` when dependency/package/plugin paths change and in full/nightly/release validation.
Retain `pip check` in the fast lane because it is comparatively cheap. Run `uv build`,
`check_package_assets.py --install-smoke`, external extractor SDK lock/sync/tests, and dependency
security checks when package/plugin ownership changes. These checks remain release-required.

### Docker runtime

Split the current Docker job into image/runtime correctness: lock validation, Compose restart-policy
rendering, BuildKit image build/cache, runtime dependency/doctor, gallery-dl, yt-dlp selector,
Telegram UI, inspection workspace, ffmpeg/ffprobe, 7-Zip, and usage-chart smokes. It runs for the
Docker/runtime category and full mode. It does not automatically run for an ordinary domain Python
change.

### Updater integration

Keep the complete privileged historical matrix and Local Bot API readiness test intact, but run it
as a separate `updater-integration` lane when installer/updater scripts, Compose lifecycle,
filesystem ownership/permissions, persistent paths, backup/restore, Local Bot API lifecycle, image
runtime contract, release-policy logic, or updater tests change. It always runs in full/nightly and
release validation. A normal application-domain edit does not pay this matrix by default.

### Installer quality

- Linux changes run ShellCheck, complete Bash parsing, and `test_tmb_update.sh`.
- PowerShell changes run PSScriptAnalyzer installation/analysis and `Test-TmbUpdate.ps1` on Windows.
- Shared installer/release contracts, workflow changes, or uncertain cross-platform changes run both
  platform lanes.

### Plugin SDK

Plugin-owned changes run the external extractor SDK lock/sync/tests and package validation. A plugin
change that is copied into the runtime image also activates Docker runtime validation.

## Full/manual validation

`workflow_dispatch` must provide an explicit full-validation mode that forces every fast and heavy
lane, regardless of changed paths. A scheduled/nightly full run is recommended for drift detection
and hosted-runner/cold-cache coverage, but its cadence must be chosen from measured cost and quota.
The same complete validation remains mandatory before release publication.

## Release behavior

`.github/workflows/publish-container.yml` remains tag-gated and fail-closed. Release publication must
continue to verify tag/package version and release policy, build and smoke-test the published image,
run the privileged updater matrix, produce reproducible archives, verify checksums, and create the
release only after all prerequisites pass. A fast development check is never evidence that a release
artifact is safe. Any future reusable workflow extraction must preserve these release-only checks,
permissions, ordering, and failure behavior.

## Concurrency

Development and pull-request CI should use a non-destructive concurrency group equivalent to:

```yaml
concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
```

This cancels obsolete runs for the same branch/ref when a newer commit arrives. Publication/release
workflows require a separate group with cancellation disabled (or an equivalent safe policy) so an
in-flight image/release transaction cannot be abandoned halfway through publication.

## Caching

Retain `astral-sh/setup-uv` caching and the shared scoped BuildKit/GHA cache. Evaluate lockfile- and
Python-version-aware uv keys, reuse of setup within a job, and avoiding duplicate installs across
fast/package lanes. Keep the existing BuildKit scope compatible with publication. Never cache
secrets, cookies, credentials, SQLite production state, mutable runtime state, or stale validation
results. Cache misses and cache corruption must simply rerun the check.

## Branch-protection behavior

At inspection time the GitHub API reported `main` as not protected, but the design must remain safe
for future required checks. No path-filtered workflow may disappear. Stable always-running checks
must report on every PR/push; the final gate must understand the classifier’s required/skipped set.
An irrelevant heavy job is a successful skip; a relevant failed or cancelled heavy job fails the
gate. The implementation must document the exact stable check names to require and the migration
sequence for branch protection.

The stable merge-blocking required check is `final-ci-gate`. It depends on `change-detection`, on
the `quality` fast lane, and on every conditional heavy lane, and it fails whenever a required heavy
lane failed or was cancelled or classification itself failed. Requiring `quality` + `change-detection`
alone is NOT sufficient: a relevant heavy lane can fail while both pass, and only `final-ci-gate`
surfaces that. Recommended required checks: `final-ci-gate` (required) and, optionally, `quality`
(required for visibility). `change-detection` need not be a required check itself because
`final-ci-gate` already gates on it.

## Failure semantics

- Classification, Git-range, YAML, or event-shape failure: request full heavy validation.
- Required heavy job failure: final gate fails.
- Required heavy job cancellation or timeout: final gate fails unless the entire run was explicitly
  superseded by the development concurrency policy.
- Irrelevant heavy job skipped after a successful classification: final gate succeeds.
- Missing secrets on forked pull requests must not convert a security check into an unsafe skip;
  use the repository’s supported read-only behavior or fail conservative.
- A fast lane failure never masks a heavy-lane failure, and a heavy-lane failure never becomes green
  merely because the lane was conditional.

## Security considerations

Keep detect-secrets in the fast lane. Keep pip-audit in dependency/package, full, nightly, and
release lanes so dependency vulnerabilities are not removed—only triggered proportionally to
dependency risk. Preserve least-privilege workflow permissions, secret-free logs, lockfile
verification, release-policy enforcement, and all updater permission/backup assertions.

## Telegram and operator behavior

T033 does not change Telegram handlers, Bot API delivery, operator alerts, or user-facing behavior.
The future CI routing tests must continue to exercise the existing Telegram UI, Local Bot API
readiness, delivery uncertainty, and worker contracts whenever their owned runtime or image paths
are classified. A skipped Docker or updater lane is a CI decision only; it must never imply that a
Telegram production path was removed or that release safety was delegated to the fast lane.

## Backward compatibility

The application, package, image, installers, updater scripts, lockfile, release policy, and current
workflow behavior remain unchanged until a separately authorized implementation task. Existing
required-check consumers receive stable reports. The full/manual path remains available even if
changed-file classification is unavailable.

## Testing

The implementation task must add deterministic classifier fixtures/simulations for:

- source-only and tests-only changes;
- documentation-only changes;
- `Dockerfile` and Compose changes;
- `uv.lock` and `pyproject.toml` changes;
- Bash installer/updater and PowerShell installer/updater changes;
- release-policy and workflow changes;
- plugin SDK changes;
- multiple-category changes;
- unknown/root-critical files;
- PR base/head, main push `before`/head, shallow/unavailable history, fork, manual full mode, and
  scheduled mode.

Also validate YAML syntax, rendered `if`/`needs` semantics, stable final-gate behavior for success,
failure, skipped, cancelled, and classifier-error outcomes, cache-key safety, and unchanged release
workflow invariants.

## Measurement plan

Before implementation, record at least 20 representative runs or the maximum available sample,
including workflow wall-clock time, per-job and dominant-step duration, queue/setup time, and
cold-cache versus warm-cache behavior. Segment samples into documentation, Python/application,
dependency/package, Docker/runtime, and updater/installer changes. After implementation, repeat the
same sample classes and publish medians/p95 plus cache-hit rates. Acceptance requires a substantial,
measured reduction for ordinary application/documentation feedback without reducing release-critical
coverage; no absolute minute target is assumed before measurement.

## Migration and rollout

1. Implement and test the classifier/gate in isolation without changing required checks.
2. Run the proposed full mode and classifier fixtures against representative historical changes.
3. Introduce stable always-running detection/fast/final-gate jobs while retaining heavy jobs in a
   parity/shadow configuration.
4. Compare required results and timing, then update branch-protection required-check names only
   after stable reporting is proven.
5. Enable conditional heavy lanes, retaining full/manual and release paths unchanged.
6. Remove only demonstrably redundant development duplication after parity evidence; never remove
   independent release/package/runtime validation.

## Operational considerations

Document which paths activate each lane, how to invoke full CI, how to interpret a skipped heavy
job, how to rerun a failed lane, how to troubleshoot classifier/base-SHA failures, and how to
measure cache behavior. Monitor queue time, job duration, cancellation rate, skipped/forced-heavy
counts, classifier fallbacks, and final-gate outcomes with bounded metadata.

## Acceptance gates

1. Ordinary Python/domain changes receive fast feedback without the irrelevant updater matrix.
2. Documentation-only changes run required documentation integrity checks without Docker/updater
   work.
3. Docker/runtime changes run Docker validation.
4. Installer/updater changes run their corresponding Linux/Windows and updater validation.
5. Dependency/package changes run appropriate security, package, plugin, and runtime checks.
6. Unknown or unavailable classification requests conservative heavy/full validation.
7. Superseded development runs cancel safely; release publication is not destructively cancelled.
8. Required checks always report; skipped irrelevant jobs cannot leave branch protection pending.
9. Existing release-critical safety validation remains available and unchanged in full/release mode.
10. Manual full CI executes every heavy path.
11. Release publication remains fail-closed and is not satisfied by fast CI alone.
12. No test is deleted and no assertion is weakened for speed.
13. Cold/warm baseline and after measurements are documented.
14. Ordinary development feedback is measurably faster.

## Graphify audit

- Version: `graphify 0.9.26`.
- `graphify check-update .` passed with no update required.
- Exact bounded queries used were:
  - `graphify query "Trace CI workflow quality gates Docker validation updater integration release validation and installer tests" --budget 1200 --graph graphify-out/graph.json`
  - `graphify query "Trace Docker build smoke tests release updater scripts installation tests and GitHub Actions ownership" --budget 1200 --graph graphify-out/graph.json`
  - `graphify query "Trace VERSION_OR_UPDATE_CHANGE through version metadata installer updater rollback archive builder tests CI and publish workflow" --budget 1200 --graph graphify-out/graph.json`
- Bounded discovery covered CI quality/Docker/updater/installer ownership and the version/update
  path; `graphify explain "SqliteJobRepository"` was not needed for this task.
- Key source/test boundaries were verified in the workflows and scripts rather than inferred from
  job names: quality gates, package assets, Docker runtime smokes, Local Bot API readiness,
  `scripts/tmb.sh`/`scripts/tmb.ps1`, installer recovery tests, release archives, and release policy.
- No fallback navigation was needed. Graphify remains discovery-only; workflow files, scripts, and
  tests remain authoritative.

## Definition of done (achieved)

The CI policy, changed-path matrix, stable-check/final-gate semantics, concurrency rules, cache
policy, Docker/updater split, installer/dependency/package behavior, full/release guarantees,
measurement method, deterministic tests, rollout plan, and developer documentation are implemented.
The required branch-protection check is `final-ci-gate` (the aggregate merge-safety check), with
`quality` optionally also required for explicit fast-quality visibility; `quality` +
`change-detection` alone is not sufficient:

- `scripts/ci_change_policy.py` — deterministic change classifier (unit-testable without Actions).
- `scripts/ci_fast_quality.sh` / `scripts/ci_docs_quality.sh` — fast lanes.
- Tiered `.github/workflows/ci.yml` — `change-detection`, stable `quality`, conditional heavy lanes
  (`dependency`, `package`, `plugin-sdk`, `docker-runtime`, `updater-integration`,
  `installer-linux`, `installer-windows`), and `final-ci-gate` (success/failure/cancelled/skipped).
- Safe same-ref development concurrency; tag-only publication workflow unchanged.

Post-implementation GitHub run timing is pending the first push (nothing was pushed in this task).
