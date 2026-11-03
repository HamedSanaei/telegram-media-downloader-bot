# Restore the CI baseline (2026-11-03)

The scheduled run `41609946686` and PR #3 run `41682115698` failed on two independent
checks: embedded release-policy drift and `msgpack` 1.2.1 (CVE-2026-114613). The PR's
Docker runtime and healthcheck smoke had passed.

Synchronize all four Linux/Windows standalone policy snapshots with the already existing
RC17 withdrawal. Preserve the old v1.3.7 guidance and RC18 as an allowed replacement.
Linux and Windows mocked install/upgrade tests now verify RC17 rejection for explicit tags
and for an alias resolving to RC17, without reaching image or service operations.
Neither the canonical policy nor the transactional sequence changes.

Update only `msgpack` to 1.2.3 in the frozen lock; it is pulled in by the audit tool's
CacheControl dependency. Keep both the offline lockfile floor check and the live
`pip-audit` gate. See the [upstream fix](https://github.com/msgpack/msgpack-python/releases/tag/v1.2.3).
No new release, tag, or production deployment is part of this repair.

## Local validation

- Linux and Windows update/recovery fixtures passed, including the new RC17 cases.
  ShellCheck passed; PSScriptAnalyzer reported no errors.
- Exact WSL fast-quality lane passed: 1,895 non-contract tests passed, five
  host-capability/opt-in skips, 16 contract deselections, 83.28% coverage.
- Wheel/sdist build, clean wheel install smoke and the live dependency audit passed.
  Lockfile comparison found no package changes other than `msgpack`.

## Graphify usage

- Version `0.9.26`; freshness check exit 0 without a stale report. No update or fallback.
- `graphify query "Trace release policy installer snapshots msgpack lockfile dependency audit tests CI and publish workflow" --budget 1200 --graph graphify-out/graph.json`
- Found `assert_release_allowed`, `test_release_policy.py`, `test_ci_workflow.py` and the
  locked-dependency ADR. Checked the standalone scripts and canonical policy directly.
