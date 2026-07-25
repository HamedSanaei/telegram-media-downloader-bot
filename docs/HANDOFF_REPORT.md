# Handoff verification report

Generated: 2026-07-25

## Release scope

This release keeps the application insulated from yt-dlp internals while adding:

- membership in every configured Telegram channel before inspection/container/format selection,
  with administrator bypass, fail-closed checks, Redis positive/negative caches, and forced recheck;
- an optional secret HTTP(S)/SOCKS proxy scoped only to yt-dlp, including legacy-config behavior;
- real two-step MP4/WebM then quality selection, exact-height availability, native/transcoded labels,
  selected-stream sizes, MP3 audio, and native-only non-transcoding `best_original`;
- automatic highest-quality MP4 delivery for Instagram posts, Reels, video Stories, Highlights,
  and ordered multi-video collections, with optional local read-only cookies;
- dynamic `@bot_username` attribution on every direct file, artifact, ZIP volume, and manifest;
- permanent SQLite/WAL user profiles, daily usage, delivered bytes, and job-id-based idempotent
  outcome accounting;
- polished source/download/convert/package/upload/finalization messages without exposing Local Bot
  API, paths, providers, or exception details to end users;
- Docker-first one-line Linux and Windows installers, an interactive `tmb` lifecycle command, a
  dedicated Local Bot API service built from pinned official source, and version-pinned GHCR
  amd64/arm64 images;
- SHA-256-verified release archives for install/update, generated and attached by tag CI;
- state-aware `tmb update`: only previously running application writers stop/restart, Redis and
  its ARQ queue stay online, SQLite/WAL is backed up consistently, and failed downloads/checksums
  restore the prior image/service set;
- atomic successful-delivery state and byte accounting, plus no-retry quarantine when receipt
  persistence becomes uncertain.

Files up to the configured 1900 MB direct ceiling are sent unchanged through the Local Bot API.
Larger files through the 4096 MB media ceiling use stored 1850 MB ZIP volumes with a SHA-256
manifest. No Telegram user account, phone number, SMS code, 2FA password, Userbot, or MTProto
session is present.

## Verification completed on this host

- Runtime baseline: CPython 3.14.5, locked yt-dlp 2026.07.04.
- `uv lock --check`: passed.
- `uv sync --frozen --group dev`: passed; 80 installed packages checked.
- Ruff lint: passed.
- Ruff format check: passed for 102 Python files.
- Strict mypy: passed for 93 source/test files.
- Default test suite: 204 passed, 1 explicitly destructive large-file case skipped, and 8 opt-in
  external contracts deselected.
- Core branch coverage: 83.67%, above the enforced 80% floor.
- Architecture boundary check: passed; only
  `infrastructure/ytdlp/` imports yt-dlp and Telethon is absent.
- UTF-8/text integrity: passed for 163 text files.
- Deterministic source manifest regenerated and verified with 167 release entries.
- SQLite migration, WAL contention, and atomic usage tests: 11 passed.
- Linux and Windows mocked `tmb update` tests passed for success, release-download failure, and
  checksum failure, including service-state restoration and Redis preservation assertions.
- External extractor SDK: lock/sync passed; 1 default test passed and 1 contract was deselected.
- `config.example.yaml`, Compose YAML, both workflow YAML files, and JSON schema parsed successfully.
- PowerShell AST parsing: passed for installers, managers, and the Windows recovery test.
- Bash syntax parsing: passed for installers, managers, and the Linux recovery test.
- Dependency integrity: `uv run pip check` passed.
- Dependency audit: `pip-audit` reported no known vulnerabilities.
- Detect-secrets baseline and explicit tracked/untracked scans passed.
- Python sdist and wheel builds passed.
- Local ignored `config.yaml` passed `config-check` without printing secrets.
- `git diff --check`: passed.

Tests cover all-channel membership, administrator bypass, cache behavior, proxy schemes and legacy
behavior, container callbacks, MP4/WebM selectors, fixed-height no-fallback behavior, WebM
conversion/delivery, dynamic attribution, multi-artifact delivery, SQLite migration and usage
idempotency, tracked upload progress, multipart persistence, Local API migration safety, and safe
interactive configuration output.

## Checks not executable on this host

- Docker Desktop/Engine is not installed, so an actual Compose startup or final Docker build could
  not run locally. CI has a required image build and the release workflow has required multi-arch
  publication.
- ShellCheck and PSScriptAnalyzer are not installed locally. Bash/PowerShell parsers passed, and CI
  now has required Linux ShellCheck and Windows PSScriptAnalyzer jobs.
- Fresh Ubuntu VM and Windows Sandbox end-to-end installer runs need Docker and release credentials
  and were not available on this workstation.
- The contract command ran, but all 8 source contracts skipped because operator-maintained public
  fixture URLs were not configured. The real Local API upload over 200 MB also remained skipped
  because its destructive opt-in variables were absent.

## Operational limitations

- Private, expired, or login-gated Instagram Stories/Highlights require a valid operator-owned
  Netscape cookies file. The project does not bypass authentication or DRM.
- Telegram has no upload idempotency key. A lost response is quarantined as
  `delivery_uncertain` and is never automatically resent.
- Multi-volume recipients need 7-Zip and must start extraction from `.zip.001`.
- SQLite/WAL is appropriate for the supported single-host topology. Multi-host workers need a
  shared leased database adapter.
- The installers consume checksummed assets from the latest GitHub Release; a tag must be published
  before the public one-line installer can install that version.
- Broadcast, user export, and administrative usage-report commands are intentionally outside this
  release; the permanent data model required for them is present.

## Release commands

Run `./manage.sh check` (or `manage.ps1 check`) and a real Compose/Docker build on a Docker-capable
release host. Publish a signed/versioned Git tag so CI creates the checksummed source assets and
matching immutable GHCR image. Use reviewed fixtures for opt-in contracts and retain the previous
image, release archive, lockfile, `config.yaml`, and database backup for rollback.
