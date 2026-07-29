# Handoff verification report

Generated: 2026-07-28

## Current change addendum

- Version 1.0.5 exposes only MP4 Native H.264/AAC, WebM Native VP9/Opus, and MP3 in
  Telegram. Generic MP4/WEBM and explicit-transcode video choices are absent even if the internal
  conversion switch is enabled.
- The application-owned native option catalog validates codecs and `transcode_required`, labels
  actual selected resolution/FPS/dynamic range/stream-summed size, deduplicates fallback and Best
  Original aliases, and creates a 16-character opaque option identity.
- Versioned `c2`/`o2`/`n2` callbacks remain below 64 bytes. Back edits the same message and reuses
  the persisted selection; expired/tampered and legacy `container:`/`fmt:` callbacks create no job
  and safely return users to a new-link or Native selection path.
- Inspection logs `native_options_built` with source/container counts, hidden transcode and unknown
  size totals, plus the selected IDs/codecs/geometry/size for every visible option. CI and release
  images run the packaged native UI callback/catalog smoke in addition to stream-copy smoke.
- Version 1.0.4 makes ordinary MP4 a native H.264/AVC + AAC stream-copy contract. Codec
  compatibility is evaluated before resolution/FPS/bitrate; AV1 format 399-like candidates cannot
  enter the fast-MP4 plan merely because their extension is MP4.
- The default `lower_resolution` policy selects the highest lower compatible H.264 stream and
  discloses the actual output height. `fail` rejects instead. WebM remains native VP9 + Opus and
  `best_original` remains codec-preserving.
- Converted MP4 uses a separate backward-compatible callback policy, is hidden by default, and
  must pass a conservative duration/pixel/FPS/codec/thread/cgroup-CPU timeout estimate before
  FFmpeg starts. Rejection is a non-retryable domain result with user guidance.
- Native MP4/M4A merging explicitly supplies `-c:v copy -c:a copy -movflags +faststart`; no
  `libx264`, scaling, FPS filter, CRF, or AAC encode argument is present on that path.
- CI and the tag publication workflow run the same packaged, network-free selector smoke inside the
  final runtime image, asserting `137+140`, rejecting `399`, checking native `248+251` WebM,
  stream-copy-only merger arguments, and Best Original normalization.
- The v1.0.3 updater runs from an isolated copy, validates complete staged scripts/Compose/config,
  restores archive executable modes, installs application entries through rollback snapshots,
  performs runtime-user filesystem and SQLite WAL probes, verifies post-start health, and restores
  the prior application/image/permissions/link/services on failure.
- Linux release tarballs package `scripts/tmb.sh` as a symlink to an executable
  `scripts/tmb-current.sh`, allowing the published v1.0.2 updater to replace the pathname without
  truncating its own executing inode. Compose restart attempts are bounded.
- Cancellation is now durable-first: SQLite reaches terminal `cancelled` before official ARQ abort,
  cancelled active rows are never recovered, finalized transient keys/directories are cleaned, and
  simultaneous user cancellation plus shutdown is consumed without ARQ requeue or a shielded-future
  warning.
- FFmpeg is limited to two encoder threads and one simultaneous transcode by default, has a
  25-minute timeout and disable switch, emits machine-readable progress fields, and terminates its
  process group on cancellation/error. Compose exposes optional `TMB_WORKER_CPUS`.
- Linux update now repairs owner-only runtime permissions from shared `APP_UID`/`APP_GID`, preserves
  all v1 state, repairs the global `tmb` command, and leaves services stopped with the prior image
  restored if permission migration is unsafe.
- The runtime image guarantees both `7zz` and `7z`; CI and publication smoke tests create, split,
  and verify a real archive rather than checking package text only.
- Project and package metadata are aligned at `1.0.5`; the lockfile changed only the editable
  project version, with no dependency upgrade.
- Instagram automatic downloads now create and enqueue the same native-only `best_original`
  contract. `force_mp4` selects native MP4 video plus M4A audio for merge/remux only; disabling it
  leaves the source container unconstrained.
- VP9 inside MP4 is distinguished from Telegram's H.264/AAC inline-video profile and remains valid
  for direct document delivery without encoding.
- Forced codec conversion is CRF-first (`libx264` CRF 20/preset medium for MP4). The configured
  maximum is a ceiling; bitrate targeting runs only after an oversized or disproportionate
  quality-pass result.
- Structured selection/transcode logs include source container/codecs/size, selected format IDs,
  reason, target codec, CRF or bitrate, and final size.
- The exact v1.0.0 configuration fixture and representative v1.0.1/v1.0.2/v1.0.3/v1.0.4
  configurations load unchanged under v1.0.5. Mocked Linux and Windows
  patch-upgrade tests confirm that only `TMB_IMAGE` changes in `.env`, while config, cookies,
  SQLite, Redis, and existing downloads remain intact.
- CI and release image builds now share the
  `type=gha,scope=telegram-media-downloader-bot-amd64` BuildKit cache. Static workflow tests verify
  the loaded CI image smoke test, Compose validation, least-privilege permissions, exact shared
  scope, and isolation of the Telegram API stage from application source changes.

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
  amd64 images;
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
- Ruff format check: passed for 111 Python files.
- Strict mypy: passed for 102 source/test files.
- Default test suite: 269 passed, 7 skipped on this Windows host (the destructive large-file case
  plus 6 Linux-only full-script Bash parse cases), and 10 external contracts deselected.
- Core branch coverage: 83.05%, above the enforced 80% floor.
- Architecture boundary check: passed; only
  `infrastructure/ytdlp/` imports yt-dlp and Telethon is absent.
- Opt-in contracts: both built-in YouTube inspection/selection plans passed; 8
  operator-environment fixtures skipped because their URLs were unset.
- UTF-8/text integrity: passed for 173 text files.
- Deterministic source manifest regenerated and verified with 179 release entries.
- SQLite migration, WAL contention, atomic usage, and cancel-safe recovery tests passed.
- Linux and Windows mocked `tmb update` tests passed for success, release-download failure, and
  checksum failure. Linux additionally passed permission rollback, candidate crash-state rollback,
  transaction ordering, lost executable-mode recovery, state preservation, global `tmb` repair,
  `command -v`, installed-script `bash -n`, and `tmb status` assertions.
- External extractor SDK: lock/sync passed; 1 default test passed and 1 contract was deselected.
- `config.example.yaml`, Compose YAML, both workflow YAML files, and JSON schema parsed successfully.
- PowerShell AST parsing: passed for all 5 scripts.
- Bash syntax parsing: passed for all 6 scripts.
- Dependency integrity: `uv run pip check` passed.
- Dependency audit: `pip-audit` reported no known vulnerabilities.
- Detect-secrets baseline and explicit tracked/untracked scans passed.
- Python 1.0.5 sdist and wheel builds passed.
- The privileged filesystem/SQLite/Docker upgrade test could not start because no
  Docker, Podman, or Nerdctl executable is installed on this host.
- `config.example.yaml` passed `config-check` without printing secrets.
- `git diff --check`: passed.

Tests cover all-channel membership, administrator bypass, cache behavior, proxy schemes and legacy
behavior, old/new container callbacks, codec-first MP4 selection, lower-resolution/fail fallback,
native WebM, pre-spawn timeout rejection, fixed-height behavior, WebM
conversion/delivery, dynamic attribution, multi-artifact delivery, SQLite migration and usage
idempotency, tracked upload progress, multipart persistence, Local API migration safety, and safe
interactive configuration output.

## Checks not executable on this host

- Docker Desktop/Engine is not installed, so an actual Compose startup or final Docker build could
  not run locally. CI has a required image build and the release workflow publishes the supported
  amd64 image only after a matching version tag.
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
