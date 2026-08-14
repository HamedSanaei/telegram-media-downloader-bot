# Handoff verification report

Generated: 2026-08-14

## Current change addendum

- CI follow-up aligns the privileged fixture's configured `APP_UID/GID` with the installer and uses
  that same identity for its final direct filesystem/SQLite probe. WSL2 diagnostics before update,
  after update, and before the probe showed legacy state moving from `root:root` `0500`/`0400` to
  runtime-owned `0700`/`0600`; only the probe's hard-coded `10001:10001` identity was wrong. The
  fixture now asserts each persistent path and the resolved Compose user/config/data mounts.
  `.env` owner/mode preservation across elevated image-pin replacement remains covered.
- ShellCheck SC2251 is resolved with an explicit conditional that fails when a `gallery_dl` section
  is present; no diagnostic suppression or assertion weakening was introduced.
- Patch 1.2.2 fixes Linux prepared-release preflight. The old command ran the configured old image
  with only `config.yaml`, so `/data` cookie paths valid in Compose were absent. Preflight now pulls
  the prepared image and runs `config-check --read-only-runtime` with read-only root, configuration,
  and project data mounts plus an ephemeral `/tmp`. Cookie checks remain strict; Local Bot API
  directory diagnostics are non-mutating until the existing post-stop runtime UID/SQLite WAL probe.
- Updater regression coverage preserves exact config/cookie bytes, requires no `gallery_dl`
  override, and fails missing/unreadable cookies before service stop. Because the original v1.2.1
  script cannot consume new updater code before its old preflight, v1.2.2 publishes a standalone
  updater with its SHA-256 file. The privileged previous-layout case verifies and runs that release
  asset. Existing project-image cleanup remains unchanged and occurs only after health, version,
  doctor, and Compose verification.
- Patch 1.2.1 fixes mixed Instagram parent discovery when yt-dlp encounters photo carousel children
  with no video formats before the real video child. The yt-dlp adapter reads raw parent entries
  with `process=False`, validates the exact gallery-dl slot count and all video ordinals, and then
  runs strict normal downloads only for validated public video-child URLs. It does not enable
  `ignoreerrors` or accept arbitrary non-zero extraction results.
- The production fixture `DZUwLh3jEDk` is represented by a deterministic 17-slot regression: 16
  gallery images and video slot 11 (`DZUtxnNDJg7`). Photo child `DZUtbhzsvJy` is never selected.
  Resolution failure occurs before image download, preventing deterministic video-plan failures
  from causing duplicate gallery image downloads on retry.
- Version 1.2.0 adds an owner-bound Instagram Photo/Document decision before enqueue. Mixed posts
  retain every source ordinal: gallery-dl supplies validated original images while yt-dlp receives
  only the canonical Instagram post URL for videos. Media groups are deterministic and capped at
  ten; document images retain the exact downloaded bytes and format.
- Terminal inspection/download failures and `delivery_uncertain` states now notify every unique
  `telegram.admin_ids` recipient after retries are exhausted. Alerts contain only opaque job and
  stable classification fields; URLs, user/chat IDs, filenames, paths, cookies, titles, and raw
  exceptions are excluded. One failed recipient does not affect other alerts, the terminal job
  state, user notification, or cleanup.
- Zero-retention was reverified for both download and temporary job directories after success,
  failure, timeout, cancellation, and uncertain delivery. Durable job records remain governed by
  `storage.job_retention_days`, and Telegram-delivered messages are not purged.

- Version 1.0.10 replaces the v1.0.9 primitive-only RGB encoder with a 2200x1450 in-memory Pillow
  dashboard. Weekly/monthly images contain an English title, Tehran-local range, generated time,
  KPI labels and values, a named legend, numeric Y axis, adaptive date labels, and important bar
  values.
- Noto Sans Regular and its SIL OFL 1.1 license are package resources included in the wheel, sdist,
  source release, and production image. `importlib.resources` supplies identical bytes on Windows
  and Linux; runtime font downloads, system fonts/fontconfig, DISPLAY, and external chart APIs are
  not used.
- Font loading is size-cached and fails with `UsageChartFontError` rather than a silent bitmap-font
  fallback. `tmb doctor`, clean-wheel installation, structural text-region tests, and offline
  read-only UID-10001 Docker smoke rendering protect the contract. CI publishes both fixture PNGs.
- Version 1.0.9 shows configured administrators a persistent reply keyboard from `/start`, `/menu`,
  or the backward-compatible `/panel`. Every management message and refresh callback independently
  checks the current `telegram.admin_ids`; ordinary users receive no management keyboard or data.
- The guided administrator URL state injects the same URL-submission callable as direct user/admin
  messages. Validation, required-channel policy, inspection, Native selection, durable jobs,
  callbacks, workers, cancellation, delivery, and zero-retention remain one shared pipeline.
- Weekly/monthly reports are deterministic in-memory Pillow PNG charts; the complete report includes
  sources, formats, delivered volume, terminal outcomes, and a Tehran-local 14-day breakdown.
  Rendering is single-flight per administrator and failures expose no SQL or internal exception.
- Public KPIs filter current administrator IDs during aggregation only. Durable administrator jobs
  and usage events remain available for audit/idempotency, while reports disclose neither IDs nor
  media URLs, filenames, or content.
- Version 1.0.8 enforces symlink-safe, idempotent zero-retention for exact job workspaces after
  success, failure, cancellation, timeout, and delivery uncertainty. Startup and maintenance
  sweepers preserve active/retryable jobs while reclaiming terminal and age-gated orphan work.
- Multi-artifact media and multipart volumes/manifests are deleted individually after Telegram
  returns success and the receipt is durable. Each multipart delivery owns an isolated cancellable
  7-Zip process, so cancelling one job cannot terminate another job's archive process.
- Cleanup emits structured per-job totals and Prometheus counters for files, directories, bytes,
  failures, and duration. No media filename, URL, global cookie, SQLite/Redis state, configuration,
  or sibling job is included in the deletion scope.
- Linux and Windows management commands now expose `tmb cleanup [--dry-run]`. A verified update can
  remove superseded stopped containers from this Compose project and unreferenced old image IDs
  from the exact project repository. Current/referenced images, IDs with foreign repository tags,
  unrelated images, volumes, and build cache are protected.
- Version 1.0.7 canonicalizes supported YouTube URLs with a valid video ID before SQLite, Redis,
  inspection, and download. `watch`, `youtu.be`, `shorts`, and `live` links lose Mix/playlist
  context while explicit `/playlist?list=...` links retain the existing bounded-playlist policy.
- Inspection and download independently force yt-dlp `noplaylist=true` for single-video intent.
  Queue and worker execution boundaries repeat normalization so retries, recovery, and legacy raw
  jobs cannot expand a YouTube Mix or start needless Deno work.
- The `youtube_url_canonicalized` event records validated video/playlist IDs, the canonical URL,
  single-video decision, and removed parameter names without logging unknown credential-like query
  parameters.
- Version 1.0.6 exposes zero-transcode AV1/AAC and H.264/AAC under MP4 Native, VP9/Opus under WebM
  Native, and MP3. Generic MP4/WEBM and explicit-transcode video choices remain absent.
- The application-owned catalog validates codecs and `transcode_required`, labels actual
  resolution/FPS/codec/stream-summed size, keeps AV1 and H.264 distinct, and creates a 16-character
  opaque option identity. The real production fixture exposes `401+140` as
  `2160p · 30fps · AV1 · 249.8 MiB` with no transcode, alongside 1080p H.264.
- The selected codec family is persisted in SQLite, included in the idempotency key and ARQ
  payload, restored after restart, and applied again to download-time selection. AV1 MP4 passes the
  native plan contract but not the inline-video profile, so it is delivered as a document.
- Best Original summaries are derived from the highest-quality visible plan and therefore always
  name a selectable resolution/container/codec/size combination.
- Versioned `c2`/`o2`/`n2` callbacks remain below 64 bytes. Back edits the same message and reuses
  the persisted selection; expired/tampered and legacy `container:`/`fmt:` callbacks create no job
  and safely return users to a new-link or Native selection path.
- Inspection logs `native_options_built` with source/container counts, hidden transcode and unknown
  size totals, plus the selected IDs/codecs/geometry/size for every visible option. CI and release
  images run the packaged native UI callback/catalog smoke in addition to stream-copy smoke.
- Version 1.0.4 originally made ordinary MP4 a native H.264/AVC + AAC stream-copy contract. Codec
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
  final runtime image, asserting AV1 `401+140`, H.264 `137+140`, native `248+251` WebM,
  stream-copy-only merger arguments, no `libx264`, and Best Original normalization.
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
- Project and package metadata are aligned at `1.0.10`; the lockfile changed only the editable
  project version and the required Pillow 12.3.0 runtime dependency, with no unrelated upgrade.
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
- The exact v1.0.0 configuration fixture and representative v1.0.1 through v1.0.9 configurations
  load unchanged under v1.0.10. Mocked Linux and Windows
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

## WSL2 privileged-updater follow-up

- Real Docker upgrade coverage passed four clean, consecutive lifecycles: v1.0.2 legacy updater,
  v1.2.1 checksummed standalone updater, then the same two paths a second time. Every run preserved
  config/cookie hashes and SQLite sentinel data, enabled WAL, and completed the runtime write probe.
- The observed configured runtime was `1000:1000`. The v1.0.2 updater changed legacy runtime
  directories from `root:root 0500` to `1000:1000 0700` and files from `root:root 0400` to
  `1000:1000 0600`; `.env` and `config.yaml` stayed installer-owned `0600`.
- Each fixture resolved Compose `bot`, `worker`, and `local-api` to `1000:1000`, with read-only
  config and writable data binds. Unique Compose project names plus `down --volumes` left no test
  container, network, volume, registry, temporary root, or temporary sudo policy behind.
- ShellCheck, complete Bash parsing, and the Linux mocked updater recovery suite passed. Lock/frozen
  sync, architecture, UTF-8 (236 files), Ruff lint/format (157 files), strict mypy (146 files),
  detect-secrets, `pip check`, and `pip-audit` all passed.
- The default non-contract suite passed with 466 tests, one destructive Local Bot API test skipped,
  and 15 external contracts deselected. Branch coverage was 82.68% against the 80% gate.
- Wheel/sdist build, bundled-license checks, clean-wheel installation, release tar/ZIP/standalone
  updater checksum smoke, example extractor SDK, Compose validation, and all existing CI-image
  gallery-dl/native-selection/UI/doctor/multipart/usage-chart smokes passed.
- Live external YouTube contracts were intentionally not rerun for this filesystem-fixture fix.

## Earlier Windows release verification

- Runtime baseline: CPython 3.14.5, locked yt-dlp 2026.07.04.
- `uv lock --check`: passed.
- `uv sync --frozen --group dev`: passed; 82 packages checked.
- Ruff lint: passed.
- Ruff format check: passed for 157 Python files.
- Strict mypy: passed for 146 source/test files.
- Patch 1.2.2 CLI/architecture regression selection: 38 passed and 6 Linux-only Bash parser
  cases skipped on Windows. The Linux mocked updater suite passed cookie visibility, read-only
  mounts, byte preservation, pre-stop failures, rollback, and project-image cleanup cases.
- Patch 1.2.1 targeted gallery-dl/router, yt-dlp engine, Twitter HLS, and Telegram delivery suite:
  108 passed and 1 symlink test skipped for unavailable Windows privilege.
- Default test suite: 458 passed, 9 skipped on this Windows host (the destructive Local Bot API
  upload, 6 Linux-only complete Bash parse cases, and 2 unavailable symlink cases), with 15 external
  contracts deselected.
- Core branch coverage: 82.59%, above the enforced 80% floor.
- Opt-in contract runner: 12 cases skipped because operator gallery/source fixtures were absent;
  the 3 fixed public YouTube cases failed externally with HTTP 429 / authentication-required after
  network timeouts. No yt-dlp adapter or dependency changed in this patch, but this live gate is not
  green on the current host.
- Architecture boundary check: passed; only
  `infrastructure/ytdlp/` imports yt-dlp and Telethon is absent.
- The YouTube Mix contract still canonicalized to video `DGbwtVtthu8`, but upstream rejected the
  metadata request before format inspection with the same HTTP 429/authentication challenge.
- UTF-8/text integrity: passed for 244 source text files.
- Deterministic source manifest regenerated and verified after the final documentation update.
- SQLite migration, WAL contention, atomic usage, and cancel-safe recovery tests passed.
- Linux and Windows mocked `tmb update` tests passed for success, release-download failure, and
  checksum failure. Linux additionally passed permission rollback, candidate crash-state rollback,
  transaction ordering, lost executable-mode recovery, state preservation, global `tmb` repair,
  `command -v`, installed-script `bash -n`, `tmb status`, post-verification old-image cleanup,
  referenced/foreign-image protection, and cleanup dry-run assertions.
  Both platforms preserve fixture administrator IDs and all three required channels.
- External extractor SDK: lock/sync passed; 1 default test passed and 1 contract was deselected.
- `config.example.yaml`, Compose YAML, both workflow YAML files, and JSON schema parsed successfully.
- PowerShell AST parsing: passed for all 4 scripts.
- Git Bash syntax parsing passed for all 6 release scripts. Portable ShellCheck 0.11.0 also passed
  every Linux management/release script, including the corrected privileged integration test.
- Dependency integrity: `uv run pip check` passed.
- Dependency audit: `pip-audit` reported no known vulnerabilities.
- Detect-secrets baseline and explicit tracked/untracked scans passed.
- Python 1.2.2 sdist and wheel builds passed, including bundled-font/OFL archive inspection and a
  clean-wheel installation/resource/decode smoke.
- Release tar/ZIP/updater asset generation passed; all three SHA-256 files verified and the
  standalone updater retained executable mode.
- The privileged filesystem/SQLite/Docker upgrade test was unavailable on that earlier Windows
  host; the WSL2 follow-up above now supplies the required real-Docker result.
- Local `config.example.yaml` parsing is covered by the test suite; its runtime `config-check`
  correctly rejects the container-only `/data/cookies/cookies.txt` path on this Windows host. The
  required release image mounts a readable cookie fixture before running the same check.
- `git diff --check`: passed.

Tests cover all-channel membership, administrator bypass, cache behavior, proxy schemes and legacy
behavior, old/new container callbacks, codec-first MP4 selection, lower-resolution/fail fallback,
native WebM, pre-spawn timeout rejection, fixed-height behavior, WebM
conversion/delivery, dynamic attribution, multi-artifact delivery, SQLite migration and usage
idempotency, tracked upload progress, multipart persistence, Local API migration safety, and safe
interactive configuration output.

## Checks not executable on the earlier Windows host

- Docker Desktop/Engine is not installed, so an actual Compose startup or final Docker build could
  not run locally. CI has a required image build and the release workflow publishes the supported
  amd64 image only after a matching version tag.
- PSScriptAnalyzer is not installed locally. PowerShell parser/recovery tests passed, and CI retains
  the required Windows PSScriptAnalyzer job.
- Fresh Ubuntu VM and Windows Sandbox end-to-end installer runs need Docker and release credentials
  and were not available on this workstation.
- Twelve source/gallery contracts skipped because operator-maintained fixture URLs were not
  configured. Three fixed public YouTube contracts were attempted and rejected upstream with HTTP
  429/authentication-required. The real Local API upload over 200 MB also remained skipped because
  its destructive opt-in variables were absent.

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
- Broadcast and user export are intentionally outside this release; administrator usage reports
  are aggregate-only and never expose user/admin IDs, URLs, filenames, or downloaded content.

## Release commands

Run `./manage.sh check` (or `manage.ps1 check`) and a real Compose/Docker build on a Docker-capable
release host. Publish a signed/versioned Git tag so CI creates the checksummed source assets and
matching immutable GHCR image. Use reviewed fixtures for opt-in contracts and retain the previous
image, release archive, lockfile, `config.yaml`, and database backup for rollback.
