# Scheduled CI dependency security refresh (2026-10-23)

## Cause and scope

Run `39916717831` is a scheduled full CI run, not a new push. Commit `be09c8c` passed
the full workflow and live audit on October 20; the same commit reported 9, 27, then 38
advisory entries on October 21, 22, and 23. The dependency database is live even though
`uv.lock` fixes installed versions. Only dependency and its aggregate final-ci-gate failed.

Targeted upgrades: certifi `2026.7.22` to `2026.10.19`, Pillow `12.3.0` to `13.0.0`,
virtualenv `21.14.5` to `21.14.9`; virtualenv requires packaging `>=26.3`, advancing
its locked `26.2` to `26.3`. No other package version, workflow, engine, application
configuration, database, or production service is changed. No release tag is created.

Pillow's old `<13` ceiling blocked the security release; the requirement now accepts
`>=13.0,<14`. Reviewed upstream removals do not affect the project's RGB PNG charts,
FreeType fonts, image signature/dimension checks or verification. Existing behavior tests
cover these paths. Four offline tests additionally guard the three reviewed lockfile
floors and the Pillow requirement. They do not claim to replace ongoing live auditing.

## Validation

- Initial Windows live `pip-audit --local --skip-editable`: no known vulnerabilities.
- Focused security-floor/chart/image pytest: 23 passed.
- Exact `bash scripts/ci_fast_quality.sh` under WSL, using an isolated Linux environment:
  passed; 1,890 passed, 5 host-capability/opt-in skips, 16 contract deselections, 83.29%
  coverage. Expected cross-filesystem hardlink fallback was informational.
- Windows full non-contract pytest: 1,883 passed, 12 platform/opt-in skips, 16 contract
  deselections, 83.19% coverage. This separately exercises the Node browser simulator
  and PowerShell companion management guards absent from the WSL host.
- Python 3.14.5, `uv lock --check`, frozen dev sync, Ruff check/format, strict mypy
  (329 files), architecture/context/text checks, manifest check, secret scan, pip check,
  and `git diff --check`: passed. New files were separately scanned with no findings.
- Exact Linux dependency lane, including bootstrap pip upgrade and `--no-sync` audit:
  passed with no known vulnerabilities.
- Wheel/sdist build and clean wheel-install assets/font smoke: passed. The clean install
  deliberately resolves declared package requirements independently of the frozen runtime;
  it does not modify `uv.lock` or the project environment.
- Frozen runtime Docker build: passed. Non-root, read-only, network-disabled weekly/monthly
  chart smoke, gallery adapter/native selection/native UI smokes, and TLS certificate-bundle
  loading: passed. Core restart policy and companion Compose syntax: passed.
- Hosted full validation is supplied by the CI workflow attached to the fix push; its
  result is reported separately after all jobs and final-ci-gate complete. Publication and
  production activation are not part of this CI repair.

Keep all CI gates fail-closed. Future advisories can legitimately fail scheduled runs;
this refresh is not a promise that dependencies will never need security maintenance.

## Upstream sources

- https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html
- https://github.com/certifi/python-certifi/releases/tag/2026.10.19
- https://github.com/pypa/virtualenv/releases/tag/21.14.9

## Graphify usage

- Version: `0.9.26`.
- `graphify check-update .`: exit 0, no stale/update message; no update required/reported.
- Exact bounded query: `graphify query "Trace Pillow certifi virtualenv dependency security update through lockfile chart rendering tests CI and publish workflow" --budget 1200 --graph graphify-out/graph.json`.
- Discovered: `chart_font_bytes`, `usage_chart_renderer.py`, `test_ci_workflow.py`,
  ADR-005 (locked updates) and ADR-025 (Pillow charts). Verified source/tests directly.
- No graph update or fallback navigation used; no graph/report dump.
