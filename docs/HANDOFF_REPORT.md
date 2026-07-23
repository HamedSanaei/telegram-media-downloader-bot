# Handoff verification report

Generated: 2026-07-23

## What is implemented

- Strict, local-only YAML configuration with Pydantic validation.
- Separate aiogram bot and ARQ worker processes connected through Redis.
- A project-owned `DownloadEngine` contract.
- One isolated `YtDlpEngine` adapter; direct `yt_dlp` imports outside its adapter directory are rejected.
- Semantic download modes and centralized yt-dlp option mapping.
- Per-job output directories, output-root validation, normalized errors, and cleanup after delivery.
- Docker Compose runtime, Linux and PowerShell management scripts, CI, pre-commit configuration,
  task specifications, architecture documentation, and a ready-to-paste Codex prompt.
- Python 3.14-or-newer project policy, with Python 3.14 selected for local uv and configurable Docker builds.

## Verification completed in the artifact environment

- 36 unit tests passed; 1 external contract test was deliberately deselected.
- Core measured coverage: 93.21%, above the configured 80% gate.
- Python bytecode compilation passed for `src/`, `tests/`, and `scripts/`.
- Architecture boundary check passed.
- UTF-8 and mojibake integrity check passed.
- TOML, YAML, and generated JSON schema parsing passed.
- `manage.sh` shell syntax check passed.

## Environment limitation

The artifact environment's Python package registry returned HTTP 503 while resolving dependencies.
For that reason, a generated `uv.lock` is not bundled and the full Ruff and mypy commands could not
be executed here. This is not hidden: `./manage.sh up`, `./manage.sh lock`, `./manage.ps1 up`, and
`./manage.ps1 lock` generate the lock file before Docker builds. CI also generates the lock and then
runs Ruff, mypy, tests, coverage, and the Docker build. The lock file must be reviewed and committed
before the first release.

## Recommended Codex entry point

Give Codex the root `PROMPT_FOR_CODEX.md`. It explicitly instructs Codex to read `AGENTS.md`, all
architecture documents, status, acceptance criteria, and task files before implementing the
remaining milestones in one coherent pass.
